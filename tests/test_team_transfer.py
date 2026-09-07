# -*- coding: utf-8 -*-
"""团队转移：配置默认值 / 子号导入与状态机 / 四步流程。"""
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from core import db as db_mod
from core import team_transfer as tt
from core.team_transfer import TeamTransferError


def _temp_db():
    """让 core.db 在临时目录建库（db 模块路径锚定项目根，测试用临时目录隔离）。"""
    tmpdir = tempfile.mkdtemp(prefix="turb-team-test-")
    root = Path(db_mod.__file__).resolve().parent.parent
    return root, tmpdir


class TeamTransferConfigTests(unittest.TestCase):
    def test_defaults(self):
        self.assertEqual(tt.BASE_URL, "https://chatgpt.com/backend-api")
        s = tt._settings()
        self.assertEqual(s["role"], "standard-user")
        self.assertEqual(s["seat_type"], "default")
        self.assertEqual(s["delay_invite"], 3)
        self.assertEqual(s["delay_accept"], 2)
        self.assertEqual(s["delay_transfer"], 5)
        self.assertTrue(s["transfer_personal"])

    def test_settings_clamped(self):
        with patch.object(tt.cfg, "TEAM_TRANSFER_REQUEST_TIMEOUT", 9999), \
             patch.object(tt.cfg, "TEAM_TRANSFER_MAX_ATTEMPTS", 99), \
             patch.object(tt.cfg, "TEAM_DELAY_AFTER_INVITE", -5):
            s = tt._settings()
            self.assertEqual(s["timeout"], 120)
            self.assertEqual(s["max_attempts"], 5)
            self.assertEqual(s["delay_invite"], 0)

    def test_retryable_status(self):
        self.assertTrue(tt._retryable_status(429))
        self.assertTrue(tt._retryable_status(502))
        self.assertFalse(tt._retryable_status(404))
        self.assertFalse(tt._retryable_status(403))

    def test_error_attributes(self):
        err = TeamTransferError("x", http_status=401, needs_relogin=True)
        self.assertEqual(err.http_status, 401)
        self.assertTrue(err.needs_relogin)
        self.assertFalse(err.retryable)

    def test_wait_otp_with_progress_logs_start_and_done(self):
        logs = []
        with patch("core.email_provider.wait_for_otp", return_value="123456"):
            code = tt.wait_otp_with_progress(
                "kid@x.com", after_ts=1.0, email_source="generic_api",
                log=logs.append, heartbeat_seconds=30,
            )
        self.assertEqual(code, "123456")
        self.assertTrue(any("开始等待邮箱 OTP" in x for x in logs))
        self.assertTrue(any("来源=generic_api" in x for x in logs))
        self.assertTrue(any("已取到 OTP" in x for x in logs))
        self.assertFalse(any("123456" in x for x in logs))

    def test_wait_otp_with_progress_heartbeat_while_blocking(self):
        logs = []

        def slow(*_a, **_k):
            time.sleep(0.8)
            return "999999"

        with patch("core.email_provider.wait_for_otp", side_effect=slow):
            code = tt.wait_otp_with_progress(
                "kid@x.com", after_ts=1.0, email_source="generic_api",
                log=logs.append, heartbeat_seconds=0.15,
            )
        self.assertEqual(code, "999999")
        self.assertTrue(any("仍在等 OTP" in x for x in logs))
        self.assertTrue(any("已取到 OTP" in x for x in logs))


class TeamChildrenImportTests(unittest.TestCase):
    def setUp(self):
        self._old_sqlite = db_mod._SQLITE_PATH
        self._old_ready = db_mod._SQLITE_READY
        self._old_ready_path = db_mod._SQLITE_READY_PATH
        fd, path = tempfile.mkstemp(suffix=".sqlite3")
        os.close(fd)
        os.remove(path)
        db_mod._SQLITE_PATH = Path(path)
        db_mod._SQLITE_READY = False
        db_mod._SQLITE_READY_PATH = None

    def tearDown(self):
        tmp = db_mod._SQLITE_PATH
        db_mod._SQLITE_PATH = self._old_sqlite
        db_mod._SQLITE_READY = self._old_ready
        db_mod._SQLITE_READY_PATH = self._old_ready_path
        try:
            if tmp and tmp != self._old_sqlite and tmp != db_mod._DEFAULT_SQLITE_PATH:
                os.remove(str(tmp))
        except OSError:
            pass

    def _record(self, email="child1@test.com", pw="pw1"):
        return {
            "email": email,
            "mail_password": pw,
            "code_url": f"https://mail.siderchn.com/emails?email={email}&password={pw}&limit=1",
        }

    def test_import_creates_pool_and_account(self):
        inserted, updated, skipped = db_mod.import_team_children([self._record()])
        self.assertEqual((inserted, updated, skipped), (1, 0, 0))
        acc = db_mod.get_account_by_email("child1@test.com")
        self.assertIsNotNone(acc)
        self.assertEqual(acc.get("email_source"), "generic_api")
        extra = json.loads(acc.get("extra_json") or "{}")
        self.assertTrue(extra.get("imported_team_child"))
        self.assertEqual(extra.get("mail_password"), "pw1")
        # 邮箱池同步创建并已标记 used
        rows = db_mod._load_generic_api_emails()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "used")
        self.assertIn("password=pw1", rows[0]["code_url"])

    def test_reimport_updates_existing_account(self):
        db_mod.import_team_children([self._record()])
        inserted, updated, skipped = db_mod.import_team_children([self._record()])
        self.assertEqual((inserted, updated, skipped), (0, 1, 0))
        self.assertEqual(len(db_mod.list_team_children()), 1)

    def test_import_skips_invalid_rows(self):
        inserted, updated, skipped = db_mod.import_team_children([
            {"email": "", "mail_password": "pw", "code_url": "u"},
            {"email": "a@b.c", "mail_password": "", "code_url": "u"},
            {"email": "a@b.c", "mail_password": "pw", "code_url": ""},
        ])
        self.assertEqual((inserted, updated, skipped), (0, 0, 3))

    def test_state_machine(self):
        db_mod.import_team_children([self._record()])
        acc = db_mod.get_account_by_email("child1@test.com")
        acc_id = acc["id"]
        self.assertTrue(db_mod.claim_account_team_transfer(acc_id))
        self.assertFalse(db_mod.claim_account_team_transfer(acc_id))  # 重复认领失败
        self.assertTrue(db_mod.mark_account_team_transfer_running(acc_id))
        self.assertTrue(db_mod.update_account_team_transfer_progress(acc_id, "invited"))
        self.assertEqual(db_mod.get_account(acc_id)["team_transfer_step"], "invited")
        self.assertTrue(db_mod.update_account_team_transfer(acc_id, {"ok": True, "step": "kicked"}))
        final = db_mod.get_account(acc_id)
        self.assertEqual(final["team_transfer_status"], "success")
        # 终态后可重新认领
        self.assertTrue(db_mod.claim_account_team_transfer(acc_id))
        # 中断恢复
        db_mod.mark_account_team_transfer_running(acc_id)
        recovered = db_mod.recover_interrupted_team_transfers()
        self.assertEqual(recovered, 1)
        self.assertEqual(db_mod.get_account(acc_id)["team_transfer_status"], "failed")


class AdminLoginFlowTests(unittest.TestCase):
    def _session(self):
        session = MagicMock()
        session.session.close = MagicMock()
        return session

    def _session_info(self):
        return {
            "accessToken": "at",
            "user": {"id": "u1"},
            "account": {"id": "team-1", "planType": "team"},
            "accounts": {},
            "expires": "soon",
        }

    def test_follows_authorize_before_password_verify(self):
        order = []
        session = self._session()

        def preflight(*_a, **_k):
            order.append("preflight")
            return session, "https://auth.openai.com/authorize?x=1"

        def follow(_sess, url):
            order.append("follow")
            self.assertIn("authorize", url)
            return "https://auth.openai.com/log-in/password"

        def pwd(_sess, password):
            order.append("password")
            self.assertEqual(password, "secret")
            return {
                "continue_url": "https://chatgpt.com/api/auth/callback/openai?code=abc",
                "page": {"type": "external_url"},
            }

        with patch("core.account_liveness._network_preflight_with_retry", side_effect=preflight), \
             patch("core.openai_auth.follow_authorize", side_effect=follow), \
             patch("core.account_liveness._password_verify", side_effect=pwd), \
             patch("core.account_export.follow_oauth_callback"), \
             patch("core.account_export.fetch_session", return_value=self._session_info()), \
             patch("core.chatgpt_plan.token_claims", return_value={"user_id": "u1", "account_id": "team-1"}):
            result = tt.login_admin("admin@x.com", "secret", log=lambda *_: None)

        self.assertTrue(result["ok"], result.get("error"))
        self.assertEqual(order, ["preflight", "follow", "password"])
        self.assertEqual(result["team_account_id"], "team-1")

    def test_email_verification_after_authorize_skips_password(self):
        session = self._session()
        waiter = MagicMock(return_value="123456")
        with patch("core.account_liveness._network_preflight_with_retry", return_value=(session, "https://auth/x")), \
             patch("core.openai_auth.follow_authorize", return_value="https://auth.openai.com/email-verification"), \
             patch("core.account_liveness._password_verify") as pwd, \
             patch("core.openai_auth.validate_email_otp", return_value={
                 "continue_url": "https://chatgpt.com/api/auth/callback/openai?code=abc",
             }), \
             patch("core.account_export.follow_oauth_callback"), \
             patch("core.account_export.fetch_session", return_value=self._session_info()), \
             patch("core.chatgpt_plan.token_claims", return_value={}):
            result = tt.login_admin("admin@x.com", "secret", otp_waiter=waiter, log=lambda *_: None)
        pwd.assert_not_called()
        waiter.assert_called_once_with("admin@x.com")
        self.assertTrue(result["ok"], result.get("error"))

    def test_workspace_page_selects_team_then_callback(self):
        session = self._session()
        select_resp = MagicMock()
        select_resp.status_code = 200
        select_resp.headers = {}
        select_resp.json.return_value = {
            "continue_url": "https://chatgpt.com/api/auth/callback/openai?code=abc",
        }
        select_resp.text = "{}"
        session.post.return_value = select_resp
        session.get.return_value = MagicMock(status_code=200, url="https://auth.openai.com/workspace")
        session.get_auth_headers.return_value = {}
        session.get_auth_navigate_headers.return_value = {}
        waiter = MagicMock(return_value="123456")
        captured = {}

        def capture_callback(_sess, url, referer=None):
            captured["url"] = url
            captured["referer"] = referer

        with patch("core.account_liveness._network_preflight_with_retry", return_value=(session, "https://auth/x")), \
             patch("core.openai_auth.follow_authorize", return_value="https://auth.openai.com/email-verification"), \
             patch("core.account_liveness._password_verify") as pwd, \
             patch("core.openai_auth.validate_email_otp", return_value={
                 "continue_url": "https://auth.openai.com/workspace",
                 "page": {"type": "workspace"},
                 "oai-client-auth-session": {
                     "workspaces": [
                         {"id": "personal-1", "name": "Personal", "kind": "personal"},
                         {"id": "team-9", "name": "Hccfl", "kind": "team"},
                     ]
                 },
             }), \
             patch("core.account_export.follow_oauth_callback", side_effect=capture_callback), \
             patch("core.account_export.fetch_session", return_value=self._session_info()), \
             patch("core.chatgpt_plan.token_claims", return_value={}):
            result = tt.login_admin("admin@x.com", "secret", otp_waiter=waiter, log=lambda *_: None)

        pwd.assert_not_called()
        self.assertTrue(result["ok"], result.get("error"))
        args, kwargs = session.post.call_args
        body = kwargs.get("data") or (args[1] if len(args) > 1 else "")
        self.assertEqual(json.loads(body), {"workspace_id": "team-9"})
        self.assertIn("workspace/select", str(args[0] if args else kwargs.get("url") or ""))
        self.assertIn("callback/openai", captured.get("url") or "")


class TransferChildFlowTests(unittest.TestCase):
    """四步流程：mock _api_step 校验请求序列、参数与状态流转。"""

    def setUp(self):
        self.captured = []

    def _fake_api_step(self, *, label, token, method, path, proxy, json_body=None, account_id=None):
        self.captured.append({
            "label": label, "method": method, "path": path,
            "json_body": json_body, "account_id": account_id,
            "token_kind": "admin" if token == "ADMIN_AT" else "child",
        })
        return {"ok": True, "http_status": 200, "data": {}, "error": None}

    def test_four_steps_sequence(self):
        logs = []
        with patch.object(tt, "_api_step", side_effect=self._fake_api_step), \
             patch.object(tt.time, "sleep"):
            result = tt.transfer_child(
                admin_email="admin@x.com",
                admin_token="ADMIN_AT",
                team_account_id="team-123",
                child_email="kid@x.com",
                child_token="CHILD_AT",
                child_user_id="user-9",
                log=logs.append,
            )
        self.assertTrue(result["ok"])
        self.assertEqual(result["step"], "kicked")
        self.assertEqual(self.captured[0]["label"], "list_members")
        self.assertTrue(self.captured[0]["path"].startswith("/accounts/team-123/users"))
        ops = self.captured[1:]
        self.assertEqual([c["method"] for c in ops], ["POST", "POST", "POST", "DELETE"])
        self.assertEqual(ops[0]["path"], "/accounts/team-123/invites")
        self.assertEqual(ops[0]["json_body"]["email_addresses"], ["kid@x.com"])
        self.assertEqual(ops[1]["path"], "/accounts/team-123/invites/accept")
        self.assertIsNone(ops[1]["account_id"])
        self.assertEqual(ops[2]["path"], "/accounts/transfer")
        self.assertEqual(ops[2]["json_body"], {"workspace_id": "team-123"})
        self.assertEqual(ops[2]["account_id"], "team-123")
        self.assertEqual(ops[3]["path"], "/accounts/team-123/users/user-9")
        self.assertEqual([c["token_kind"] for c in ops], ["admin", "child", "child", "admin"])
        self.assertEqual(set(result["steps"]), {"invited", "accepted", "transferred", "kicked"})

    def test_failure_stops_and_reports_step(self):
        def fail_transfer(*, label, token, method, path, proxy, json_body=None, account_id=None):
            if label == "transfer_account":
                return {"ok": False, "http_status": 422, "data": None, "error": "transfer_account: HTTP 422", "retryable": False}
            return {"ok": True, "http_status": 200, "data": {}, "error": None}

        with patch.object(tt, "_api_step", side_effect=fail_transfer), \
             patch.object(tt.time, "sleep"):
            result = tt.transfer_child(
                admin_email="admin@x.com", admin_token="A", team_account_id="t",
                child_email="kid@x.com", child_token="C", child_user_id="u",
                log=lambda *_: None,
            )
        self.assertFalse(result["ok"])
        self.assertEqual(result["step"], "transferred")
        self.assertIn("422", result["error"])
        self.assertEqual(set(result["steps"]), {"invited", "accepted", "transferred"})

    def test_clip_http_body_collapses_whitespace(self):
        self.assertEqual(tt._clip_http_body("  a\n b  "), "a b")
        long = "x" * 900
        clipped = tt._clip_http_body(long, limit=20)
        self.assertTrue(clipped.endswith("…"))
        self.assertEqual(len(clipped), 21)

    def test_auth_failure_flags_needs_relogin(self):
        def auth_fail(*, label, **kw):
            raise TeamTransferError("登录态失效", http_status=401, needs_relogin=True)

        with patch.object(tt, "_api_step", side_effect=auth_fail), \
             patch.object(tt.time, "sleep"):
            result = tt.transfer_child(
                admin_email="admin@x.com", admin_token="A", team_account_id="t",
                child_email="kid@x.com", child_token="C", child_user_id="u",
                log=lambda *_: None,
            )
        self.assertFalse(result["ok"])
        self.assertTrue(result.get("needs_relogin"))

    def test_already_member_still_invites_and_accepts(self):
        def already_member(*, label, token, method, path, proxy, json_body=None, account_id=None):
            self.captured.append({"label": label, "method": method, "path": path, "json_body": json_body})
            if label == "list_members":
                return {"ok": True, "http_status": 200, "data": {
                    "items": [{"id": "user-9", "email": "kid@x.com", "role": "standard-user"}],
                }, "error": None}
            if label == "transfer_account":
                self.assertEqual(token, "CHILD_AT")
                return {"ok": True, "http_status": 200, "data": {"success": True}, "error": None}
            if label == "kick_user":
                return {"ok": True, "http_status": 200, "data": {}, "error": None}
            return {"ok": True, "http_status": 200, "data": {}, "error": None}

        logs = []
        with patch.object(tt, "_api_step", side_effect=already_member), \
             patch.object(tt.time, "sleep"):
            result = tt.transfer_child(
                admin_email="admin@x.com", admin_token="ADMIN_AT", team_account_id="team-123",
                child_email="kid@x.com", child_token="CHILD_AT", child_user_id="user-9",
                log=logs.append,
            )
        self.assertTrue(result["ok"])
        labels = [c["label"] for c in self.captured]
        self.assertEqual(labels, ["list_members", "invite", "accept_invite", "transfer_account", "kick_user"])
        self.assertFalse(any("跳过步骤 accepted" in x for x in logs))
        self.assertTrue(any("已在团队=是" in x for x in logs))

    def test_already_member_skips_transfer_only_when_workspace_gone(self):
        def side(*, label, **kw):
            self.captured.append(label)
            if label == "list_members":
                return {"ok": True, "http_status": 200, "data": {
                    "items": [{"id": "u", "email": "kid@x.com", "role": "standard-user"}],
                }, "error": None}
            if label == "transfer_account":
                raise TeamTransferError(
                    'transfer_account: HTTP 403: {"detail":{"code":"invalid_workspace_selected"}}',
                    http_status=403, needs_relogin=True,
                )
            return {"ok": True, "http_status": 200, "data": {}, "error": None}

        logs = []
        with patch.object(tt, "_api_step", side_effect=side), \
             patch.object(tt.time, "sleep"):
            result = tt.transfer_child(
                admin_email="a", admin_token="A", team_account_id="t",
                child_email="kid@x.com", child_token="C", child_user_id="u",
                log=logs.append,
            )
        self.assertTrue(result["ok"])
        self.assertEqual(result["step"], "kicked")
        self.assertTrue(any("跳过步骤 transferred" in x for x in logs))
        self.assertTrue(any("子号没有可合并的个人空间" in x for x in logs))
        self.assertIn("kick_user", self.captured)

    def test_new_member_transfer_failure_is_not_skipped(self):
        def side(*, label, **kw):
            if label == "list_members":
                return {"ok": True, "http_status": 200, "data": {"items": []}, "error": None}
            if label == "transfer_account":
                return {"ok": False, "http_status": 422, "data": None,
                        "error": "transfer_account: HTTP 422: workspace_id", "retryable": False}
            return {"ok": True, "http_status": 200, "data": {}, "error": None}

        with patch.object(tt, "_api_step", side_effect=side), \
             patch.object(tt.time, "sleep"):
            result = tt.transfer_child(
                admin_email="a", admin_token="A", team_account_id="t",
                child_email="kid@x.com", child_token="C", child_user_id="u",
                log=lambda *_: None,
            )
        self.assertFalse(result["ok"])
        self.assertEqual(result["step"], "transferred")
        self.assertIn("422", result["error"])

    def test_accept_workspace_gone_refreshes_token_then_retries(self):
        def side(*, label, token=None, **kw):
            self.captured.append((label, token))
            if label == "list_members":
                return {"ok": True, "http_status": 200, "data": {"items": []}, "error": None}
            if label == "accept_invite" and token == "STALE_AT":
                raise tt._http_auth_error(
                    "accept_invite",
                    403,
                    '{ "error": { "message": "{\\"detail\\":{\\"code\\":\\"invalid_workspace_selected\\"}}", '
                    '"code": "invalid_workspace_selected" }, "status": 403 }',
                )
            return {"ok": True, "http_status": 200, "data": {}, "error": None}

        refreshed = []

        def refresh():
            refreshed.append(1)
            return {"access_token": "FRESH_AT", "account_id": "team-ws"}

        logs = []
        with patch.object(tt, "_api_step", side_effect=side), \
             patch.object(tt.time, "sleep"):
            result = tt.transfer_child(
                admin_email="a", admin_token="A", team_account_id="t",
                child_email="kid@x.com", child_token="STALE_AT", child_user_id="u",
                log=logs.append,
                refresh_child_token=refresh,
            )
        self.assertTrue(result["ok"], result.get("error"))
        self.assertFalse(result.get("needs_relogin"))
        self.assertEqual(result["step"], "kicked")
        self.assertEqual(refreshed, [1])
        self.assertTrue(any("重试接受邀请" in x for x in logs))
        self.assertEqual(
            [label for label, _token in self.captured],
            ["list_members", "invite", "accept_invite", "accept_invite", "transfer_account", "kick_user"],
        )
        accept_tokens = [token for label, token in self.captured if label == "accept_invite"]
        self.assertEqual(accept_tokens, ["STALE_AT", "FRESH_AT"])
        transfer_tokens = [token for label, token in self.captured if label == "transfer_account"]
        self.assertEqual(transfer_tokens, ["FRESH_AT"])

    def test_accept_workspace_gone_without_refresh_fails(self):
        def side(*, label, **kw):
            if label == "list_members":
                return {"ok": True, "http_status": 200, "data": {"items": []}, "error": None}
            if label == "accept_invite":
                raise tt._http_auth_error(
                    "accept_invite", 403, '{"detail":{"code":"invalid_workspace_selected"}}',
                )
            return {"ok": True, "http_status": 200, "data": {}, "error": None}

        with patch.object(tt, "_api_step", side_effect=side), \
             patch.object(tt.time, "sleep"):
            result = tt.transfer_child(
                admin_email="a", admin_token="A", team_account_id="t",
                child_email="kid@x.com", child_token="C", child_user_id="u",
                log=lambda *_: None,
            )
        self.assertFalse(result["ok"])
        self.assertEqual(result["step"], "accepted")
        self.assertTrue(result.get("needs_relogin"))
        self.assertIn("重新登录", result["error"])

    def test_accept_real_auth_failure_is_not_skipped(self):
        def side(*, label, **kw):
            if label == "list_members":
                return {"ok": True, "http_status": 200, "data": {"items": []}, "error": None}
            if label == "accept_invite":
                raise tt._http_auth_error("accept_invite", 401, '{"error":{"message":"unauthorized"}}')
            return {"ok": True, "http_status": 200, "data": {}, "error": None}

        with patch.object(tt, "_api_step", side_effect=side), \
             patch.object(tt.time, "sleep"):
            result = tt.transfer_child(
                admin_email="a", admin_token="A", team_account_id="t",
                child_email="kid@x.com", child_token="C", child_user_id="u",
                log=lambda *_: None,
            )
        self.assertFalse(result["ok"])
        self.assertEqual(result["step"], "accepted")
        self.assertTrue(result.get("needs_relogin"))
        self.assertIn("登录态失效", result["error"])

    def test_http_auth_error_workspace_gone_is_not_relogin(self):
        err = tt._http_auth_error("accept_invite", 403, '{"detail":{"code":"invalid_workspace_selected"}}')
        self.assertFalse(err.needs_relogin)
        self.assertNotIn("登录态失效", str(err))
        real = tt._http_auth_error("accept_invite", 403, '{"error":{"message":"forbidden"}}')
        self.assertTrue(real.needs_relogin)
        self.assertIn("登录态失效", str(real))

    def test_find_member_matches_nested_email_and_account_user_id(self):
        members = [{
            "id": "user-9",
            "account_user_id": "user-9__team-1",
            "verified_email": "Kid@x.com",
            "role": "standard-user",
        }]
        found = tt._find_team_member(members, email="kid@x.com", user_id="missing")
        self.assertEqual(found["id"], "user-9")
        found_uid = tt._find_team_member(members, email="other@x.com", user_id="user-9")
        self.assertEqual(found_uid["id"], "user-9")
        self.assertEqual(tt._extract_member_items({"data": {"items": members}}), members)


class TeamChildOtpTests(unittest.TestCase):
    @patch("core.generic_api_mail_client.fetch_latest_otp", return_value="445566")
    @patch("core.manual_otp.wait_for_manual_otp")
    def test_generic_api_otp_even_when_email_service_off(self, manual, fetch):
        from config import email as email_config
        from core.email_provider import wait_for_otp

        with patch.object(email_config, "USE_EMAIL_SERVICE", False):
            code = wait_for_otp("kid@mail.com", after_ts=1.0, email_source="generic_api")
        self.assertEqual(code, "445566")
        fetch.assert_called_once_with("kid@mail.com", after_ts=1.0)
        manual.assert_not_called()

    @patch("core.generic_api_mail_client.fetch_latest_otp", return_value="778899")
    @patch("core.manual_otp.wait_for_manual_otp")
    @patch("core.db.get_generic_api_email_by_email", return_value={"email": "kid@mail.com", "code_url": "https://mail.example/emails"})
    def test_pool_code_url_binds_generic_api_when_source_missing(self, _pool, manual, fetch):
        from config import email as email_config
        from core.email_provider import wait_for_otp

        with patch.object(email_config, "USE_EMAIL_SERVICE", False), \
             patch("core.email_provider._registered_email_source", return_value=None):
            code = wait_for_otp("kid@mail.com", after_ts=2.0)
        self.assertEqual(code, "778899")
        fetch.assert_called_once()
        manual.assert_not_called()

    @patch("core.manual_otp.wait_for_manual_otp", return_value="000000")
    def test_unbound_email_still_manual_when_service_off(self, manual):
        from config import email as email_config
        from core.email_provider import wait_for_otp

        with patch.object(email_config, "USE_EMAIL_SERVICE", False), \
             patch("core.email_provider._registered_email_source", return_value=None), \
             patch("core.email_provider.resolve_email_source", return_value="outlook"), \
             patch("core.db.get_generic_api_email_by_email", return_value=None):
            code = wait_for_otp("someone@outlook.com", after_ts=1.0)
        self.assertEqual(code, "000000")
        manual.assert_called_once()


class AdminSessionPersistTests(unittest.TestCase):
    def setUp(self):
        from core import team_transfer_service as svc
        self.svc = svc
        self._old_sqlite = db_mod._SQLITE_PATH
        self._old_ready = db_mod._SQLITE_READY
        self._old_ready_path = db_mod._SQLITE_READY_PATH
        self._old_admin = dict(svc._ADMIN)
        self._old_hydrated = svc._ADMIN_HYDRATED
        fd, path = tempfile.mkstemp(suffix=".sqlite3")
        os.close(fd)
        os.remove(path)
        db_mod._SQLITE_PATH = Path(path)
        db_mod._SQLITE_READY = False
        db_mod._SQLITE_READY_PATH = None
        svc._ADMIN.update({
            "state": "idle", "email": "", "access_token": "", "user_id": None,
            "team_account_id": None, "team_plan_type": None, "expires": None,
            "error": None, "logged_in_at": None,
        })
        svc._ADMIN_HYDRATED = False

    def tearDown(self):
        tmp = db_mod._SQLITE_PATH
        db_mod._SQLITE_PATH = self._old_sqlite
        db_mod._SQLITE_READY = self._old_ready
        db_mod._SQLITE_READY_PATH = self._old_ready_path
        self.svc._ADMIN.clear()
        self.svc._ADMIN.update(self._old_admin)
        self.svc._ADMIN_HYDRATED = self._old_hydrated
        try:
            if tmp and tmp != self._old_sqlite and tmp != db_mod._DEFAULT_SQLITE_PATH:
                os.remove(str(tmp))
        except OSError:
            pass

    def test_logged_in_survives_process_restart(self):
        svc = self.svc
        svc._ADMIN.update({
            "state": "logged_in",
            "email": "admin@school.edu",
            "access_token": "at-admin",
            "user_id": "user-1",
            "team_account_id": "team-1",
            "team_plan_type": "team",
            "expires": None,
            "error": None,
            "logged_in_at": "2026-09-04T17:00:00",
        })
        svc._persist_admin()
        svc._ADMIN.update({
            "state": "idle", "email": "", "access_token": "", "user_id": None,
            "team_account_id": None, "team_plan_type": None, "expires": None,
            "error": None, "logged_in_at": None,
        })
        svc._ADMIN_HYDRATED = False
        status = svc.admin_status()
        self.assertEqual(status["state"], "logged_in")
        self.assertEqual(status["email"], "admin@school.edu")
        self.assertTrue(status["has_access_token"])
        self.assertNotIn("access_token", status)
        ctx = svc._admin_context()
        self.assertEqual(ctx["access_token"], "at-admin")
        self.assertEqual(ctx["team_account_id"], "team-1")

    def test_reset_clears_persisted_session(self):
        svc = self.svc
        svc._ADMIN.update({
            "state": "logged_in",
            "email": "admin@school.edu",
            "access_token": "at-admin",
            "team_account_id": "team-1",
        })
        svc._persist_admin()
        svc.reset_admin()
        svc._ADMIN_HYDRATED = False
        status = svc.admin_status()
        self.assertEqual(status["state"], "idle")
        self.assertFalse(status["has_access_token"])
        self.assertIsNone(svc._admin_context())


class LogTailTests(unittest.TestCase):
    def test_missing_file_still_uses_running_fn(self):
        from webui.app import _read_log_tail
        path = Path(tempfile.mkdtemp()) / "missing.log"
        data = _read_log_tail(path, max_bytes=80, running_fn=lambda: True)
        self.assertTrue(data["running"])
        self.assertEqual(data["log"], "")
        data_idle = _read_log_tail(path, max_bytes=80, running_fn=lambda: False)
        self.assertFalse(data_idle["running"])


if __name__ == "__main__":
    unittest.main()
