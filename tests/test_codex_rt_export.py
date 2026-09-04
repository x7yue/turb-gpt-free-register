# -*- coding: utf-8 -*-
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import db
from webui.app import create_app


def _storage_patches(root: Path) -> dict:
    missing = root / "missing.json"
    return {
        "_ACCOUNTS_JSON": root / "accounts.json",
        "_OUTLOOK_JSON": root / "outlook.json",
        "_GENERIC_API_EMAIL_JSON": root / "generic.json",
        "_JOBS_JSON": root / "jobs.json",
        "_DOMAIN_EMAIL_JSON": missing,
        "_LEGACY_ACCOUNTS_JSON": missing,
        "_LEGACY_OUTLOOK_JSON": missing,
        "_LEGACY_JOBS_JSON": missing,
        "_CODEX_DIR": root / "codex_accounts",
        "_CODEX_AGENT_DIR": root / "codex_agent_accounts",
        "_LEGACY_CODEX_EXPORT_STATE": root / "codex-export.json",
        "_SQLITE_READY": False,
        "_SQLITE_READY_PATH": None,
    }


class CodexRtExportTests(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.root = Path(self._td.name)
        (self.root / "accounts.json").write_text("[]", encoding="utf-8")
        self._cm = patch.multiple(db, **_storage_patches(self.root))
        self._cm.start()
        self.addCleanup(self._cm.stop)
        self.addCleanup(self._td.cleanup)
        self.app = create_app(auth_code="test-auth")
        self.client = self.app.test_client()
        self.headers = {"X-Auth-Code": "test-auth"}

    def _insert_account(self, email: str, **kwargs) -> int:
        return db.insert_account(email=email, access_token="at-" + email, **kwargs)

    def _save_codex(self, email: str, refresh_token: str, plan: str = "") -> str:
        fname = f"codex-{email}.json" if not plan else f"codex-{email}-{plan}.json"
        db.upsert_codex_credential({
            "type": "codex",
            "email": email,
            "refresh_token": refresh_token,
            "access_token": "access-" + email,
            "id_token": "id-" + email,
            "account_id": "acc-" + email,
        }, fname)
        return fname

    def test_export_by_filename(self):
        fname = self._save_codex("a@example.com", "rt-aaa")
        r = self.client.post(
            "/api/codex/export-rt",
            json={"filenames": [fname]},
            headers=self.headers,
        )
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["count"], 1)
        self.assertEqual(body["text"], "a@example.com----rt-aaa\n")
        self.assertTrue(str(body["filename"]).startswith("codex-rt-"))
        rec = db.list_codex_accounts()[0]
        self.assertEqual(rec["exported_count"], 1)

    def test_export_by_account_ids(self):
        acc_id = self._insert_account("child@example.com", extra={"imported_team_child": True})
        self._save_codex("child@example.com", "rt-child")
        r = self.client.post(
            "/api/codex/export-rt",
            json={"account_ids": [acc_id, acc_id, 999, "bad"]},
            headers=self.headers,
        )
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertEqual(body["count"], 1)
        self.assertEqual(body["text"], "child@example.com----rt-child\n")
        reasons = {(item.get("id"), item.get("reason")) for item in body["skipped"]}
        self.assertIn((999, "账号不存在"), reasons)
        self.assertIn(("bad", "ID 非法"), reasons)

    def test_skip_missing_refresh_token(self):
        fname = self._save_codex("empty@example.com", "")
        r = self.client.post(
            "/api/codex/export-rt",
            json={"filenames": [fname]},
            headers=self.headers,
        )
        self.assertEqual(r.status_code, 409)
        body = r.get_json()
        self.assertFalse(body["ok"])
        self.assertEqual(body["error"], "没有可导出的 refresh_token")
        self.assertEqual(body["skipped"][0]["reason"], "凭证没有 refresh_token")

    def test_requires_selector(self):
        r = self.client.post("/api/codex/export-rt", json={}, headers=self.headers)
        self.assertEqual(r.status_code, 400)
        self.assertIn("filenames", r.get_json()["error"])

    def test_account_without_codex_credential(self):
        acc_id = self._insert_account("none@example.com")
        r = self.client.post(
            "/api/codex/export-rt",
            json={"account_ids": [acc_id]},
            headers=self.headers,
        )
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.get_json()["skipped"][0]["reason"], "没有 Codex 凭证")


if __name__ == "__main__":
    unittest.main()
