from __future__ import annotations

import os
import tempfile
import unittest
import base64
import json
from types import SimpleNamespace
from unittest import mock

from codex_manager.commands.best import best_account_rows, cmd_best
from codex_manager.errors import ManagerError
from codex_manager.paths import Paths, account_path, ensure_dirs, status_path
from codex_manager.storage import atomic_write_json, read_json


def make_auth(*, refresh_token: str, account_id: str, email: str, subject: str, access_exp: int) -> dict:
    def encode(value: dict) -> str:
        raw = json.dumps(value, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    return {
        "tokens": {
            "refresh_token": refresh_token,
            "id_token": {
                "email": email,
                "sub": subject,
                "https://api.openai.com/auth": {"chatgpt_account_id": account_id},
            },
            "access_token": f"{encode({'alg': 'none', 'typ': 'JWT'})}.{encode({'exp': access_exp})}.",
        }
    }


def limits(*, plan: str, remaining: float) -> dict:
    return {
        "fetched_at": "2026-08-25T12:00:00Z",
        "plan_type": plan,
        "snapshots": [
            {
                "limit_id": "codex",
                "limit_reached": remaining <= 0.0,
                "plan_type": plan,
                "secondary": {
                    "remaining_percent": remaining,
                    "used_percent": 100.0 - remaining,
                    "window_minutes": 30 * 24 * 60 if plan == "free" else 7 * 24 * 60,
                    "reset_after_seconds": 3600,
                },
            }
        ],
    }


class BestCommandTests(unittest.TestCase):
    def _paths(self, tmpdir: str) -> Paths:
        paths = Paths()
        ensure_dirs(paths)
        for name, plan, remaining in (("paid-full", "plus", 0.0), ("free-ready", "free", 75.0)):
            atomic_write_json(
                account_path(paths, name),
                make_auth(
                    refresh_token=f"{name}-refresh",
                    account_id=name,
                    email=f"{name}@example.com",
                    subject=name,
                    access_exp=4_102_444_800,
                ),
            )
            atomic_write_json(status_path(paths, name), {"state": "ok", "rate_limits": limits(plan=plan, remaining=remaining)})
        return paths

    def test_selects_free_when_all_paid_accounts_are_exhausted(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir, mock.patch.dict(
            os.environ,
            {"CODEX_HOME": f"{tmpdir}/codex", "CODEX_MANAGER_HOME": f"{tmpdir}/manager"},
            clear=False,
        ):
            paths = self._paths(tmpdir)
            with mock.patch("codex_manager.commands.best.activate") as activate_account:
                self.assertEqual(0, cmd_best(SimpleNamespace()))

            activate_account.assert_called_once()
            self.assertEqual("free-ready", activate_account.call_args.args[1])
            rows = best_account_rows(paths)
            self.assertTrue(rows[1].limits.startswith("monthly 75%"))

    def test_fails_after_printing_rows_when_every_cached_limit_is_exhausted(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir, mock.patch.dict(
            os.environ,
            {"CODEX_HOME": f"{tmpdir}/codex", "CODEX_MANAGER_HOME": f"{tmpdir}/manager"},
            clear=False,
        ):
            paths = self._paths(tmpdir)
            status = status_path(paths, "free-ready")
            payload = read_json(status)
            payload["rate_limits"] = limits(plan="free", remaining=0.0)
            atomic_write_json(status, payload)

            with self.assertRaisesRegex(ManagerError, "all cached account limits are exhausted"):
                cmd_best(SimpleNamespace())


if __name__ == "__main__":
    unittest.main()
