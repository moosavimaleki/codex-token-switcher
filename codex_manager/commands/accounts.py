from __future__ import annotations

import curses
import sys
import time
from pathlib import Path

from ..auth import account_metadata, format_identity, read_auth, same_account_identity, should_promote_live_auth
from ..errors import ManagerError
from ..paths import Paths, account_path, ensure_dirs, list_accounts, sanitize_name, status_path
from ..storage import atomic_write_json, load_state, manager_lock, save_state, write_log
from ..time_utils import iso_now
from ..views import describe_account, print_accounts


def atomic_copy_auth(src: Path, dst: Path) -> None:
    atomic_write_json(dst, read_auth(src))


def imported_live_auth_should_become_active(paths: Paths, active: str | None, auth: dict) -> bool:
    if active is None:
        return True
    active_path = account_path(paths, active)
    if not active_path.exists():
        return True
    try:
        stored_active_auth = read_auth(active_path)
    except ManagerError:
        return True
    same_identity, _reason = same_account_identity(stored_active_auth, auth)
    return not same_identity


def write_status(
    paths: Paths,
    name: str,
    state: str,
    message: str | None = None,
    **extra,
) -> None:
    data = {
        "state": state,
        "message": message,
        "last_checked_at": iso_now(),
    }
    data.update(extra)
    atomic_write_json(status_path(paths, name), data)


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
        live_auth_import = src == paths.codex_auth.resolve()
        if live_auth_import and imported_live_auth_should_become_active(paths, state.get("active"), auth):
            state["active"] = name
            state["last_activated_at"] = iso_now()
            write_log(paths, f"tracked live Codex auth as active account {name}")
        save_state(paths, state)
        write_status(paths, name, "ok", "active" if state.get("active") == name else "imported")
    meta = account_metadata(auth)
    print(f"added {name}: {meta.get('email') or 'unknown email'}")
    return 0


def sync_active(paths: Paths) -> None:
    state = load_state(paths)
    active = state.get("active")
    if not active or not paths.codex_auth.exists():
        return
    active_path = account_path(paths, active)
    if not active_path.exists():
        write_status(paths, active, "warning", "active account file is missing; skipped sync")
        return
    try:
        auth = read_auth(paths.codex_auth)
        stored_auth = read_auth(active_path)
    except ManagerError as exc:
        write_status(paths, active, "warning", f"could not sync active auth: {exc}")
        return
    same_identity, reason = same_account_identity(stored_auth, auth)
    if not same_identity:
        message = f"skipped sync: {reason}"
        write_status(paths, active, "warning", message)
        write_log(paths, f"skipped syncing active auth for {active}: {reason}")
        return
    should_promote, promotion_reason = should_promote_live_auth(stored_auth, auth)
    if not should_promote:
        write_status(paths, active, "ok", promotion_reason)
        write_log(paths, f"left manager copy unchanged for {active}: {promotion_reason}")
        return
    atomic_write_json(active_path, auth)
    write_status(paths, active, "ok", f"synced from live auth: {promotion_reason}")
    write_log(paths, f"synced live auth back into manager for {active}: {promotion_reason}")


def sync_live_auth_to_matching_account(paths: Paths) -> str | None:
    if not paths.codex_auth.exists():
        return None
    try:
        live_auth = read_auth(paths.codex_auth)
    except ManagerError:
        return None

    matches: list[tuple[str, dict]] = []
    for name in list_accounts(paths):
        try:
            stored_auth = read_auth(account_path(paths, name))
        except ManagerError:
            continue
        same_identity, _reason = same_account_identity(stored_auth, live_auth)
        if same_identity:
            matches.append((name, stored_auth))

    if not matches:
        active = load_state(paths).get("active")
        message = f"live Codex auth does not match any stored account: {format_identity(live_auth)}"
        if active:
            write_status(paths, active, "warning", message)
        write_log(paths, message)
        return None

    if len(matches) > 1:
        names = ", ".join(name for name, _auth in matches)
        write_log(paths, f"live Codex auth matched multiple accounts; skipped sync: {names}")
        return None

    name, stored_auth = matches[0]
    should_promote, promotion_reason = should_promote_live_auth(stored_auth, live_auth)
    if should_promote:
        atomic_write_json(account_path(paths, name), live_auth)
        write_status(paths, name, "ok", f"synced from live auth: {promotion_reason}")
        write_log(paths, f"synced live auth into account {name}: {promotion_reason}")

    state = load_state(paths)
    if state.get("active") != name:
        state["active"] = name
        state["last_activated_at"] = iso_now()
        save_state(paths, state)
        write_log(paths, f"tracked live Codex auth as active account {name}")
    return name


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


def delete_account(paths: Paths, name: str) -> None:
    name = sanitize_name(name)
    with manager_lock(paths):
        active = load_state(paths).get("active")
        if name == active:
            raise ManagerError("cannot delete the active account; activate another account first")
        path = account_path(paths, name)
        if not path.exists():
            raise ManagerError(f"unknown account: {name}")
        path.unlink()
        status_path(paths, name).unlink(missing_ok=True)
        write_log(paths, f"deleted account {name}")


def interactive_ls(paths: Paths) -> int:
    accounts = list_accounts(paths)
    if not accounts:
        print_accounts(paths)
        return 0
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print_accounts(paths)
        return 0

    def run(stdscr):
        nonlocal accounts
        curses.curs_set(0)
        selected = 0
        while True:
            active = load_state(paths).get("active")
            rows = [describe_account(paths, name, active) for name in accounts]
            stdscr.erase()
            stdscr.addstr(0, 0, "codex-manager ls - up/down select, Enter activate, d delete, r refresh view, q quit")
            stdscr.addstr(1, 0, f"active auth: {paths.codex_auth}")
            for idx, row in enumerate(rows):
                prefix = ">" if idx == selected else " "
                limits = row["limits"][:34]
                line = (
                    f"{prefix} {row['mark']} {row['name']:<18} {row['state']:<13} "
                    f"expires {row['expires']:<9} limits {limits:<34} {row['email']}"
                )
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
            elif key in (ord("d"),):
                name = accounts[selected]
                if name == active:
                    stdscr.addstr(len(accounts) + 5, 0, "cannot delete active account; activate another account first")
                    stdscr.refresh()
                    time.sleep(1.3)
                    continue
                stdscr.addstr(len(accounts) + 5, 0, f"delete {name}? y/N")
                stdscr.clrtoeol()
                stdscr.refresh()
                confirm = stdscr.getch()
                if confirm not in (ord("y"), ord("Y")):
                    continue
                try:
                    delete_account(paths, name)
                except ManagerError as exc:
                    stdscr.addstr(len(accounts) + 6, 0, str(exc)[: curses.COLS - 1])
                    stdscr.clrtoeol()
                    stdscr.refresh()
                    time.sleep(1.3)
                    continue
                accounts = list_accounts(paths)
                if not accounts:
                    stdscr.addstr(len(rows) + 6, 0, f"deleted {name}; no accounts left")
                    stdscr.clrtoeol()
                    stdscr.refresh()
                    time.sleep(1.0)
                    return 0
                selected = min(selected, len(accounts) - 1)
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
