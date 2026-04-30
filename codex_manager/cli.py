from __future__ import annotations

import argparse
import sys

from .commands import cmd_add, cmd_doctor, cmd_ls, cmd_maintain
from .errors import ManagerError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="codex-manager")
    sub = parser.add_subparsers(dest="cmd", required=True)

    add = sub.add_parser("add", help="import a healthy ChatGPT auth.json")
    add.add_argument("name")
    add.add_argument("auth_json")
    add.add_argument("--force", action="store_true")
    add.set_defaults(func=cmd_add)

    ls = sub.add_parser("ls", help="interactive account selector")
    ls.add_argument("--plain", action="store_true", help="print non-interactive list")
    ls.set_defaults(func=cmd_ls)

    maintain = sub.add_parser("maintain", help="internal: sync active and refresh inactive accounts")
    maintain.add_argument("--quiet", action="store_true")
    maintain.set_defaults(func=cmd_maintain)

    doctor = sub.add_parser("doctor", help="show full health, scheduler, and status details")
    doctor.add_argument("--journal-lines", type=int, default=50)
    doctor.add_argument("--log-lines", type=int, default=50)
    doctor.set_defaults(func=cmd_doctor)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except ManagerError as exc:
        print(f"codex-manager: {exc}", file=sys.stderr)
        return 1
