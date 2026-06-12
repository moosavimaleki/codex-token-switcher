from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

from codex_manager.commands.accounts import rename_account
from codex_manager.history import append_rate_limit_history, load_rate_limit_history
from codex_manager.paths import Paths, account_path, ensure_dirs, status_path
from codex_manager.storage import atomic_write_json, read_json, save_state

from test_accounts_sync import make_auth


def make_rate_limits(*, fetched_at: str, primary: float, secondary: float) -> dict:
    return {
        "fetched_at": fetched_at,
        "plan_type": "plus",
        "snapshots": [
            {
                "limit_id": "codex",
                "primary": {"remaining_percent": primary, "used_percent": 100.0 - primary, "window_minutes": 300},
                "secondary": {"remaining_percent": secondary, "used_percent": 100.0 - secondary, "window_minutes": 10080},
            }
        ],
    }


class RenameAccountTests(unittest.TestCase):
    def test_rename_account_moves_status_history_and_active_state(self) -> None:
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
                auth = make_auth(
                    refresh_token="blue-refresh",
                    account_id="acct-blue",
                    email="blue@example.com",
                    subject="user-blue",
                )
                atomic_write_json(account_path(paths, "blue"), auth)
                atomic_write_json(status_path(paths, "blue"), {"state": "ok", "message": "ready"})
                append_rate_limit_history(
                    paths,
                    "blue",
                    make_rate_limits(fetched_at="2026-06-12T09:00:00Z", primary=50.0, secondary=75.0),
                )

                renamed = rename_account(paths, "blue", "violet")

                self.assertEqual("violet", renamed)
                self.assertTrue(account_path(paths, "violet").exists())
                self.assertFalse(account_path(paths, "blue").exists())
                self.assertTrue(status_path(paths, "violet").exists())
                self.assertFalse(status_path(paths, "blue").exists())
                self.assertEqual("violet", read_json(paths.state_file)["active"])
                history_rows = load_rate_limit_history(paths, account="violet")
                self.assertEqual(1, len(history_rows))
                self.assertEqual("violet", history_rows[0]["account"])


if __name__ == "__main__":
    unittest.main()
