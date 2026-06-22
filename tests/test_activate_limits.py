from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

from codex_manager.codex.limits import LimitFetchError
from codex_manager.commands.accounts import activate
from codex_manager.paths import Paths, account_path, ensure_dirs, status_path
from codex_manager.storage import atomic_write_json, read_json, save_state

from test_accounts_sync import make_auth


class ActivateLimitTests(unittest.TestCase):
    def test_switching_accounts_preserves_previous_account_limits(self) -> None:
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
                save_state(paths, {"active": "one"})
                one_auth = make_auth(
                    refresh_token="one-refresh",
                    account_id="acct-one",
                    email="one@example.com",
                    subject="user-one",
                    access_exp=4102444800,
                )
                two_auth = make_auth(
                    refresh_token="two-refresh",
                    account_id="acct-two",
                    email="two@example.com",
                    subject="user-two",
                    access_exp=4102444800,
                )
                one_limits = {
                    "fetched_at": "2026-06-15T08:00:00Z",
                    "snapshots": [
                        {
                            "limit_id": "codex",
                            "primary": {"remaining_percent": 45.0},
                            "secondary": {"remaining_percent": 70.0},
                        }
                    ],
                }
                two_limits = {
                    "fetched_at": "2026-06-15T09:00:00Z",
                    "snapshots": [
                        {
                            "limit_id": "codex",
                            "primary": {"remaining_percent": 80.0},
                            "secondary": {"remaining_percent": 90.0},
                        }
                    ],
                }
                atomic_write_json(account_path(paths, "one"), one_auth)
                atomic_write_json(account_path(paths, "two"), two_auth)
                atomic_write_json(paths.codex_auth, one_auth)
                atomic_write_json(
                    status_path(paths, "one"),
                    {"state": "ok", "message": "checked", "rate_limits": one_limits},
                )

                with mock.patch(
                    "codex_manager.commands.accounts.fetch_rate_limits",
                    return_value=two_limits,
                ):
                    activate(paths, "two")

                previous_status = read_json(status_path(paths, "one"))
                self.assertEqual(one_limits, previous_status["rate_limits"])
                self.assertEqual("ok", previous_status["state"])

    def test_activate_preserves_cached_limits_when_refresh_fails(self) -> None:
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
                save_state(paths, {"active": "old"})
                auth = make_auth(
                    refresh_token="blue-refresh",
                    account_id="acct-blue",
                    email="blue@example.com",
                    subject="user-blue",
                    access_exp=4102444800,
                )
                cached_limits = {
                    "fetched_at": "2026-06-12T09:00:00Z",
                    "snapshots": [
                        {
                            "limit_id": "codex",
                            "primary": {"remaining_percent": 55.0, "window_minutes": 300, "reset_after_seconds": 3600},
                            "secondary": {
                                "remaining_percent": 88.0,
                                "window_minutes": 7 * 24 * 60,
                                "reset_after_seconds": 24 * 3600,
                            },
                        }
                    ],
                }
                atomic_write_json(account_path(paths, "blue"), auth)
                atomic_write_json(status_path(paths, "blue"), {"state": "ok", "message": "cached", "rate_limits": cached_limits})

                with mock.patch(
                    "codex_manager.commands.accounts.fetch_rate_limits",
                    side_effect=LimitFetchError("limits fetch failed: boom"),
                ):
                    activate(paths, "blue")

                status = read_json(status_path(paths, "blue"))
                self.assertEqual(cached_limits, status["rate_limits"])
                self.assertIn("showing cached limits", status["message"])

    def test_activate_writes_fresh_limits_when_fetch_succeeds(self) -> None:
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
                auth = make_auth(
                    refresh_token="blue-refresh",
                    account_id="acct-blue",
                    email="blue@example.com",
                    subject="user-blue",
                    access_exp=4102444800,
                )
                fresh_limits = {
                    "fetched_at": "2026-06-12T10:00:00Z",
                    "snapshots": [{"limit_id": "codex", "primary": {"remaining_percent": 75.0}, "secondary": {"remaining_percent": 90.0}}],
                }
                atomic_write_json(account_path(paths, "blue"), auth)

                with mock.patch("codex_manager.commands.accounts.fetch_rate_limits", return_value=fresh_limits):
                    activate(paths, "blue")

                status = read_json(status_path(paths, "blue"))
                self.assertEqual(fresh_limits, status["rate_limits"])
                self.assertEqual("active", status["message"])


if __name__ == "__main__":
    unittest.main()
