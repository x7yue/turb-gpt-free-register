# -*- coding: utf-8 -*-
"""
团队转移核心流程：母号登录 + 四步（邀请→接受→母号合并个人空间数据→踢出）。

母号登录：
    复用 account_liveness 的协议登录链路（CSRF → Signin → Authorize →
    密码验证，必要时邮箱 OTP / TOTP MFA → OAuth callback → session）。
    OTP 优先走 wait_for_manual_otp（WebUI 人工输入），也接受调用方传入
    自动取码函数。

四步转移：
    用 AT 直接调 chatgpt.com/backend-api（对照 remove_personal_space 参考
    实现）。母号/子号各用独立 BrowserSession（curl_cffi TLS 指纹），种子按
    账号派生保持稳定；直接使用 env.session 绕过通用熔断器，避免一步 403
    把整个多步流程拦腰打断。

注意：step2/step3 的请求体字段来自参考脚本的推测，首次真实运行时按
响应校准（见模块底部 _PAYLOAD 变量与 config.team_transfer）。
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from datetime import datetime, timezone

from config import team_transfer as cfg
from core.chatgpt_plan import normalize_token, resolve_plan_check_route, token_claims
from core.session import BrowserSession

logger = logging.getLogger(__name__)

BASE_URL = "https://chatgpt.com/backend-api"

# 后端 API 中账号/用户 id 的常见形态（防手滑拼错 URL）
_TEAM_ID_RE = re.compile(r"^[0-9a-zA-Z\-_.]+$")


class TeamTransferError(RuntimeError):
    """团队转移失败（含 http_status / retryable / needs_relogin 附加信息）。"""

    def __init__(self, message: str, *, http_status: int | None = None,
                 retryable: bool = False, needs_relogin: bool = False):
        super().__init__(message)
        self.http_status = http_status
        self.retryable = retryable
        self.needs_relogin = needs_relogin


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _settings() -> dict:
    """读配置（模块属性方式，支持热加载），钳制到安全范围。"""
    return {
        "role": str(getattr(cfg, "TRANSFER_ROLE", "standard-user") or "standard-user"),
        "seat_type": str(getattr(cfg, "TRANSFER_SEAT_TYPE", "default") or "default"),
        "tos_version": str(getattr(cfg, "TRANSFER_ACCEPTED_TOS_VERSION", "2024-12-17") or "2024-12-17"),
        "transfer_personal": bool(getattr(cfg, "TRANSFER_PERSONAL", True)),
        "delay_invite": max(0, int(getattr(cfg, "TEAM_DELAY_AFTER_INVITE", 3) or 0)),
        "delay_accept": max(0, int(getattr(cfg, "TEAM_DELAY_AFTER_ACCEPT", 2) or 0)),
        "delay_transfer": max(0, int(getattr(cfg, "TEAM_DELAY_AFTER_TRANSFER", 5) or 0)),
        "timeout": min(120, max(5, int(getattr(cfg, "TEAM_TRANSFER_REQUEST_TIMEOUT", 30) or 30))),
        "max_attempts": min(5, max(1, int(getattr(cfg, "TEAM_TRANSFER_MAX_ATTEMPTS", 3) or 3))),
        "retry_delay": min(30, max(0, int(getattr(cfg, "TEAM_TRANSFER_RETRY_DELAY", 2) or 2))),
    }


def _page_type(result: dict | None) -> str:
    if not isinstance(result, dict):
        return ""
    page = result.get("page") if isinstance(result.get("page"), dict) else {}
    return str(page.get("type") or "")


def _auth_session(result: dict | None) -> dict:
    if not isinstance(result, dict):
        return {}
    sess = result.get("oai-client-auth-session") or {}
    return sess if isinstance(sess, dict) else {}


def _pick_team_workspace(workspaces: list) -> dict | None:
    items = [w for w in workspaces if isinstance(w, dict) and w.get("id")]
    if not items:
        return None
    for item in items:
        blob = " ".join(
            str(item.get(k) or "") for k in ("kind", "type", "plan_type", "planType", "name")
        ).lower()
        if "team" in blob or "business" in blob:
            return item
    return items[0]


def _is_workspace_step(url: str, page_type: str) -> bool:
    return page_type == "workspace" or "/workspace" in str(url or "")


def _select_workspace(session: BrowserSession, workspace_id: str) -> dict:
    headers = session.get_auth_headers(referer="https://auth.openai.com/workspace")
    headers["content-type"] = "application/json"
    resp = session.post(
        "https://auth.openai.com/api/accounts/workspace/select",
        headers=headers,
        data=json.dumps({"workspace_id": workspace_id}),
        allow_redirects=False,
    )
    status = int(getattr(resp, "status_code", 0) or 0)
    if status >= 400:
        raise TeamTransferError(
            f"选择 workspace 失败 HTTP {status}: {(getattr(resp, 'text', None) or '')[:300]}"
        )
    try:
        data = resp.json()
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    loc = ""
    try:
        loc = resp.headers.get("location") or resp.headers.get("Location") or ""
    except Exception:
        loc = ""
    if loc and not data.get("continue_url"):
        data["continue_url"] = loc
    return data


def _complete_after_otp(session: BrowserSession, validate_result: dict, *, log) -> dict:
    """OTP 成功后：Team 号若停在 workspace 选择页则先 select，再 OAuth callback。"""
    from core.account_export import fetch_session, follow_oauth_callback
    from core.account_liveness import _extract_continue_url

    page_type = _page_type(validate_result)
    continue_url = _extract_continue_url(validate_result)
    if "about-you" in continue_url:
        raise TeamTransferError("母号登录后进入资料页，疑似账号未完成注册")

    if _is_workspace_step(continue_url, page_type):
        workspaces = _auth_session(validate_result).get("workspaces") or []
        picked = _pick_team_workspace(workspaces)
        if not picked:
            raise TeamTransferError(f"母号需要选择 workspace，但响应里没有可用工作区: {workspaces}")
        log(
            f"选择 workspace：{picked.get('name') or picked.get('id')} "
            f"({picked.get('kind') or picked.get('type') or 'unknown'})"
        )
        if continue_url:
            try:
                nav_headers = session.get_auth_navigate_headers(
                    referer="https://auth.openai.com/email-verification"
                )
                session.get(continue_url, headers=nav_headers, allow_redirects=True)
            except Exception as exc:
                log(f"打开 workspace 页失败，继续尝试 select: {type(exc).__name__}: {str(exc)[:160]}")
        select_res = _select_workspace(session, str(picked["id"]))
        continue_url = _extract_continue_url(select_res)
        if not continue_url:
            raise TeamTransferError(f"workspace/select 后没有回调地址: {select_res}")
        log("workspace 已选择，完成 OAuth 回调")
        follow_oauth_callback(session, continue_url, referer="https://auth.openai.com/workspace")
        return fetch_session(session)

    if not continue_url:
        raise TeamTransferError(f"OTP 验证成功但没有 continue_url: {validate_result}")
    follow_oauth_callback(session, continue_url, referer="https://auth.openai.com/email-verification")
    return fetch_session(session)


# ============================================================
# 母号登录
# ============================================================

def login_admin(
    email: str,
    password: str,
    *,
    totp_secret: str = "",
    otp_waiter=None,
    proxy: str | None = None,
    log=print,
    role_label: str = "母号",
) -> dict:
    """
    母号/子号密码登录，返回 {ok, email, access_token, user_id, account_id, team_account_id, expires, error}。

    team_account_id 取 session 返回的 accounts 里 planType=team 的条目；找不到时
    回退 AT 的 chatgpt_account_id claim（由调用方决定是否继续）。

    otp_waiter: fn(email) -> code。默认用 core.manual_otp.wait_for_manual_otp
    （WebUI 人工输入通道）。
    """
    # 延迟导入：登录链路依赖 openai_auth/sentinel 全家桶，避免模块加载即拉起
    from core.chatgpt_auth import get_csrf_token, signin_openai
    from core.openai_auth import follow_authorize
    from core.account_liveness import (
        _extract_continue_url,
        _extract_factor_id,
        _login_via_email_otp,
        _mfa_issue_challenge,
        _mfa_verify,
        _network_preflight_with_retry,
        _password_verify,
    )
    from core.account_export import fetch_session
    from core.manual_otp import wait_for_manual_otp

    if otp_waiter is None:
        otp_waiter = wait_for_manual_otp

    result = {
        "ok": False,
        "email": email,
        "access_token": "",
        "user_id": None,
        "account_id": None,
        "team_account_id": None,
        "team_plan_type": None,
        "expires": None,
        "checked_at": _now_iso(),
        "error": None,
    }

    session: BrowserSession | None = None
    try:
        log(f"开始{role_label}登录：{email}")
        # 备用登录链：CSRF → Signin（providers 容易被 CF 拦，不作硬门槛）
        session, authorize_url = _network_preflight_with_retry(email, proxy)
        log("authorize URL 已获取，跟随登录重定向")
        final_url = follow_authorize(session, authorize_url)
        log(f"authorize 落点：{final_url}")
        otp_after_ts = time.time()

        session_info: dict | None = None
        continue_url = ""
        page_type = ""
        password_result: dict = {}

        if "email-verification" in (final_url or ""):
            log("authorize 后进入邮箱验证页，开始等待 OTP")
            current_otp = otp_waiter(email)
            log("OTP 已提交验证，继续完成登录")
            from core.openai_auth import EmailOtpInvalidError, validate_email_otp
            try:
                validate_result = validate_email_otp(session, current_otp, sentinel_header=None, so_header=None)
            except EmailOtpInvalidError as exc:
                raise TeamTransferError(f"邮箱验证码无效/过期，请重新登录: {exc}") from exc
            session_info = _complete_after_otp(session, validate_result, log=log)
        else:
            log("authorize 后进入密码页，开始密码登录")
            try:
                password_result = _password_verify(session, password)
            except Exception as exc:
                body = ""
                resp = getattr(exc, "response", None)
                if resp is not None:
                    body = str(getattr(resp, "text", None) or getattr(resp, "content", "") or "")[:300]
                raise TeamTransferError(
                    f"密码验证失败: {type(exc).__name__}: {str(exc)[:160]}"
                    + (f" body={body}" if body else "")
                ) from exc
            continue_url = _extract_continue_url(password_result)
            page = password_result.get("page") if isinstance(password_result, dict) else {}
            page = page if isinstance(page, dict) else {}
            page_type = str(page.get("type") or "")
            log(f"密码验证完成：page_type={page_type or '空'}, has_continue_url={bool(continue_url)}")

        if session_info is None and ("/mfa-challenge/" in continue_url or page_type == "mfa_challenge"):
            factor_id = _extract_factor_id(password_result, continue_url)
            if not factor_id:
                raise TeamTransferError(f"密码登录后进入 MFA 但未拿到 factor_id: {password_result}")
            if not totp_secret:
                raise TeamTransferError("母号开启了 TOTP MFA，但未提供 totp_secret")
            log(f"进入 MFA challenge，提交 TOTP：factor_id={factor_id}")
            _mfa_issue_challenge(session, factor_id)
            import pyotp
            mfa_result = _mfa_verify(session, factor_id, pyotp.TOTP(totp_secret).now())
            mfa_continue_url = _extract_continue_url(mfa_result) or continue_url
            from core.account_export import follow_oauth_callback
            follow_oauth_callback(session, mfa_continue_url, referer=f"https://auth.openai.com/mfa-challenge/{factor_id}")
            session_info = fetch_session(session)

        elif session_info is None and ("email-verification" in continue_url or page_type in {"email_verification", "email_otp_send"}):
            # 密码登录后仍要求邮箱验证：人工在 WebUI 提交验证码
            log("密码登录后进入邮箱验证页，开始等待 OTP")
            current_otp = otp_waiter(email)
            log("OTP 已提交验证，继续完成登录")
            from core.openai_auth import EmailOtpInvalidError, validate_email_otp
            try:
                validate_result = validate_email_otp(session, current_otp, sentinel_header=None, so_header=None)
            except EmailOtpInvalidError as exc:
                raise TeamTransferError(f"邮箱验证码无效/过期，请重新登录: {exc}") from exc
            session_info = _complete_after_otp(session, validate_result, log=log)

        elif session_info is None and continue_url:
            log("密码登录直接给出回调地址，完成回调")
            from core.account_export import follow_oauth_callback
            follow_oauth_callback(session, continue_url, referer="https://auth.openai.com/log-in/password")
            session_info = fetch_session(session)
        elif session_info is None:
            raise TeamTransferError(f"密码登录成功但没有可用 continue_url: {password_result}")

        access_token = str((session_info or {}).get("accessToken") or "")
        if not access_token:
            raise TeamTransferError("登录后未拿到 accessToken")

        claims = token_claims(access_token)
        result.update({
            "ok": True,
            "access_token": access_token,
            "user_id": (session_info.get("user") or {}).get("id") or claims.get("user_id"),
            "account_id": claims.get("account_id"),
            "expires": session_info.get("expires"),
        })

        # 找 team workspace：优先 session.accounts，回退 AT claim
        account_block = session_info.get("account") or {}
        accounts_map = session_info.get("accounts") if isinstance(session_info.get("accounts"), dict) else {}
        team_entry = None
        if account_block.get("planType") == "team" and account_block.get("id"):
            team_entry = account_block
        for _key, item in accounts_map.items():
            if isinstance(item, dict) and item.get("planType") == "team" and item.get("id"):
                team_entry = item
                break
        if team_entry:
            result["team_account_id"] = team_entry.get("id")
            result["team_plan_type"] = team_entry.get("planType")
        elif claims.get("account_id"):
            result["team_account_id"] = claims.get("account_id")
            result["team_plan_type"] = claims.get("claim_plan_type")
            log(f"session 未直接给出 team workspace，回退 AT claim: {result['team_account_id']}")
        log(
            f"{role_label}登录成功：{email} plan={claims.get('claim_plan_type') or '-'} "
            f"account_id={result.get('account_id')} team_account_id={result.get('team_account_id')}"
        )
        return result
    except TeamTransferError as exc:
        result["ok"] = False
        result["error"] = str(exc)
        logger.exception("%s登录失败: %s", role_label, email)
        return result
    except Exception as exc:
        result["ok"] = False
        result["error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
        logger.exception("%s登录失败: %s", role_label, email)
        return result
    finally:
        if session is not None:
            try:
                session.session.close()
            except Exception:
                pass


# ============================================================
# AT 直调 backend-api 的通用单步
# ============================================================

def _retryable_status(status: int) -> bool:
    return status in (408, 409, 425, 429) or 500 <= status < 600


def _clip_http_body(text: str, limit: int = 800) -> str:
    raw = " ".join(str(text or "").split())
    if len(raw) <= limit:
        return raw
    return raw[:limit] + "…"


def _api_step(
    *,
    label: str,
    token: str,
    method: str,
    path: str,
    proxy: str | None,
    json_body: dict | None = None,
    account_id: str | None = None,
) -> dict:
    """
    单个 backend-api 调用，带临时性错误重试。返回 {ok, http_status, data, error}。

    401/403 抛 TeamTransferError(needs_relogin=True) 由上层决定终止/换号。
    """
    settings = _settings()
    route = resolve_plan_check_route(explicit_proxy=proxy)
    seed = f"team:{normalize_token(token)[:40]}:{label}"
    url = f"{BASE_URL}{path}"

    last_error: dict | None = None
    for attempt in range(1, settings["max_attempts"] + 1):
        env = None
        resp = None
        try:
            env = BrowserSession(proxy=route["proxy"], detect_exit_geo=False, fingerprint_seed=seed)
            headers = env._get_common_headers()
            headers.update({
                "accept": "*/*",
                "authorization": f"Bearer {normalize_token(token)}",
                "content-type": "application/json",
                "oai-device-id": env.device_id,
                "oai-language": env.navigator_language(),
                "origin": "https://chatgpt.com",
                "referer": "https://chatgpt.com/",
                "sec-fetch-dest": "empty",
                "sec-fetch-mode": "cors",
                "sec-fetch-site": "same-origin",
            })
            if account_id:
                headers["chatgpt-account-id"] = account_id
            resp = env.session.request(
                method.upper(),
                url,
                headers=headers,
                data=None if json_body is None else json.dumps(json_body),
                allow_redirects=False,
                timeout=settings["timeout"],
            )
            status = int(resp.status_code)
            text = resp.text or ""
            body = _clip_http_body(text)
            if 200 <= status < 300:
                try:
                    data = resp.json()
                except Exception:
                    data = {"_raw": text[:1000]}
                logger.info("[团队转移] %s %s %s HTTP %s ok", label, method.upper(), path, status)
                return {"ok": True, "http_status": status, "data": data, "error": None}
            logger.warning(
                "[团队转移] %s %s %s HTTP %s account_id=%s body=%s",
                label, method.upper(), path, status, account_id or "-", body or "(empty)",
            )
            if status in (401, 403):
                raise _http_auth_error(label, status, body)
            last_error = {
                "ok": False,
                "http_status": status,
                "data": None,
                "error": f"{label}: HTTP {status}: {body or '(empty body)'}",
                "retryable": _retryable_status(status),
            }
        except TeamTransferError:
            raise
        except Exception as exc:
            last_error = {
                "ok": False,
                "http_status": None,
                "data": None,
                "error": f"{label}: {type(exc).__name__}: {str(exc)[:200]}",
                "retryable": True,
            }
        finally:
            if env is not None:
                try:
                    env.session.close()
                except Exception:
                    pass

        if not last_error.get("retryable") or attempt >= settings["max_attempts"]:
            return last_error
        wait = settings["retry_delay"] * attempt
        logger.warning("[团队转移] %s 临时失败（%s/%s），%ss 后重试: %s", label, attempt, settings["max_attempts"], wait, last_error["error"])
        time.sleep(wait)

    return last_error or {"ok": False, "http_status": None, "data": None, "error": f"{label}: 未执行"}


def _error_text(exc: Exception | dict | None) -> str:
    if isinstance(exc, dict):
        return str(exc.get("error") or "")
    return str(exc or "")


def _error_has_code(exc: Exception | dict | None, *codes: str) -> bool:
    blob = _error_text(exc).lower()
    return any(str(code or "").lower() in blob for code in codes if code)


def _workspace_gone(blob: Exception | dict | str | None) -> bool:
    """原个人空间已合并/失效：invalid_workspace_selected 或 token_expired。"""
    text = blob if isinstance(blob, str) else _error_text(blob)
    return _error_has_code({"error": text}, "invalid_workspace_selected", "token_expired")


def _http_auth_error(label: str, status: int, body: str) -> TeamTransferError:
    """401/403：工作区失效不算登录态丢失，交给上层按步骤决定是否跳过。"""
    gone = _workspace_gone(body)
    prefix = f"{label}: HTTP {status}" if gone else f"{label}: 登录态失效 HTTP {status}"
    return TeamTransferError(
        f"{prefix}: {body or '(empty body)'}",
        http_status=status,
        retryable=False,
        needs_relogin=not gone,
    )


def _extract_member_items(data) -> list[dict]:
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if not isinstance(data, dict):
        return []
    for key in ("items", "users", "members", "account_users"):
        val = data.get(key)
        if isinstance(val, list):
            return [x for x in val if isinstance(x, dict)]
    inner = data.get("data")
    if inner is not None and inner is not data:
        return _extract_member_items(inner)
    return []


def _list_team_members(admin_token: str, team_account_id: str, proxy: str | None) -> list[dict]:
    """母号拉团队成员；失败时返回空列表，不中断主流程。"""
    try:
        res = _api_step(
            label="list_members",
            token=admin_token,
            method="GET",
            path=f"/accounts/{team_account_id}/users?limit=50&offset=0&query=",
            proxy=proxy,
            account_id=team_account_id,
        )
    except TeamTransferError:
        return []
    if not res.get("ok"):
        return []
    return _extract_member_items(res.get("data"))


def _member_emails(item: dict) -> set[str]:
    values = [
        item.get("email"),
        item.get("verified_email"),
        item.get("email_address"),
    ]
    user = item.get("user") if isinstance(item.get("user"), dict) else {}
    values.extend([user.get("email"), user.get("verified_email"), user.get("email_address")])
    return {str(v).strip().lower() for v in values if str(v or "").strip()}


def _find_team_member(members: list[dict], *, email: str, user_id: str) -> dict | None:
    email_l = str(email or "").strip().lower()
    uid = str(user_id or "").strip()
    for item in members:
        item_uid = str(item.get("id") or "").strip()
        account_uid = str(item.get("account_user_id") or "").strip().split("__")[0]
        if uid and uid in (item_uid, account_uid):
            return item
        if email_l and email_l in _member_emails(item):
            return item
    return None


def _mail_request_timeout() -> int:
    return max(5, min(120, int(getattr(cfg, "TEAM_MAIL_REQUEST_TIMEOUT", 40) or 40)))


def _token_account_id(token: str) -> str | None:
    try:
        aid = str(token_claims(token).get("account_id") or "").strip()
    except Exception:
        return None
    return aid or None


def wait_otp_with_progress(
    email: str,
    *,
    after_ts: float,
    email_source: str | None = None,
    log=print,
    heartbeat_seconds: float = 10,
    max_wait: int | None = None,
) -> str:
    """等待 OTP，期间按心跳把进度写进调用方日志，避免看起来像卡住。"""
    from config import email as email_cfg
    from core.email_provider import wait_for_otp

    wait_limit = int(max_wait if max_wait is not None else (getattr(email_cfg, "OTP_MAX_WAIT", 90) or 90))
    poll = int(getattr(email_cfg, "OTP_POLL_INTERVAL", 3) or 3)
    source = str(email_source or "").strip() or "自动"
    beat = float(heartbeat_seconds if heartbeat_seconds is not None else 10)
    if beat <= 0:
        beat = 10.0
    beat = max(0.05, beat)
    log(
        f"开始等待邮箱 OTP：来源={source}，最长 {wait_limit}s，"
        f"约每 {poll}s 拉一次收件箱（单次请求最多约 {_mail_request_timeout()}s，超时也会打日志）"
    )
    started = time.time()
    stop = threading.Event()

    def _heartbeat() -> None:
        while not stop.wait(beat):
            elapsed = int(time.time() - started)
            left = max(0, wait_limit - elapsed)
            log(f"仍在等 OTP：已等 {elapsed}s，还剩约 {left}s，收件箱轮询中…")

    worker = threading.Thread(target=_heartbeat, name=f"otp-heartbeat:{email}", daemon=True)
    worker.start()
    try:
        code = wait_for_otp(email, after_ts=after_ts, email_source=email_source, max_wait=wait_limit)
        elapsed = int(time.time() - started)
        digits = len(str(code or "").strip())
        log(f"已取到 OTP（等待 {elapsed}s，{digits} 位），正在提交验证")
        return code
    except Exception as exc:
        elapsed = int(time.time() - started)
        log(f"等待 OTP 失败（已等 {elapsed}s）：{type(exc).__name__}: {str(exc)[:220]}")
        raise
    finally:
        stop.set()


# ============================================================
# 四步流程
# ============================================================

def transfer_child(
    *,
    admin_email: str,
    admin_token: str,
    team_account_id: str,
    child_email: str,
    child_token: str,
    child_user_id: str,
    proxy: str | None = None,
    log=print,
    refresh_child_token=None,
) -> dict:
    """
    对单个子号执行四步：邀请 → 接受 → 母号合并个人空间数据 → 踢出。

    child_user_id 是子号加入团队后的 user id（PATCH/DELETE 用）。
    refresh_child_token: 可选 fn() -> {access_token, account_id?}。接受邀请若因
    子号 AT 绑着已失效个人空间而 403，会重新登录子号后再真正 POST accept。

    返回 {ok, email, steps: {step: {...}}, step, error, checked_at}。
    ok=True 表示四步全部成功（最后一步为 kicked）。
    """
    settings = _settings()
    steps: dict[str, dict] = {}
    result = {
        "ok": False,
        "email": child_email,
        "steps": steps,
        "step": "",
        "error": None,
        "checked_at": _now_iso(),
    }

    def _record(step: str, res: dict) -> None:
        steps[step] = {
            "ok": bool(res.get("ok")),
            "http_status": res.get("http_status"),
            "error": res.get("error"),
        }

    def _run_step(step: str, **kw) -> dict:
        result["step"] = step
        res = _api_step(**kw)
        _record(step, res)
        if not res.get("ok"):
            result["error"] = res.get("error") or f"{step} 失败"
            raise TeamTransferError(result["error"], http_status=res.get("http_status"))
        return res

    def _mark_skipped(step: str, reason: str) -> None:
        _record(step, {"ok": True, "http_status": 200, "error": None})
        log(f"[{child_email}] 跳过步骤 {step}：{reason}")

    try:
        members = _list_team_members(admin_token, team_account_id, proxy)
        already_member = _find_team_member(members, email=child_email, user_id=child_user_id)
        log(
            f"[{child_email}] 成员列表 {len(members)} 人，"
            f"已在团队={'是 role=' + str(already_member.get('role') or '-') if already_member else '否'}"
        )

        # 步骤1：母号邀请子号。已在团队里也照发，409/already 才视为邀请已存在。
        log(f"[{child_email}] 步骤1/4 邀请加入团队 {team_account_id}")
        try:
            invite_res = _run_step(
                "invited",
                label="invite",
                token=admin_token,
                method="POST",
                path=f"/accounts/{team_account_id}/invites",
                proxy=proxy,
                account_id=team_account_id,
                json_body={
                    "email_addresses": [child_email],
                    "role": settings["role"],
                    "seat_type": settings["seat_type"],
                },
            )
            invite_data = invite_res.get("data")
            if invite_data is not None:
                log(f"[{child_email}] 邀请响应: {_clip_http_body(json.dumps(invite_data, ensure_ascii=False), 500)}")
        except TeamTransferError as exc:
            if exc.http_status == 409 or _error_has_code(exc, "already"):
                _mark_skipped("invited", f"邀请接口返回已存在：{exc}")
            else:
                raise
        time.sleep(settings["delay_invite"])

        # 步骤2：子号真正接受邀请。网页 JS：
        # POST /accounts/{acceptWorkspaceId}/invites/accept
        # chatgpt-account-id = 当前会话工作区（子号 JWT 里的 account）。
        # 子号 AT 若仍绑着已合并掉的个人空间，先重新登录再 POST，不跳过。
        child_session = {
            "token": child_token,
            "account_id": _token_account_id(child_token),
        }

        def _do_accept():
            return _run_step(
                "accepted",
                label="accept_invite",
                token=child_session["token"],
                method="POST",
                path=f"/accounts/{team_account_id}/invites/accept",
                proxy=proxy,
                account_id=child_session["account_id"],
                json_body={},
            )

        log(
            f"[{child_email}] 步骤2/4 接受邀请 POST /accounts/{team_account_id}/invites/accept "
            f"chatgpt-account-id={child_session['account_id'] or '-'}"
        )
        try:
            _do_accept()
        except TeamTransferError as exc:
            if not _workspace_gone(exc):
                raise
            if not callable(refresh_child_token):
                raise TeamTransferError(
                    f"accept_invite: 子号 AT 绑定的工作区已失效，无法接受邀请，请先重新登录该子号: {exc}",
                    http_status=exc.http_status,
                    needs_relogin=True,
                ) from exc
            log(f"[{child_email}] 子号工作区已失效，重新登录后再接受邀请")
            refreshed = refresh_child_token() or {}
            new_token = str(refreshed.get("access_token") or "").strip()
            if not new_token:
                raise TeamTransferError(
                    f"子号重新登录后仍没有 accessToken，无法接受邀请: {refreshed.get('error') or ''}".strip(),
                    needs_relogin=True,
                )
            child_session["token"] = new_token
            child_session["account_id"] = (
                str(refreshed.get("account_id") or "").strip() or _token_account_id(new_token)
            )
            log(
                f"[{child_email}] 子号已重新登录，重试接受邀请 "
                f"chatgpt-account-id={child_session['account_id'] or '-'}"
            )
            _do_accept()
        time.sleep(settings["delay_accept"])

        # 步骤3：母号合并个人空间数据（Settings → Merge）。
        # POST /accounts/transfer，Bearer 母号 AT，chatgpt-account-id 与
        # body.workspace_id 都是团队 workspace。个人空间已合并过时会
        # invalid_workspace_selected / token_expired，按已完成跳过。
        log(f"[{child_email}] 步骤3/4 母号合并个人空间 POST /accounts/transfer workspace_id={team_account_id}")
        try:
            _run_step(
                "transferred",
                label="transfer_account",
                token=admin_token,
                method="POST",
                path="/accounts/transfer",
                proxy=proxy,
                account_id=team_account_id,
                json_body={"workspace_id": team_account_id},
            )
        except TeamTransferError as exc:
            if _workspace_gone(exc):
                _mark_skipped("transferred", f"母号没有可合并的个人空间（可能已合并过）：{exc}")
            else:
                raise
        time.sleep(settings["delay_transfer"])

        # 步骤4：母号踢出子号
        log(f"[{child_email}] 步骤4/4 踢出子号")
        _run_step(
            "kicked",
            label="kick_user",
            token=admin_token,
            method="DELETE",
            path=f"/accounts/{team_account_id}/users/{child_user_id}",
            proxy=proxy,
            account_id=team_account_id,
        )

        result["ok"] = True
        result["step"] = "kicked"
        log(f"[{child_email}] 四步流程全部完成")
        return result
    except TeamTransferError as exc:
        result["error"] = str(exc)
        if exc.needs_relogin:
            result["needs_relogin"] = True
        log(f"[{child_email}] 失败于步骤 {result['step']}: {exc}")
        return result
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
        logger.exception("[团队转移] 子号流程异常: %s", child_email)
        return result
