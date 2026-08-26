from __future__ import annotations

import datetime as dt
import unittest

from codex_manager.codex.limits import (
    describe_rate_limit_windows,
    format_rate_limit_resets,
    format_rate_limits_summary,
    normalize_rate_limits,
)


class RateLimitResetFormattingTests(unittest.TestCase):
    def test_normalize_preserves_a_single_primary_window(self) -> None:
        rate_limits = normalize_rate_limits(
            {
                "rate_limit": {
                    "allowed": True,
                    "primary_window": {"used_percent": 36.0, "limit_window_seconds": 720 * 3600},
                }
            }
        )
        snapshot = rate_limits["snapshots"][0]
        self.assertEqual(64.0, snapshot["primary"]["remaining_percent"])
        self.assertIsNone(snapshot["secondary"])

    def test_format_rate_limit_resets_uses_tomorrow_and_absolute_day_labels(self) -> None:
        now = dt.datetime(2026, 6, 12, 9, 0, tzinfo=dt.timezone.utc)
        rate_limits = {
            "fetched_at": "2026-06-12T09:00:00Z",
            "snapshots": [
                {
                    "limit_id": "codex",
                    "primary": {"remaining_percent": 30.0, "window_minutes": 300, "reset_after_seconds": 25 * 3600},
                    "secondary": {"remaining_percent": 80.0, "window_minutes": 7 * 24 * 60, "reset_after_seconds": 3 * 24 * 3600},
                }
            ],
        }

        lines = format_rate_limit_resets(rate_limits, now=now)

        self.assertEqual(2, len(lines))
        self.assertTrue(lines[0].startswith("5h reset: 1d 1h left |"))
        self.assertTrue(lines[1].startswith("Weekly reset: 3d 0h left | Mon 2026-06-15 at "))

    def test_describe_rate_limit_windows_marks_reached_status(self) -> None:
        now = dt.datetime(2026, 6, 12, 9, 0, tzinfo=dt.timezone.utc)
        rate_limits = {
            "fetched_at": "2026-06-12T09:00:00Z",
            "snapshots": [
                {
                    "limit_id": "codex",
                    "limit_reached": True,
                    "primary": {"remaining_percent": 0.0, "used_percent": 100.0, "window_minutes": 300, "reset_after_seconds": 3600},
                    "secondary": {"remaining_percent": 40.0, "used_percent": 60.0, "window_minutes": 7 * 24 * 60, "reset_after_seconds": 7200},
                }
            ],
        }

        windows = describe_rate_limit_windows(rate_limits, now=now)

        self.assertEqual(2, len(windows))
        self.assertEqual("5h", windows[0]["key"])
        self.assertTrue(windows[0]["reached"])
        self.assertEqual("weekly", windows[1]["key"])
        self.assertFalse(windows[1]["reached"])
        self.assertTrue(str(windows[1]["reset_text"]).startswith("today at "))
        self.assertEqual("2h 0m", windows[1]["reset_in_text"])

    def test_two_live_windows_are_displayed_without_fixed_assumptions(self) -> None:
        rate_limits = {
            "fetched_at": "2026-06-12T09:00:00Z",
            "snapshots": [
                {
                    "limit_id": "codex",
                    "primary": {"remaining_percent": 35.0, "window_minutes": 300},
                    "secondary": {"remaining_percent": 82.0, "window_minutes": 7 * 24 * 60},
                }
            ],
        }

        self.assertTrue(
            format_rate_limits_summary(rate_limits, compact=True).startswith("5h 35%; weekly 82%")
        )

    def test_free_monthly_budget_is_not_labeled_weekly(self) -> None:
        now = dt.datetime(2026, 6, 12, 9, 0, tzinfo=dt.timezone.utc)
        rate_limits = {
            "fetched_at": "2026-06-12T09:00:00Z",
            "plan_type": "free",
            "snapshots": [
                {
                    "limit_id": "codex",
                    "plan_type": "free",
                    "secondary": {
                        "remaining_percent": 72.0,
                        "used_percent": 28.0,
                        "window_minutes": 30 * 24 * 60,
                        "reset_after_seconds": 23 * 24 * 3600,
                    },
                }
            ],
        }

        lines = format_rate_limit_resets(rate_limits, now=now)
        windows = describe_rate_limit_windows(rate_limits, now=now)

        self.assertTrue(lines[0].startswith("Monthly reset: 23d 0h left |"))
        self.assertEqual("monthly", windows[0]["key"])
        self.assertEqual("monthly", windows[0]["label"])
        self.assertTrue(format_rate_limits_summary(rate_limits, compact=True).startswith("monthly 72%"))


if __name__ == "__main__":
    unittest.main()
