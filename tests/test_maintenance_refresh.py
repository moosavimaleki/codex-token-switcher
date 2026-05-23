from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

from codex_manager.commands.accounts import sync_live_auth_to_matching_account
from codex_manager.commands.maintenance import run_account_checks
from codex_manager.paths import Paths, account_path, ensure_dirs
from codex_manager.storage import atomic_write_json, read_json, save_state

from test_accounts_sync import make_auth


class MaintenanceRefreshTests(unittest.TestCase):
    def test_check_does_not_refresh_active_account_by_default_even_when_forced(self) -> None:
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
                save_state(paths, {"active": "blue"})

                active_auth = make_auth(
                    refresh_token="blue-refresh",
                    account_id="acct-blue",
                    email="blue@example.com",
                    subject="user-blue",
                    access_exp=1,
                )
                atomic_write_json(account_path(paths, "blue"), active_auth)
                atomic_write_json(paths.codex_auth, active_auth)

                with mock.patch("codex_manager.commands.maintenance.fetch_rate_limits", return_value={}):
                    with mock.patch("codex_manager.commands.maintenance.refresh_auth") as refresh:
                        run_account_checks(paths, include_active=False, force_refresh=True)

                refresh.assert_not_called()

    def test_live_auth_promotes_matching_stored_account_before_refreshing(self) -> None:
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
                save_state(paths, {"active": "red"})

                red_auth = make_auth(
                    refresh_token="red-refresh",
                    account_id="acct-red",
                    email="red@example.com",
                    subject="user-red",
                    access_exp=4102444800,
                )
                old_blue_auth = make_auth(
                    refresh_token="old-blue-refresh",
                    account_id="acct-blue",
                    email="blue@example.com",
                    subject="user-blue",
                    access_exp=1,
                )
                live_blue_auth = make_auth(
                    refresh_token="new-blue-refresh",
                    account_id="acct-blue",
                    email="blue@example.com",
                    subject="user-blue",
                    access_exp=4102444800,
                )

                atomic_write_json(account_path(paths, "red"), red_auth)
                atomic_write_json(account_path(paths, "blue"), old_blue_auth)
                atomic_write_json(paths.codex_auth, live_blue_auth)

                with mock.patch("codex_manager.commands.maintenance.fetch_rate_limits", return_value={}):
                    with mock.patch("codex_manager.commands.maintenance.refresh_auth") as refresh:
                        run_account_checks(paths, include_active=False, force_refresh=False)

                refresh.assert_not_called()
                self.assertEqual("blue", read_json(paths.state_file)["active"])
                self.assertEqual(
                    "new-blue-refresh",
                    read_json(account_path(paths, "blue"))["tokens"]["refresh_token"],
                )

    def test_live_auth_with_unknown_email_does_not_change_active_account(self) -> None:
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
                save_state(paths, {"active": "blue"})

                blue_auth = make_auth(
                    refresh_token="blue-refresh",
                    account_id="acct-blue",
                    email="blue@example.com",
                    subject="user-blue",
                )
                unknown_live_auth = make_auth(
                    refresh_token="unknown-refresh",
                    account_id="acct-unknown",
                    email="unknown@example.com",
                    subject="user-unknown",
                )
                atomic_write_json(account_path(paths, "blue"), blue_auth)
                atomic_write_json(paths.codex_auth, unknown_live_auth)

                matched = sync_live_auth_to_matching_account(paths)

                self.assertIsNone(matched)
                self.assertEqual("blue", read_json(paths.state_file)["active"])
                self.assertEqual(
                    "blue-refresh",
                    read_json(account_path(paths, "blue"))["tokens"]["refresh_token"],
                )
                status = read_json(paths.status_dir / "blue.json")
                self.assertEqual("warning", status["state"])
                self.assertIn("unknown@example.com", status["message"])


if __name__ == "__main__":
    unittest.main()
