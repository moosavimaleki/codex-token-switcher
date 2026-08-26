from __future__ import annotations

import argparse
import sys

from .commands import (
    cmd_add,
    cmd_best,
    cmd_chart,
    cmd_check,
    cmd_gateway,
    cmd_compact,
    cmd_config,
    cmd_doctor,
    cmd_ls,
    cmd_maintain,
    cmd_sessions,
    cmd_scheduler_apply,
)
from .errors import ManagerError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="codex-manager")
    sub = parser.add_subparsers(dest="cmd")

    add = sub.add_parser("add", help="import a healthy ChatGPT auth.json")
    add.add_argument("name", nargs="?")
    add.add_argument("auth_json", nargs="?")
    add.add_argument("--force", action="store_true")
    add.set_defaults(func=cmd_add)

    ls = sub.add_parser("ls", help="interactive account selector")
    ls.add_argument("--plain", action="store_true", help="print non-interactive list")
    ls.set_defaults(func=cmd_ls)

    best = sub.add_parser("best", help="activate the highest-ranked account with cached quota remaining")
    best.set_defaults(func=cmd_best)

    maintain = sub.add_parser("maintain", help="internal: sync active and refresh inactive accounts")
    maintain.add_argument("--quiet", action="store_true")
    maintain.set_defaults(func=cmd_maintain)

    check = sub.add_parser("check", help="check all accounts now and refresh when needed")
    check.add_argument("--force-refresh", action="store_true", help="refresh inactive accounts even if access token looks valid")
    check.add_argument("--refresh-active", action="store_true", help="also refresh the active live Codex account")
    check.add_argument("--quiet", action="store_true")
    check.set_defaults(func=cmd_check)

    sessions = sub.add_parser("sessions", help="monitor Chrome ChatGPT sessions and revoke excess Codex sessions")
    sessions.add_argument("--dry-run", action="store_true", help="report excess Codex sessions without revoking them")
    sessions.add_argument("--quiet", action="store_true")
    sessions.set_defaults(func=cmd_sessions)

    gateway = sub.add_parser("gateway", help="run the OpenAI-compatible Codex gateway")
    gateway.add_argument("--listen", help="listen address, for example 127.0.0.1:8787")
    gateway.add_argument("--api-key", help="local bearer key")
    gateway.set_defaults(func=cmd_gateway)

    chart = sub.add_parser("chart", help="open the Textual history chart")
    chart.add_argument("--account", help="account name")
    chart.add_argument("--hours", type=int, help="look back N recent hours")
    chart.add_argument("--days", type=int, help="look back N recent days")
    chart.add_argument(
        "--window-offset",
        type=int,
        default=0,
        help="shift the window back by N hours or days, matching the selected unit",
    )
    chart.add_argument(
        "--timezone",
        help="timezone offset for axis labels, for example local, UTC, +03:30, or -07:00",
    )
    chart.set_defaults(func=cmd_chart)

    compact = sub.add_parser("compact", help="compact a Codex session with a selected account")
    compact.add_argument("session_id", help="Codex thread/session id or rollout .jsonl path")
    compact.add_argument("--account", help="account name; skips the interactive picker")
    compact.add_argument("--codex-bin", help="codex executable path (default: newest found)")
    compact.add_argument("--timeout", type=float, default=900.0, help="seconds to wait for compaction")
    compact.set_defaults(func=cmd_compact)

    config = sub.add_parser(
        "config",
        help="interactive config wizard",
        usage="codex-manager config [show|set ...|reset]",
        description="Run without a subcommand to open the interactive config wizard.",
    )
    config.set_defaults(func=cmd_config)
    config_sub = config.add_subparsers(dest="config_cmd")
    config_show = config_sub.add_parser("show", help="show config")
    config_show.set_defaults(func=cmd_config)
    config_reset = config_sub.add_parser("reset", help="reset config to this release's defaults")
    config_reset.set_defaults(func=cmd_config)
    config_set = config_sub.add_parser("set", help="update config")
    config_set.add_argument("--proxy", help="HTTP/HTTPS proxy URL, or 'none' to disable")
    config_set.add_argument("--interval", help="maintenance interval, for example 30m, 6h, or 1d")
    config_set.add_argument("--monitor-interval", help="history/check interval, for example 5min")
    session_monitor_group = config_set.add_mutually_exclusive_group()
    session_monitor_group.add_argument("--session-monitor", dest="session_monitor", action="store_true", help="enable Chrome session monitoring")
    session_monitor_group.add_argument("--no-session-monitor", dest="session_monitor", action="store_false", help="disable Chrome session monitoring")
    config_set.set_defaults(session_monitor=None)
    config_set.add_argument("--session-monitor-interval", help="Chrome session monitor interval, for example 15min")
    config_set.add_argument("--chrome-root", help="Chrome profile directory, or 'none' for automatic detection")
    config_set.add_argument("--randomized-delay", help="systemd randomized delay, for example 0s or 10min")
    config_set.add_argument("--history-retention-days", type=int, help="how many days of limit history to keep")
    config_set.add_argument("--gateway-listen", help="gateway address, for example 127.0.0.1:8787")
    config_set.add_argument("--gateway-api-key", help="local gateway bearer key")
    config_set.add_argument("--apply-scheduler", action="store_true", help="rewrite and restart the installed timer")
    config_set.add_argument("--bin", help="codex-manager executable path for scheduler apply")
    config_set.set_defaults(func=cmd_config)

    scheduler = sub.add_parser("scheduler", help="install or update the maintenance scheduler")
    scheduler_sub = scheduler.add_subparsers(dest="scheduler_cmd", required=True)
    scheduler_apply = scheduler_sub.add_parser("apply", help="apply scheduler from config")
    scheduler_apply.add_argument("--bin", help="codex-manager executable path")
    scheduler_apply.add_argument("--quiet", action="store_true")
    scheduler_apply.set_defaults(func=cmd_scheduler_apply)

    doctor = sub.add_parser("doctor", help="show full health, scheduler, and status details")
    doctor.add_argument("--journal-lines", type=int, default=50)
    doctor.add_argument("--log-lines", type=int, default=50)
    doctor.set_defaults(func=cmd_doctor)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "cmd", None) is None:
        args.cmd = "ls"
        args.plain = False
        args.func = cmd_ls
    try:
        return args.func(args)
    except ManagerError as exc:
        print(f"codex-manager: {exc}", file=sys.stderr)
        return 1
