from __future__ import annotations

import datetime as dt
import unittest

from codex_manager.codex.limits import describe_rate_limit_windows, format_rate_limit_resets


class RateLimitResetFormattingTests(unittest.TestCase):
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
        self.assertTrue(lines[0].startswith("5h reset: tomorrow at "))
        self.assertTrue(lines[1].startswith("Weekly reset: Mon 2026-06-15 at "))

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
        self.assertTrue(windows[0]["reached"])
        self.assertTrue(str(windows[0]["reset_text"]).startswith("today at "))


if __name__ == "__main__":
    unittest.main()
