# -*- coding: utf-8 -*-
"""团队转移后台服务：母号登录态 + 子号转移队列。

母号登录在后台线程执行（密码登录可能阻塞等待人工 OTP）。
登录成功后的 email/AT/team_account_id 会写入 SQLite（storage_meta），
WebUI 重启后自动恢复，不必重新登录。点「取消」会清掉持久化会话。
子号转移走 ThreadPoolExecutor + db 状态机，母号相关步骤（invite/kick）
有全局锁串行，避免对母号并发请求触发风控。
"""
from __future__ import annotations

import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from config import team_transfer as cfg
from core import db
from core.team_transfer import TeamTransferError, login_admin, transfer_child

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_LOG_DIR = _PROJECT_ROOT / "注册日志"

# ---- 母号登录态（进程内存 + SQLite 持久化） ----
_ADMIN_LOCK = threading.Lock()
_ADMIN_META_KEY = "team_admin_session"
_ADMIN_HYDRATED = False
# 每次开始登录 / 退出母号时递增，用来丢弃已取消的后台登录结果。
_ADMIN_GEN = 0
_ADMIN: dict = {
    "state": "idle",  # idle / logging_in / waiting_otp / logged_in / failed
    "email": "",
    "access_token": "",
    "user_id": None,
    "team_account_id": None,
    "team_plan_type": None,
    "expires": None,
    "error": None,
    "logged_in_at": None,
}


def _hydrate_admin() -> None:
    """进程启动后第一次读母号状态时，从 SQLite 恢复上次登录成功的会话。"""
    global _ADMIN_HYDRATED
    if _ADMIN_HYDRATED:
        return
    _ADMIN_HYDRATED = True
    raw = db.get_storage_meta(_ADMIN_META_KEY)
    if not raw:
        return
    try:
        data = json.loads(raw)
    except Exception:
        logger.warning("母号登录态持久化数据无法解析，已忽略")
        return
    if not isinstance(data, dict):
        return
    if data.get("state") != "logged_in" or not str(data.get("access_token") or "").strip():
        return
    with _ADMIN_LOCK:
        if _ADMIN.get("state") != "idle":
            return
        _ADMIN.update({
            "state": "logged_in",
            "email": str(data.get("email") or ""),
            "access_token": str(data.get("access_token") or ""),
            "user_id": data.get("user_id"),
            "team_account_id": data.get("team_account_id"),
            "team_plan_type": data.get("team_plan_type"),
            "expires": data.get("expires"),
            "error": None,
            "logged_in_at": data.get("logged_in_at"),
        })


def _persist_admin() -> None:
    """只把已登录成功的母号会话写入 SQLite；其它状态不覆盖上次成功记录。"""
    with _ADMIN_LOCK:
        payload = {
            "state": _ADMIN.get("state"),
            "email": _ADMIN.get("email") or "",
            "access_token": _ADMIN.get("access_token") or "",
            "user_id": _ADMIN.get("user_id"),
            "team_account_id": _ADMIN.get("team_account_id"),
            "team_plan_type": _ADMIN.get("team_plan_type"),
            "expires": _ADMIN.get("expires"),
            "error": None,
            "logged_in_at": _ADMIN.get("logged_in_at"),
        }
    if payload["state"] == "logged_in" and payload["access_token"] and payload["email"]:
        db.set_storage_meta(_ADMIN_META_KEY, json.dumps(payload, ensure_ascii=False))
        return
    if payload["state"] == "idle":
        db.delete_storage_meta(_ADMIN_META_KEY)

# ---- 子号转移队列 ----
def _workers() -> int:
    return min(4, max(1, int(getattr(cfg, "TEAM_TRANSFER_WORKERS", 2) or 2)))


def _queue_limit() -> int:
    return max(1, int(getattr(cfg, "TEAM_TRANSFER_QUEUE_LIMIT", 500) or 500))


_EXECUTOR = ThreadPoolExecutor(max_workers=_workers(), thread_name_prefix="team-transfer")
_QUEUE_SLOTS = threading.BoundedSemaphore(_queue_limit())
# 母号相关步骤（invite/kick）串行锁：转移并发时保护母号会话
_ADMIN_STEP_LOCK = threading.Lock()

# ============================================================
# 日志
# ============================================================

def log_path(admin_email: str) -> Path:
    safe = str(admin_email or "").replace("/", "_").replace("\\", "_").replace(":", "_")
    return _LOG_DIR / f"team-transfer-{safe}.log"


def _append_log(admin_email: str, line: str, *, clear: bool = False) -> None:
    p = log_path(admin_email)
    p.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%H:%M:%S")
    mode = "w" if clear else "a"
    try:
        with p.open(mode, encoding="utf-8") as f:
            f.write(f"{stamp} [INFO] {line}\n")
    except Exception:
        logger.exception("写团队转移日志失败: %s", p)


# ============================================================
# 母号登录
# ============================================================

def admin_status() -> dict:
    _hydrate_admin()
    with _ADMIN_LOCK:
        state = dict(_ADMIN)
    state.pop("access_token", None)  # AT 不出进程
    state["has_access_token"] = bool(_ADMIN.get("access_token"))
    waiting = _ADMIN.get("state") == "waiting_otp"
    if waiting:
        state["manual_otp_waiting"] = _manual_otp_waiting(_ADMIN.get("email") or "")
    return state


def _manual_otp_waiting(email: str) -> dict | None:
    try:
        from core.manual_otp import list_waiting
        for item in list_waiting():
            if str(item.get("email") or "").lower() == str(email or "").lower():
                return item
    except Exception:
        pass
    return None


def submit_admin_otp(email: str, code: str) -> dict:
    """母号登录等待 OTP 时由 WebUI 提交验证码。"""
    with _ADMIN_LOCK:
        if _ADMIN.get("state") not in {"waiting_otp", "logging_in"}:
            return {"ok": False, "error": "母号当前不在等待验证码状态"}
        expected = str(_ADMIN.get("email") or "").lower()
    if str(email or "").strip().lower() != expected:
        return {"ok": False, "error": "邮箱与当前登录中的母号不一致"}
    from core.manual_otp import submit_manual_otp
    return submit_manual_otp(email, code)


def start_admin_login(email: str, password: str, totp_secret: str = "") -> dict:
    """后台线程执行母号登录。同一时间只允许一个登录任务。"""
    global _ADMIN_GEN
    email = str(email or "").strip()
    password = str(password or "")
    if not email or not password:
        return {"accepted": False, "error": "母号邮箱和密码不能为空"}

    _hydrate_admin()
    with _ADMIN_LOCK:
        if _ADMIN.get("state") in {"logging_in", "waiting_otp"}:
            return {"accepted": False, "error": "母号正在登录中，请等待完成或先退出"}
        _ADMIN_GEN += 1
        _ADMIN.update({
            "state": "logging_in",
            "email": email,
            "access_token": "",
            "user_id": None,
            "team_account_id": None,
            "team_plan_type": None,
            "expires": None,
            "error": None,
            "logged_in_at": None,
        })

    thread = threading.Thread(target=_run_admin_login, args=(email, password, totp_secret), daemon=True, name="team-admin-login")
    thread.start()
    return {"accepted": True, "state": "logging_in", "email": email}


def reset_admin() -> dict:
    """退出当前母号：清内存、清持久化，并丢弃进行中的登录线程结果。"""
    global _ADMIN_GEN
    _hydrate_admin()
    with _ADMIN_LOCK:
        email = _ADMIN.get("email") or ""
        _ADMIN_GEN += 1
        _ADMIN.update({
            "state": "idle",
            "email": "",
            "access_token": "",
            "user_id": None,
            "team_account_id": None,
            "team_plan_type": None,
            "expires": None,
            "error": None,
            "logged_in_at": None,
        })
    if email:
        try:
            from core.manual_otp import clear_waiting
            clear_waiting(email)
        except Exception:
            pass
    _persist_admin()
    _append_log(email or "admin", "[母号] 已退出登录，可更换母号", clear=True)
    return {"ok": True, "state": "idle"}


def _run_admin_login(email: str, password: str, totp_secret: str) -> None:
    with _ADMIN_LOCK:
        gen = _ADMIN_GEN

    def _still_current() -> bool:
        with _ADMIN_LOCK:
            return (
                _ADMIN_GEN == gen
                and str(_ADMIN.get("email") or "") == email
                and _ADMIN.get("state") in {"logging_in", "waiting_otp"}
            )

    def log(line: str) -> None:
        logger.info("[母号登录] %s", line)
        _append_log(email, f"[母号登录] {line}")

    def otp_waiter(target_email: str) -> str:
        if not _still_current():
            raise TeamTransferError("母号已退出，登录已取消")
        with _ADMIN_LOCK:
            if _ADMIN_GEN == gen:
                _ADMIN["state"] = "waiting_otp"
        log("等待人工输入邮箱验证码（WebUI 团队转移页提交）")
        from core.manual_otp import wait_for_manual_otp
        timeout = max(60, int(getattr(cfg, "TEAM_ADMIN_OTP_TIMEOUT", 300) or 300))
        try:
            return wait_for_manual_otp(target_email, timeout=timeout)
        finally:
            with _ADMIN_LOCK:
                if _ADMIN_GEN == gen and _ADMIN.get("state") == "waiting_otp":
                    _ADMIN["state"] = "logging_in"

    try:
        import config as config_pkg
        config_pkg.reload_all()
    except Exception as exc:
        log(f"配置热加载失败，使用当前内存配置: {exc}")

    try:
        result = login_admin(email, password, totp_secret=totp_secret, otp_waiter=otp_waiter, log=log)
    except Exception as exc:
        result = {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:300]}"}

    with _ADMIN_LOCK:
        if _ADMIN_GEN != gen or str(_ADMIN.get("email") or "") != email:
            log("登录结果已丢弃：母号已退出或更换")
            return
        if result.get("ok"):
            _ADMIN.update({
                "state": "logged_in",
                "access_token": result.get("access_token") or "",
                "user_id": result.get("user_id"),
                "team_account_id": result.get("team_account_id"),
                "team_plan_type": result.get("team_plan_type"),
                "expires": result.get("expires"),
                "error": None,
                "logged_in_at": datetime.now().isoformat(timespec="seconds"),
            })
        else:
            _ADMIN.update({
                "state": "failed",
                "access_token": "",
                "error": result.get("error") or "登录失败",
            })
    if result.get("ok"):
        _persist_admin()
        _append_log(email, f"[母号登录] 成功 team_account_id={result.get('team_account_id')}")
    else:
        _append_log(email, f"[母号登录] 失败: {result.get('error')}")


def _admin_context() -> dict | None:
    _hydrate_admin()
    with _ADMIN_LOCK:
        if _ADMIN.get("state") == "logged_in" and _ADMIN.get("access_token") and _ADMIN.get("team_account_id"):
            return {
                "email": _ADMIN.get("email"),
                "access_token": _ADMIN.get("access_token"),
                "team_account_id": _ADMIN.get("team_account_id"),
            }
    return None


# ============================================================
# 子号转移队列
# ============================================================

def is_running(account_id: int) -> bool:
    acc = db.get_account(account_id)
    if not acc:
        return False
    return str(acc.get("team_transfer_status") or "") in {"queued", "running"}


def enqueue_account_team_transfer(*, account_id: int, email: str, trigger: str = "manual") -> dict:
    account_id = int(account_id)
    email = str(email or "").strip()
    if not email:
        return {"accepted": False, "busy": False, "error": "email 为空"}
    if _admin_context() is None:
        return {"accepted": False, "busy": False, "error": "母号未登录或未就绪，请先完成母号登录"}
    if not _QUEUE_SLOTS.acquire(blocking=False):
        return {"accepted": False, "busy": False, "queue_full": True, "error": "转移队列已满，请稍后重试"}
    if not db.claim_account_team_transfer(account_id, trigger=trigger):
        _QUEUE_SLOTS.release()
        return {"accepted": False, "busy": True, "error": "该账号已有转移任务在队列或执行中"}
    _append_log(_ADMIN.get("email") or "admin", f"[转移] 已入队 account_id={account_id} email={email}", clear=False)
    try:
        _EXECUTOR.submit(_run_transfer, account_id=account_id, email=email)
    except Exception as exc:
        _QUEUE_SLOTS.release()
        db.update_account_team_transfer(account_id, {
            "ok": False,
            "error": f"转移入队失败: {type(exc).__name__}: {str(exc)[:160]}",
        })
        return {"accepted": False, "busy": False, "error": "转移入队失败"}
    return {
        "accepted": True,
        "busy": False,
        "account_id": account_id,
        "email": email,
        "status": "queued",
    }


def _run_transfer(*, account_id: int, email: str) -> dict:
    admin_email = ""
    try:
        if not db.mark_account_team_transfer_running(account_id):
            return {"ok": False, "error": "账号已删除或转移状态已被重置，取消执行"}
        ctx = _admin_context()
        if ctx is None:
            result = {"ok": False, "error": "母号登录态已失效，请重新登录母号"}
            db.update_account_team_transfer(account_id, result)
            return result
        admin_email = ctx["email"]

        acc = db.get_account(account_id) or {}
        access_token = str(acc.get("access_token") or "").strip()
        if not access_token:
            result = {"ok": False, "error": "子号没有 accessToken，请先批量登录提取 AT"}
            db.update_account_team_transfer(account_id, result)
            _append_log(admin_email, f"[转移] {email} 失败: {result['error']}")
            return result

        # 子号 user_id：优先登录时回写的 user_id；接受邀请步骤以团队侧视角执行
        child_user_id = str(acc.get("user_id") or "").strip()
        if not child_user_id:
            from core.chatgpt_plan import token_claims
            claims = token_claims(access_token)
            child_user_id = str(claims.get("user_id") or "").strip()
        if not child_user_id:
            result = {"ok": False, "error": "无法确定子号 user_id，请先重新登录该账号"}
            db.update_account_team_transfer(account_id, result)
            _append_log(admin_email, f"[转移] {email} 失败: {result['error']}")
            return result

        def log(line: str) -> None:
            logger.info("[团队转移] %s", line)
            _append_log(admin_email, line)

        def progress(step: str) -> None:
            db.update_account_team_transfer_progress(account_id, step)

        def refresh_child_token() -> dict:
            acc_now = db.get_account(account_id) or {}
            extra: dict = {}
            try:
                raw_extra = acc_now.get("extra_json") or "{}"
                extra = json.loads(raw_extra) if isinstance(raw_extra, str) else (raw_extra or {})
            except Exception:
                extra = {}
            if not isinstance(extra, dict):
                extra = {}
            password = str(
                extra.get("registration_password")
                or extra.get("mail_password")
                or acc_now.get("registration_password")
                or ""
            ).strip()
            email_source = str(acc_now.get("email_source") or "").strip() or None
            totp_secret = str(acc_now.get("totp_secret") or "").strip()

            def otp_waiter(target_email: str) -> str:
                from core.team_transfer import wait_otp_with_progress
                return wait_otp_with_progress(
                    target_email,
                    after_ts=time.time() - 20,
                    email_source=email_source,
                    log=lambda line: log(f"[{email}] {line}"),
                    heartbeat_seconds=10,
                )

            log(f"[{email}] 子号 AT 工作区已失效，重新登录后再接受邀请")
            login_res = login_admin(
                email,
                password,
                totp_secret=totp_secret,
                otp_waiter=otp_waiter,
                log=lambda line: log(f"[{email}] {line}"),
                role_label="子号",
            )
            if not login_res.get("ok") or not str(login_res.get("access_token") or "").strip():
                raise TeamTransferError(
                    f"子号重新登录失败: {login_res.get('error') or '未知错误'}",
                    needs_relogin=True,
                )
            db.update_account_liveness(account_id, {
                "ok": True,
                "status": "live",
                "access_token": login_res["access_token"],
                "checked_at": login_res.get("checked_at"),
                "session": {
                    "accessToken": login_res["access_token"],
                    "expires": login_res.get("expires"),
                    "user": {"id": login_res.get("user_id")},
                    "account": {
                        "id": login_res.get("account_id") or login_res.get("team_account_id"),
                        "planType": login_res.get("team_plan_type"),
                    },
                },
            })
            return {
                "access_token": login_res["access_token"],
                "account_id": login_res.get("account_id") or login_res.get("team_account_id"),
                "user_id": login_res.get("user_id"),
            }

        log(f"[转移] 开始 {email}（团队 {ctx['team_account_id']}，user_id={child_user_id}）")
        result = transfer_child(
            admin_email=ctx["email"],
            admin_token=ctx["access_token"],
            team_account_id=ctx["team_account_id"],
            child_email=email,
            child_token=access_token,
            child_user_id=child_user_id,
            log=log,
            refresh_child_token=refresh_child_token,
        )
        # 步进回写：把已完成的步骤同步到 DB（transfer_child 内部只在结束时返回）
        steps = result.get("steps") or {}
        for step_name, step_res in steps.items():
            if step_res.get("ok"):
                progress(step_name)
        db.update_account_team_transfer(account_id, result)
        if result.get("ok"):
            _append_log(admin_email, f"[转移] {email} 完成")
        else:
            _append_log(admin_email, f"[转移] {email} 失败于 {result.get('step')}: {result.get('error')}")
        return result
    except Exception as exc:
        result = {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:500]}"}
        try:
            db.update_account_team_transfer(account_id, result)
        except Exception:
            logger.exception("[团队转移] 写入异常状态失败: account_id=%s", account_id)
        logger.exception("[团队转移] 后台异常: %s", email)
        try:
            _append_log(admin_email or "admin", f"[转移] {email} 后台异常: {result['error']}")
        except Exception:
            pass
        return result
    finally:
        _QUEUE_SLOTS.release()


def queue_settings() -> dict:
    return {"workers": _workers(), "queue_limit": _queue_limit()}


def transfer_status_snapshot(account_ids: list[int] | None = None) -> list[dict]:
    """返回子号转移状态快照（WebUI 轮询用）。"""
    if account_ids:
        rows = []
        for acc_id in account_ids:
            acc = db.get_account(acc_id)
            if acc:
                rows.append(acc)
    else:
        rows = db.list_team_children()
    return [
        {
            "account_id": int(r.get("id") or 0),
            "email": r.get("email"),
            "status": r.get("team_transfer_status") or "",
            "step": r.get("team_transfer_step") or "",
            "ok": r.get("team_transfer_ok"),
            "error": r.get("team_transfer_error"),
            "completed_at": r.get("team_transfer_completed_at"),
        }
        for r in rows
    ]
