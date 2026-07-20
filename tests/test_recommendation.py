from __future__ import annotations

import datetime as dt
import os
import tempfile
import unittest
from unittest import mock

from codex_manager.paths import Paths, ensure_dirs, status_path
from codex_manager.recommendation import account_recommendations
from codex_manager.storage import atomic_write_json


NOW = dt.datetime(2026, 5, 28, 12, 0, tzinfo=dt.timezone.utc)


def make_rate_limits(
    *,
    primary: float,
    weekly: float,
    primary_reset_seconds: int = 4 * 3600,
    weekly_reset_seconds: int = 4 * 24 * 3600,
) -> dict:
    return {
        "fetched_at": NOW.isoformat().replace("+00:00", "Z"),
        "plan_type": "plus",
        "snapshots": [
            {
                "limit_id": "codex",
                "allowed": True,
                "limit_reached": False,
                "primary": {
                    "remaining_percent": primary,
                    "used_percent": 100.0 - primary,
                    "window_minutes": 300,
                    "reset_after_seconds": primary_reset_seconds,
                },
                "secondary": {
                    "remaining_percent": weekly,
                    "used_percent": 100.0 - weekly,
                    "window_minutes": 10080,
                    "reset_after_seconds": weekly_reset_seconds,
                },
            }
        ],
    }


class RecommendationTests(unittest.TestCase):
    def test_weekly_pacing_beats_larger_balance(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with mock.patch.dict(
                os.environ,
                {"CODEX_MANAGER_HOME": f"{tmpdir}/manager", "CODEX_HOME": f"{tmpdir}/codex"},
                clear=False,
            ):
                paths = Paths()
                ensure_dirs(paths)
                atomic_write_json(status_path(paths, "steady"), {"state": "ok", "rate_limits": make_rate_limits(primary=35, weekly=95)})
                atomic_write_json(status_path(paths, "burned"), {"state": "ok", "rate_limits": make_rate_limits(primary=95, weekly=50)})

                recs = account_recommendations(paths, ["steady", "burned"], now=NOW)

                self.assertTrue(recs["steady"].is_best)
                self.assertEqual("BEST", recs["steady"].label)
                self.assertEqual("SAVE", recs["burned"].label)

    def test_account_behind_weekly_pace_is_protected(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with mock.patch.dict(
                os.environ,
                {"CODEX_MANAGER_HOME": f"{tmpdir}/manager", "CODEX_HOME": f"{tmpdir}/codex"},
                clear=False,
            ):
                paths = Paths()
                ensure_dirs(paths)
                atomic_write_json(status_path(paths, "low-weekly"), {"state": "ok", "rate_limits": make_rate_limits(primary=99, weekly=20)})

                recs = account_recommendations(paths, ["low-weekly"], now=NOW)

                self.assertEqual("RISK", recs["low-weekly"].label)
                self.assertFalse(recs["low-weekly"].recommendable)
                self.assertIn("protect weekly pace", recs["low-weekly"].reason)

    def test_nearer_weekly_reset_wins_when_quota_health_matches(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with mock.patch.dict(
                os.environ,
                {"CODEX_MANAGER_HOME": f"{tmpdir}/manager", "CODEX_HOME": f"{tmpdir}/codex"},
                clear=False,
            ):
                paths = Paths()
                ensure_dirs(paths)
                atomic_write_json(
                    status_path(paths, "near-reset"),
                    {
                        "state": "ok",
                        "rate_limits": make_rate_limits(primary=5, weekly=95, weekly_reset_seconds=10 * 60),
                    },
                )
                atomic_write_json(
                    status_path(paths, "later-reset"),
                    {"state": "ok", "rate_limits": make_rate_limits(primary=95, weekly=95, weekly_reset_seconds=4 * 24 * 3600)},
                )

                recs = account_recommendations(paths, ["near-reset", "later-reset"], now=NOW)

                self.assertTrue(recs["near-reset"].recommendable)
                self.assertEqual("BEST", recs["near-reset"].label)


if __name__ == "__main__":
    unittest.main()
