from __future__ import annotations

import curses
import os
import sys
import time
from typing import Any
from pathlib import Path

from ..auth import account_metadata, format_identity, read_auth, same_account_identity, should_promote_live_auth
from ..codex.limits import LimitFetchError, fetch_rate_limits
from ..config import ensure_config
from ..errors import ManagerError
from ..history import append_rate_limit_history, rename_history_account
from ..paths import Paths, account_path, ensure_dirs, list_accounts, sanitize_name, status_path
from ..storage import atomic_write_json, load_state, manager_lock, read_json, save_state, write_log
from ..time_utils import iso_now
from ..views import describe_account, print_accounts


# Metadata populated by auxiliary scanners must survive a normal account check.
_PRESERVED_STATUS_FIELDS = ("chrome_profile", "session_monitor_disabled")


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
    *,
    preserve_existing: bool = False,
    **extra,
) -> None:
    existing = _load_existing_status(paths, name)
    if preserve_existing:
        data = existing
    else:
        data = {
            field: existing[field]
            for field in _PRESERVED_STATUS_FIELDS
            if field in existing and field not in extra
        }
    data.update({
        "state": state,
        "message": message,
        "last_checked_at": iso_now(),
    })
    data.update(extra)
    atomic_write_json(status_path(paths, name), data)


def cmd_add(args) -> int:
    paths = Paths()
    if not getattr(args, "name", None) or not getattr(args, "auth_json", None):
        if sys.stdin.isatty() and sys.stdout.isatty():
            from ..textual_ui import run_textual_dashboard

            run_textual_dashboard(paths, initial_tab="accounts")
            return 0
        raise ManagerError("`codex-manager add` needs <name> and <auth.json> outside an interactive terminal")
    meta = add_account(paths, args.name, args.auth_json, force=args.force)
    print(f"added {sanitize_name(args.name)}: {meta.get('email') or 'unknown email'}")
    return 0


def add_account(paths: Paths, name: str, auth_json: str, force: bool = False) -> dict:
    name = sanitize_name(name)
    src = Path(auth_json).expanduser().resolve()
    auth = read_auth(src)
    with manager_lock(paths):
        dst = account_path(paths, name)
        if dst.exists() and not force:
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
    return account_metadata(auth)


def sync_active(paths: Paths) -> None:
    state = load_state(paths)
    active = state.get("active")
    if not active or not paths.codex_auth.exists():
        return
    active_path = account_path(paths, active)
    if not active_path.exists():
        write_status(
            paths,
            active,
            "warning",
            "active account file is missing; skipped sync",
            preserve_existing=True,
        )
        return
    try:
        auth = read_auth(paths.codex_auth)
        stored_auth = read_auth(active_path)
    except ManagerError as exc:
        write_status(
            paths,
            active,
            "warning",
            f"could not sync active auth: {exc}",
            preserve_existing=True,
        )
        return
    same_identity, reason = same_account_identity(stored_auth, auth)
    if not same_identity:
        message = f"skipped sync: {reason}"
        write_status(paths, active, "warning", message, preserve_existing=True)
        write_log(paths, f"skipped syncing active auth for {active}: {reason}")
        return
    should_promote, promotion_reason = should_promote_live_auth(stored_auth, auth)
    if not should_promote:
        write_status(paths, active, "ok", promotion_reason, preserve_existing=True)
        write_log(paths, f"left manager copy unchanged for {active}: {promotion_reason}")
        return
    atomic_write_json(active_path, auth)
    write_status(
        paths,
        active,
        "ok",
        f"synced from live auth: {promotion_reason}",
        preserve_existing=True,
    )
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
            write_status(paths, active, "warning", message, preserve_existing=True)
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
        write_status(
            paths,
            name,
            "ok",
            f"synced from live auth: {promotion_reason}",
            preserve_existing=True,
        )
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
        auth = read_auth(src)
        previous_status = _load_existing_status(paths, name)
        rate_limits, limits_error = _fetch_account_limits(paths, auth)
        if rate_limits is not None:
            append_rate_limit_history(paths, name, rate_limits)
        else:
            rate_limits = previous_status.get("rate_limits") if isinstance(previous_status.get("rate_limits"), dict) else None
        status_message = "active"
        if limits_error and rate_limits is not None:
            status_message = f"active; showing cached limits because refresh failed: {limits_error}"
        elif limits_error:
            status_message = f"active; {limits_error}"
        write_status(
            paths,
            name,
            "ok" if rate_limits is not None else "warning" if limits_error else "ok",
            status_message,
            rate_limits=rate_limits,
            limits_error=limits_error,
        )


def _load_existing_status(paths: Paths, name: str) -> dict[str, Any]:
    try:
        return read_json(status_path(paths, name))
    except ManagerError:
        return {}


def _fetch_account_limits(paths: Paths, auth: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    config = ensure_config(paths)
    try:
        return fetch_rate_limits(auth, proxy_url=config.get("proxy")), None
    except LimitFetchError as exc:
        return None, str(exc)


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


def rename_account(paths: Paths, old_name: str, new_name: str) -> str:
    old_name = sanitize_name(old_name)
    new_name = sanitize_name(new_name)
    if old_name == new_name:
        raise ManagerError("new name must be different")
    with manager_lock(paths):
        src = account_path(paths, old_name)
        dst = account_path(paths, new_name)
        if not src.exists():
            raise ManagerError(f"unknown account: {old_name}")
        if dst.exists():
            raise ManagerError(f"account already exists: {new_name}")
        os.replace(src, dst)
        old_status = status_path(paths, old_name)
        new_status = status_path(paths, new_name)
        if old_status.exists():
            os.replace(old_status, new_status)
        renamed_samples = rename_history_account(paths, old_name, new_name)
        state = load_state(paths)
        if state.get("active") == old_name:
            state["active"] = new_name
            save_state(paths, state)
        write_log(paths, f"renamed account {old_name} -> {new_name} (history samples: {renamed_samples})")
    return new_name


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
    if sys.stdin.isatty() and sys.stdout.isatty():
        from ..textual_ui import run_textual_dashboard

        run_textual_dashboard(paths, initial_tab="accounts")
        return 0
    return interactive_ls(paths)
