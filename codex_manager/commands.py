from __future__ import annotations

import curses
import os
import shutil
import sys
import time
from pathlib import Path

from .auth import account_metadata, read_auth, refresh_auth, should_refresh
from .constants import DEFAULT_LAST_REFRESH_MAX_AGE, DEFAULT_REFRESH_MARGIN
from .errors import ManagerError
from .paths import Paths, account_path, ensure_dirs, list_accounts, sanitize_name, status_path
from .storage import atomic_write_json, load_state, manager_lock, read_json, save_state, tail_lines, write_log
from .system import run_command
from .terminal import bad, badge, dim, info, ok, section, style, warn
from .time_utils import human_delta, iso_now
from .views import colored_mode, describe_account, print_accounts


def atomic_copy_auth(src: Path, dst: Path) -> None:
    atomic_write_json(dst, read_auth(src))


def write_status(paths: Paths, name: str, state: str, message: str | None = None) -> None:
    atomic_write_json(status_path(paths, name), {
        "state": state,
        "message": message,
        "last_checked_at": iso_now(),
    })


def cmd_add(args) -> int:
    paths = Paths()
    name = sanitize_name(args.name)
    src = Path(args.auth_json).expanduser().resolve()
    auth = read_auth(src)
    with manager_lock(paths):
        dst = account_path(paths, name)
        if dst.exists() and not args.force:
            raise ManagerError(f"account already exists: {name} (use --force to overwrite)")
        atomic_write_json(dst, auth)
        state = load_state(paths)
        if state.get("active") is None and src == paths.codex_auth.resolve():
            state["active"] = name
            state["last_activated_at"] = iso_now()
        save_state(paths, state)
        write_status(paths, name, "ok", "imported")
    meta = account_metadata(auth)
    print(f"added {name}: {meta.get('email') or 'unknown email'}")
    return 0


def sync_active(paths: Paths) -> None:
    state = load_state(paths)
    active = state.get("active")
    if not active or not paths.codex_auth.exists():
        return
    try:
        auth = read_auth(paths.codex_auth)
    except ManagerError as exc:
        write_status(paths, active, "warning", f"could not sync active auth: {exc}")
        return
    atomic_write_json(account_path(paths, active), auth)


def activate(paths: Paths, name: str) -> None:
    name = sanitize_name(name)
    src = account_path(paths, name)
    if not src.exists():
        raise ManagerError(f"unknown account: {name}")
    with manager_lock(paths):
        sync_active(paths)
        paths.codex_auth.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        atomic_copy_auth(src, paths.codex_auth)
        state = load_state(paths)
        state["active"] = name
        state["last_activated_at"] = iso_now()
        save_state(paths, state)
        write_status(paths, name, "ok", "active")


def interactive_ls(paths: Paths) -> int:
    accounts = list_accounts(paths)
    if not accounts:
        print_accounts(paths)
        return 0
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print_accounts(paths)
        return 0

    def run(stdscr):
        curses.curs_set(0)
        selected = 0
        while True:
            active = load_state(paths).get("active")
            rows = [describe_account(paths, name, active) for name in accounts]
            stdscr.erase()
            stdscr.addstr(0, 0, "codex-manager ls - ↑/↓ select, Enter activate, r refresh view, q quit")
            stdscr.addstr(1, 0, f"active auth: {paths.codex_auth}")
            for idx, row in enumerate(rows):
                prefix = ">" if idx == selected else " "
                line = f"{prefix} {row['mark']} {row['name']:<18} {row['state']:<13} expires {row['expires']:<9} {row['email']}"
                attr = curses.A_REVERSE if idx == selected else curses.A_NORMAL
                stdscr.addstr(idx + 3, 0, line[: curses.COLS - 1], attr)
                if idx == selected:
                    stdscr.addstr(idx + 4, 4, row["reason"][: curses.COLS - 5])
            stdscr.refresh()
            key = stdscr.getch()
            if key in (ord("q"), 27):
                return 0
            if key in (curses.KEY_UP, ord("k")):
                selected = max(0, selected - 1)
            elif key in (curses.KEY_DOWN, ord("j")):
                selected = min(len(accounts) - 1, selected + 1)
            elif key in (ord("r"),):
                continue
            elif key in (curses.KEY_ENTER, 10, 13):
                activate(paths, accounts[selected])
                stdscr.addstr(len(accounts) + 5, 0, f"activated {accounts[selected]}. Start/restart Codex to use it.")
                stdscr.refresh()
                time.sleep(1.0)
                return 0
        return 0

    return curses.wrapper(run)


def cmd_ls(args) -> int:
    paths = Paths()
    ensure_dirs(paths)
    if args.plain:
        print_accounts(paths)
        return 0
    return interactive_ls(paths)


def maintenance(paths: Paths) -> int:
    refreshed = 0
    with manager_lock(paths):
        ensure_dirs(paths)
        active = load_state(paths).get("active")
        sync_active(paths)
        for name in list_accounts(paths):
            if name == active:
                write_status(paths, name, "ok", "active; synced, skipped refresh")
                continue
            path = account_path(paths, name)
            try:
                auth = read_auth(path)
                needed, reason = should_refresh(auth)
                if not needed:
                    write_status(paths, name, "ok", reason)
                    continue
                atomic_write_json(path, refresh_auth(auth))
                refreshed += 1
                write_status(paths, name, "ok", f"refreshed: {reason}")
                write_log(paths, f"refreshed inactive account {name}: {reason}")
            except ManagerError as exc:
                write_status(paths, name, "needs_login", str(exc))
                write_log(paths, f"account {name} needs attention: {exc}")
    return refreshed


def cmd_maintain(args) -> int:
    refreshed = maintenance(Paths())
    if not args.quiet:
        print(f"maintenance complete; refreshed {refreshed} inactive account(s)")
    return 0


def print_command_output(label: str, command: list[str], timeout: int = 5) -> None:
    code, output = run_command(command, timeout=timeout)
    status = ok("ok") if code == 0 else warn(f"exit={code}") if code is not None else bad("unavailable")
    print(f"{style(label, 'bold')}: {dim(' '.join(command))} [{status}]")
    print(output if output else dim("(no output)"))


def cmd_doctor(args) -> int:
    paths = Paths()
    ensure_dirs(paths)
    state = load_state(paths)
    active = state.get("active")
    accounts = list_accounts(paths)

    section("Summary")
    selected = ok(active) if active else warn("(none)")
    print(f"{style('Manager home', 'bold'):<22} {paths.manager_home}")
    print(f"{style('Active auth', 'bold'):<22} {paths.codex_auth}")
    print(f"{style('Selected account', 'bold'):<22} {selected}")
    print(f"{style('Accounts', 'bold'):<22} {len(accounts)}")
    print(f"{style('Refresh policy', 'bold'):<22} inactive tokens refresh when access token ≤ {warn(human_delta(DEFAULT_REFRESH_MARGIN))} or last_refresh ≥ {warn(human_delta(DEFAULT_LAST_REFRESH_MAX_AGE))}")
    if active is None and accounts:
        print(f"{warn('Action needed')}       run {info('codex-manager ls')} and press Enter on the active account")

    section("Accounts")
    print_accounts(paths)

    section("Account Status")
    if not accounts:
        print(dim("No account status files yet."))
    for name in accounts:
        sp = status_path(paths, name)
        if sp.exists():
            s = read_json(sp)
            state_name = str(s.get("state") or "unknown")
            print(f"{style(name, 'bold'):<18} {badge(state_name, state_name):<24} {s.get('message') or ''} {dim(s.get('last_checked_at') or '')}")
        else:
            print(f"{style(name, 'bold'):<18} {warn('● no status')}          no status file yet")

    section("Files")
    print(f"{colored_mode(paths.codex_auth, '600')}  {paths.codex_auth}")
    print(f"{colored_mode(paths.state_file, '600')}  {paths.state_file}")
    print(f"{colored_mode(paths.lock_file, '600')}  {paths.lock_file}")
    print(f"{colored_mode(paths.log_file, '600')}  {paths.log_file}")
    for name in accounts:
        path = account_path(paths, name)
        print(f"{colored_mode(path, '600')}  {path}")

    section("Scheduler")
    service_path = paths.home / ".config/systemd/user/codex-manager-maintain.service"
    timer_path = paths.home / ".config/systemd/user/codex-manager-maintain.timer"
    print(f"{style('systemd service', 'bold'):<22} {colored_mode(service_path)}  {service_path}")
    print(f"{style('systemd timer', 'bold'):<22} {colored_mode(timer_path)}  {timer_path}")
    if shutil.which("systemctl"):
        print_command_output("timer status", ["systemctl", "--user", "status", "codex-manager-maintain.timer", "--no-pager"], timeout=8)
        print_command_output("timer schedule", ["systemctl", "--user", "list-timers", "codex-manager-maintain.timer", "--no-pager"], timeout=8)
        print_command_output("service status", ["systemctl", "--user", "status", "codex-manager-maintain.service", "--no-pager"], timeout=8)
        print_command_output("service journal", ["journalctl", "--user", "-u", "codex-manager-maintain.service", "-n", str(args.journal_lines), "--no-pager"], timeout=8)
    else:
        print(warn("systemctl not found"))

    section("Crontab Fallback")
    if shutil.which("crontab"):
        print_command_output("crontab", ["sh", "-lc", "crontab -l 2>/dev/null | grep codex-manager || true"])
    else:
        print(warn("crontab not found"))

    section("Manager Log")
    lines = tail_lines(paths.log_file, args.log_lines)
    if lines:
        for line in lines:
            print(dim(line[:32]) + line[32:] if len(line) > 32 else line)
    else:
        print(dim("(no manager log entries yet; normal if no refresh/error happened)"))
    return 0
