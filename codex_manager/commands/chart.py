from __future__ import annotations

from ..errors import ManagerError
from ..paths import Paths
from ..textual_ui import ChartDefaults, run_textual_dashboard


def cmd_chart(args) -> int:
    if args.hours is None and args.days is None:
        args.hours = 24
    if args.hours is not None and args.days is not None:
        raise ManagerError("pick either --hours or --days")
    run_textual_dashboard(
        Paths(),
        initial_tab="charts",
        chart=ChartDefaults(
            account=args.account,
            hours=args.hours,
            days=args.days,
            window_offset=args.window_offset or 0,
            timezone=args.timezone,
        ),
    )
    return 0
