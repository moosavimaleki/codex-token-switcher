from __future__ import annotations

import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from codex_manager.commands.accounts import cmd_add, sync_active
from codex_manager.paths import Paths, account_path, ensure_dirs, status_path
from codex_manager.storage import atomic_write_json, read_json, save_state


def make_auth(
    *,
    refresh_token: str,
    account_id: str | None = None,
    email: str | None = None,
    subject: str | None = None,
) -> dict:
    id_token: dict[str, str] = {}
    if email:
        id_token["email"] = email
    if subject:
        id_token["sub"] = subject
    if account_id:
        id_token["https://api.openai.com/auth"] = {"chatgpt_account_id": account_id}
    return {
        "tokens": {
            "refresh_token": refresh_token,
            "id_token": id_token,
        }
    }


class SyncActiveTests(unittest.TestCase):
    def test_sync_active_updates_stored_account_when_identity_matches(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with mock.patch.dict(
                os.environ,
                {
                    "CODEX_HOME": f"{tmpdir}/codex",
                    "CODEX_MANAGER_HOME": f"{tmpdir}/manager",
                },
                clear=False,
            ):
                paths = Paths()
                ensure_dirs(paths)
                save_state(paths, {"active": "main"})

                stored_auth = make_auth(
                    refresh_token="stored-refresh",
                    account_id="acct-main",
                    email="main@example.com",
                    subject="user-main",
                )
                current_auth = make_auth(
                    refresh_token="current-refresh",
                    account_id="acct-main",
                    email="main@example.com",
                    subject="user-main",
                )

                atomic_write_json(account_path(paths, "main"), stored_auth)
                atomic_write_json(paths.codex_auth, current_auth)

                sync_active(paths)

                synced_auth = read_json(account_path(paths, "main"))
                self.assertEqual("current-refresh", synced_auth["tokens"]["refresh_token"])

    def test_sync_active_skips_when_auth_json_is_a_different_account(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with mock.patch.dict(
                os.environ,
                {
                    "CODEX_HOME": f"{tmpdir}/codex",
                    "CODEX_MANAGER_HOME": f"{tmpdir}/manager",
                },
                clear=False,
            ):
                paths = Paths()
                ensure_dirs(paths)
                save_state(paths, {"active": "main"})

                stored_auth = make_auth(
                    refresh_token="stored-refresh",
                    account_id="acct-main",
                    email="main@example.com",
                    subject="user-main",
                )
                foreign_auth = make_auth(
                    refresh_token="foreign-refresh",
                    account_id="acct-other",
                    email="other@example.com",
                    subject="user-other",
                )

                atomic_write_json(account_path(paths, "main"), stored_auth)
                atomic_write_json(paths.codex_auth, foreign_auth)

                sync_active(paths)

                unchanged_auth = read_json(account_path(paths, "main"))
                self.assertEqual("stored-refresh", unchanged_auth["tokens"]["refresh_token"])

                status = read_json(status_path(paths, "main"))
                self.assertEqual("warning", status["state"])
                self.assertIn("skipped sync", status["message"])


class AddAccountTests(unittest.TestCase):
    def test_adding_live_codex_auth_updates_active_when_external_login_changed_account(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with mock.patch.dict(
                os.environ,
                {
                    "CODEX_HOME": f"{tmpdir}/codex",
                    "CODEX_MANAGER_HOME": f"{tmpdir}/manager",
                },
                clear=False,
            ):
                paths = Paths()
                ensure_dirs(paths)
                save_state(paths, {"active": "main"})

                old_active_auth = make_auth(
                    refresh_token="stored-refresh",
                    account_id="acct-main",
                    email="main@example.com",
                    subject="user-main",
                )
                new_live_auth = make_auth(
                    refresh_token="new-refresh",
                    account_id="acct-other",
                    email="other@example.com",
                    subject="user-other",
                )

                atomic_write_json(account_path(paths, "main"), old_active_auth)
                atomic_write_json(paths.codex_auth, new_live_auth)

                result = cmd_add(SimpleNamespace(name="other", auth_json=str(paths.codex_auth), force=False))

                self.assertEqual(0, result)
                state = read_json(paths.state_file)
                self.assertEqual("other", state["active"])

                imported_auth = read_json(account_path(paths, "other"))
                self.assertEqual("new-refresh", imported_auth["tokens"]["refresh_token"])

                status = read_json(status_path(paths, "other"))
                self.assertEqual("ok", status["state"])
                self.assertEqual("active", status["message"])


if __name__ == "__main__":
    unittest.main()
