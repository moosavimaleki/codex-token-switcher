from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

from codex_manager.history import (
    append_rate_limit_history,
    build_history_window,
    load_rate_limit_history,
    parse_timezone_offset,
    prune_rate_limit_history,
)
from codex_manager.paths import Paths, ensure_dirs


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


class HistoryTests(unittest.TestCase):
    def test_append_and_query_history_window(self) -> None:
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
                append_rate_limit_history(
                    paths,
                    "blue",
                    make_rate_limits(fetched_at="2026-05-23T10:00:00Z", primary=82.0, secondary=37.0),
                )
                append_rate_limit_history(
                    paths,
                    "blue",
                    make_rate_limits(fetched_at="2026-05-23T11:00:00Z", primary=75.0, secondary=35.0),
                )

                rows = load_rate_limit_history(paths, account="blue")
                self.assertEqual(2, len(rows))

                with mock.patch("codex_manager.history.utcnow") as mocked_utcnow:
                    import datetime as dt

                    mocked_utcnow.return_value = dt.datetime(2026, 5, 23, 12, 0, tzinfo=dt.timezone.utc)
                    window = build_history_window(
                        paths,
                        account="blue",
                        hours=6,
                        window_offset=0,
                        timezone="+03:30",
                    )
                self.assertEqual("blue", window.account)
                self.assertEqual("6h", window.window_label)
                self.assertEqual("0h", window.offset_label)
                self.assertEqual("UTC+03:30", window.timezone_label)
                self.assertEqual([82.0, 75.0], [point[1] for point in window.primary_points])
                self.assertEqual([37.0, 35.0], [point[1] for point in window.secondary_points])

    def test_query_history_window_with_lookback_offset(self) -> None:
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
                append_rate_limit_history(
                    paths,
                    "blue",
                    make_rate_limits(fetched_at="2026-05-23T05:00:00Z", primary=90.0, secondary=40.0),
                )
                append_rate_limit_history(
                    paths,
                    "blue",
                    make_rate_limits(fetched_at="2026-05-23T10:00:00Z", primary=82.0, secondary=37.0),
                )
                append_rate_limit_history(
                    paths,
                    "blue",
                    make_rate_limits(fetched_at="2026-05-23T11:00:00Z", primary=75.0, secondary=35.0),
                )
                with mock.patch("codex_manager.history.utcnow") as mocked_utcnow:
                    import datetime as dt

                    mocked_utcnow.return_value = dt.datetime(2026, 5, 23, 12, 0, tzinfo=dt.timezone.utc)
                    window = build_history_window(
                        paths,
                        account="blue",
                        hours=6,
                        window_offset=1,
                        timezone="UTC",
                    )
                self.assertEqual([90.0, 82.0, 75.0], [point[1] for point in window.primary_points])

    def test_prune_history_removes_old_samples(self) -> None:
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
                append_rate_limit_history(
                    paths,
                    "blue",
                    make_rate_limits(fetched_at="2026-01-01T10:00:00Z", primary=82.0, secondary=37.0),
                )
                append_rate_limit_history(
                    paths,
                    "blue",
                    make_rate_limits(fetched_at="2026-05-23T11:00:00Z", primary=75.0, secondary=35.0),
                )

                with mock.patch("codex_manager.history.utcnow") as mocked_utcnow:
                    import datetime as dt

                    mocked_utcnow.return_value = dt.datetime(2026, 5, 23, 12, 0, tzinfo=dt.timezone.utc)
                    prune_rate_limit_history(paths, retention_days=30)

                rows = load_rate_limit_history(paths, account="blue")
                self.assertEqual(1, len(rows))
                self.assertEqual("2026-05-23T11:00:00Z", rows[0]["recorded_at"])

    def test_parse_timezone_offset(self) -> None:
        offset = parse_timezone_offset("-07:00")
        self.assertEqual("-1 day, 17:00:00", str(offset.utcoffset(None)))


if __name__ == "__main__":
    unittest.main()
