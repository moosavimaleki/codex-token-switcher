from __future__ import annotations

import curses
import contextlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

from .app_server import CodexAppServer
from .auth import account_metadata, read_auth, refresh_auth, should_refresh
from .config import cron_expression, ensure_config, redact_url, save_config
from .constants import DEFAULT_LAST_REFRESH_MAX_AGE, DEFAULT_REFRESH_MARGIN
from .errors import ManagerError
from .limits import LimitFetchError, fetch_rate_limits, format_rate_limits_summary
from .paths import Paths, account_path, ensure_dirs, list_accounts, sanitize_name, status_path
from .storage import atomic_write_json, load_state, manager_lock, read_json, save_state, tail_lines, write_log
from .system import run_command
from .terminal import bad, badge, dim, info, ok, section, style, warn
from .time_utils import human_delta, iso_now
from .views import colored_mode, describe_account, print_accounts

CRON_MARKER = "# codex-manager-maintain"


def atomic_copy_auth(src: Path, dst: Path) -> None:
    atomic_write_json(dst, read_auth(src))


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


def print_config(paths: Paths, config: dict) -> None:
    printable = dict(config)
    if printable.get("proxy"):
        printable["proxy"] = redact_url(printable.get("proxy"))
    print(f"{style('Config file', 'bold'):<22} {paths.config_file}")
    print(json.dumps(printable, indent=2, ensure_ascii=False))


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
            stdscr.addstr(0, 0, "codex-manager ls - ↑/↓ select, Enter activate, d delete, r refresh view, q quit")
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


def resolve_session_id(value: str) -> str:
    candidate = Path(value).expanduser()
    if not candidate.exists():
        return value
    if not candidate.is_file():
        raise ManagerError(f"session path is not a file: {candidate}")
    try:
        with candidate.open("r", encoding="utf-8") as handle:
            first_line = handle.readline()
        payload = json.loads(first_line)
        session_id = payload.get("payload", {}).get("id")
    except Exception as exc:
        raise ManagerError(f"could not read session id from {candidate}: {exc}") from exc
    if not isinstance(session_id, str) or not session_id:
        raise ManagerError(f"could not find session id in {candidate}")
    return session_id


def print_compact_account_picker(paths: Paths, accounts: list[str]) -> None:
    active = load_state(paths).get("active")
    print(f"{dim('Active auth')}  {paths.codex_auth}")
    print("")
    print(
        f"  {'#':<3} {style('Account', 'bold'):<18} {style('State', 'bold'):<16} "
        f"{style('Expires', 'bold'):<12} {style('Limits', 'bold'):<36} {style('Email', 'bold')}"
    )
    for idx, name in enumerate(accounts, 1):
        row = describe_account(paths, name, active)
        state_label = row["state"]
        state_text = {
            "active": ok("active"),
            "ok": ok("ok"),
            "refresh soon": warn("refresh soon"),
            "error": bad("error"),
        }.get(state_label, state_label)
        marker = ok("*") if name == active else dim(" ")
        limits = row["limits"][:35]
        print(
            f"{marker} {idx:<3} {row['name']:<18} {state_text:<25} "
            f"{row['expires']:<12} {limits:<36} {row['email']}"
        )


def select_account_for_compact(paths: Paths, requested: str | None) -> str:
    accounts = list_accounts(paths)
    if not accounts:
        print_accounts(paths)
        raise ManagerError("no accounts available")
    if requested:
        name = sanitize_name(requested)
        if name not in accounts:
            raise ManagerError(f"unknown account: {name}")
        return name
    print_compact_account_picker(paths, accounts)
    if not sys.stdin.isatty():
        raise ManagerError("run in a terminal or pass --account <name>")
    print("")
    while True:
        answer = input("Compact with account number/name (empty to cancel): ").strip()
        if not answer:
            raise ManagerError("cancelled")
        if answer.isdigit():
            idx = int(answer)
            if 1 <= idx <= len(accounts):
                return accounts[idx - 1]
        else:
            try:
                name = sanitize_name(answer)
            except ManagerError:
                name = ""
            if name in accounts:
                return name
        print(warn("Choose one of the listed account numbers or names."))


@contextlib.contextmanager
def checked_out_app_server_auth(paths: Paths, account: str):
    target_auth = paths.codex_app_auth
    selected_path = account_path(paths, account)
    config = ensure_config(paths)
    with manager_lock(paths):
        ensure_dirs(paths)
        sync_active(paths)
        if not selected_path.exists():
            raise ManagerError(f"unknown account: {account}")
        original_exists = target_auth.exists()
        original_auth = read_json(target_auth) if original_exists else None
        selected_auth = read_auth(selected_path)
        target_auth.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        atomic_write_json(target_auth, selected_auth)
        write_log(paths, f"checked out account {account} for app-server compaction")

        def refresh_selected_auth(_params: dict) -> dict:
            current_auth = read_auth(target_auth)
            refreshed_auth = refresh_auth(current_auth, proxy_url=config.get("proxy"))
            atomic_write_json(target_auth, refreshed_auth)
            atomic_write_json(selected_path, refreshed_auth)
            meta = account_metadata(refreshed_auth)
            access_token = refreshed_auth.get("tokens", {}).get("access_token")
            account_id = meta.get("account_id") or refreshed_auth.get("tokens", {}).get("account_id")
            if not isinstance(access_token, str) or not access_token:
                raise ManagerError("refresh did not return an access token")
            if not isinstance(account_id, str) or not account_id:
                raise ManagerError("refresh did not return a ChatGPT account id")
            write_log(paths, f"refreshed account {account} during app-server compaction")
            return {
                "accessToken": access_token,
                "chatgptAccountId": account_id,
                "chatgptPlanType": meta.get("plan"),
            }

        try:
            yield refresh_selected_auth
        finally:
            updated_selected_auth = None
            try:
                updated_selected_auth = read_auth(target_auth)
            except ManagerError as exc:
                write_log(paths, f"could not sync compact auth for {account}: {exc}")
            if updated_selected_auth is not None:
                atomic_write_json(selected_path, updated_selected_auth)

            active = load_state(paths).get("active")
            if original_exists and original_auth is not None:
                if target_auth == paths.codex_auth and active == account and updated_selected_auth is not None:
                    atomic_write_json(target_auth, updated_selected_auth)
                else:
                    atomic_write_json(target_auth, original_auth)
            elif target_auth == paths.codex_auth and active == account and updated_selected_auth is not None:
                atomic_write_json(target_auth, updated_selected_auth)
            else:
                target_auth.unlink(missing_ok=True)
            write_log(paths, f"restored app-server auth after compact with {account}")


def cmd_compact(args) -> int:
    paths = Paths()
    ensure_dirs(paths)
    config = ensure_config(paths)
    thread_id = resolve_session_id(args.session_id)
    account = select_account_for_compact(paths, args.account)
    print(f"compact session: {thread_id}")
    print(f"account: {account}")
    print(dim("Starting Codex app-server with the selected auth..."))
    with checked_out_app_server_auth(paths, account) as refresh_handler:
        with CodexAppServer(
            paths.codex_home,
            codex_bin=args.codex_bin,
            auth_refresh_handler=refresh_handler,
            proxy_url=config.get("proxy"),
        ) as server:
            server.initialize()
            server.compact_thread(thread_id)
            print(dim("Compaction started; waiting for completion..."))
            result = server.wait_for_compaction(thread_id, timeout=args.timeout)
    detail = f"turn {result['turn_id']}" if result.get("turn_id") else "compact turn"
    print(ok(f"compaction completed for {thread_id} with {account} ({detail})"))
    return 0


def run_account_checks(paths: Paths, include_active: bool, force_refresh: bool = False) -> dict:
    config = ensure_config(paths)
    results = []
    refreshed = 0
    failures = 0
    with manager_lock(paths):
        ensure_dirs(paths)
        active = load_state(paths).get("active")
        sync_active(paths)
        for name in list_accounts(paths):
            path = account_path(paths, name)
            try:
                auth = read_auth(path)
                needed, reason = should_refresh(auth)
                if force_refresh:
                    needed = True
                    reason = f"force refresh requested; {reason}"
                if name == active and not include_active and not force_refresh:
                    needed = False
                    reason = "active; synced, skipped refresh"
                refreshed_now = False
                if not needed:
                    checked_auth = auth
                    message = reason
                else:
                    refreshed_auth = refresh_auth(auth, proxy_url=config.get("proxy"))
                    atomic_write_json(path, refreshed_auth)
                    if name == active:
                        paths.codex_auth.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                        atomic_write_json(paths.codex_auth, refreshed_auth)
                    refreshed += 1
                    refreshed_now = True
                    checked_auth = refreshed_auth
                    message = f"refreshed: {reason}"
                    write_log(paths, f"refreshed account {name}: {reason}")
                limits_error = None
                rate_limits = None
                try:
                    rate_limits = fetch_rate_limits(checked_auth, proxy_url=config.get("proxy"))
                except LimitFetchError as exc:
                    if exc.status_code in {401, 403} and not refreshed_now:
                        refreshed_auth = refresh_auth(checked_auth, proxy_url=config.get("proxy"))
                        atomic_write_json(path, refreshed_auth)
                        if name == active:
                            paths.codex_auth.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                            atomic_write_json(paths.codex_auth, refreshed_auth)
                        refreshed += 1
                        checked_auth = refreshed_auth
                        message = f"refreshed after limits auth failure: {message}"
                        write_log(paths, f"refreshed account {name} after limits auth failure")
                        rate_limits = fetch_rate_limits(checked_auth, proxy_url=config.get("proxy"))
                    else:
                        limits_error = str(exc)
                state = "warning" if limits_error else "ok"
                status_message = message if not limits_error else f"{message}; {limits_error}"
                write_status(
                    paths,
                    name,
                    state,
                    status_message,
                    rate_limits=rate_limits,
                    limits_error=limits_error,
                )
                display_limits = format_rate_limits_summary(rate_limits) if rate_limits else limits_error
                results.append({
                    "name": name,
                    "state": state,
                    "message": status_message,
                    "limits": display_limits,
                })
            except ManagerError as exc:
                failures += 1
                write_status(paths, name, "needs_login", str(exc))
                write_log(paths, f"account {name} needs attention: {exc}")
                results.append({"name": name, "state": "needs_login", "message": str(exc)})
    return {"refreshed": refreshed, "failures": failures, "results": results}


def maintenance(paths: Paths) -> int:
    return int(run_account_checks(paths, include_active=False)["refreshed"])


def cmd_maintain(args) -> int:
    refreshed = maintenance(Paths())
    if not args.quiet:
        print(f"maintenance complete; refreshed {refreshed} account(s)")
    return 0


def cmd_check(args) -> int:
    paths = Paths()
    summary = run_account_checks(paths, include_active=True, force_refresh=args.force_refresh)
    if not args.quiet:
        for result in summary["results"]:
            state = str(result["state"])
            message = str(result["message"])
            limits = f"  {dim(result['limits'])}" if result.get("limits") else ""
            print(f"{badge(state, state):<20} {result['name']:<18} {message}{limits}")
        print(
            f"checked {len(summary['results'])} account(s); "
            f"refreshed {summary['refreshed']}; failures {summary['failures']}"
        )
    return 1 if summary["failures"] else 0


def prompt_config_value(label: str, current: str) -> str:
    try:
        return input(f"{label} [{current}]: ").strip()
    except EOFError:
        return ""


def cmd_config_interactive(paths: Paths) -> int:
    config = ensure_config(paths)
    section("Config")
    print_config(paths, config)
    print("")
    print(dim("Press Enter to keep a value. Use `none` to disable proxy."))

    updates = {}
    proxy = prompt_config_value("Proxy", redact_url(config.get("proxy")))
    interval = prompt_config_value("Maintenance interval", str(config["maintain_interval"]))
    randomized_delay = prompt_config_value("Randomized delay", str(config["randomized_delay"]))

    if proxy:
        updates["proxy"] = proxy
    if interval:
        updates["maintain_interval"] = interval
    if randomized_delay:
        updates["randomized_delay"] = randomized_delay

    if updates:
        config = save_config(paths, updates)
        print("")
        print_config(paths, config)
    else:
        print(dim("No config changes."))

    apply_answer = prompt_config_value("Apply scheduler now? Y/n", "Y").lower()
    if apply_answer in {"", "y", "yes"}:
        status = apply_scheduler(paths)
        print(f"scheduler applied: {status}")
    else:
        print(dim("Scheduler left unchanged."))
    return 0


def cmd_config(args) -> int:
    paths = Paths()
    if getattr(args, "config_cmd", None) is None:
        return cmd_config_interactive(paths)

    if args.config_cmd == "show":
        print_config(paths, ensure_config(paths))
        return 0

    updates = {}
    if args.proxy is not None:
        updates["proxy"] = args.proxy
    if args.interval is not None:
        updates["maintain_interval"] = args.interval
    if args.randomized_delay is not None:
        updates["randomized_delay"] = args.randomized_delay
    if not updates:
        raise ManagerError("provide --proxy, --interval, or --randomized-delay")

    config = save_config(paths, updates)
    print_config(paths, config)
    if args.apply_scheduler:
        status = apply_scheduler(paths, args.bin)
        print(f"scheduler applied: {status}")
    elif "maintain_interval" in updates or "randomized_delay" in updates:
        print(dim("Run `codex-manager scheduler apply` to update the installed timer."))
    return 0


def resolve_manager_bin() -> str:
    found = shutil.which("codex-manager")
    if found:
        return found
    candidate = Path(sys.argv[0]).expanduser()
    if candidate.exists():
        return str(candidate.resolve())
    return sys.argv[0]


def scheduler_paths(paths: Paths) -> tuple[Path, Path]:
    user_dir = paths.home / ".config/systemd/user"
    return user_dir / "codex-manager-maintain.service", user_dir / "codex-manager-maintain.timer"


def write_text_file(path: Path, content: str, mode: int = 0o644) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    os.chmod(path, mode)


def install_crontab(bin_path: str, interval: str) -> str:
    schedule = cron_expression(interval)
    line = f"{schedule} {shlex.quote(bin_path)} maintain --quiet >/dev/null 2>&1 {CRON_MARKER}"
    current = subprocess.run(
        ["crontab", "-l"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        check=False,
    )
    existing = current.stdout if current.returncode == 0 else ""
    lines = [
        line
        for line in existing.splitlines()
        if CRON_MARKER not in line and "codex-manager maintain --quiet" not in line
    ]
    lines.append(line)
    proc = subprocess.run(
        ["crontab", "-"],
        input="\n".join(lines) + "\n",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise ManagerError(f"failed to install crontab: {proc.stderr.strip()}")
    return f"crontab: {line}"


def apply_scheduler(paths: Paths, bin_path: str | None = None) -> str:
    config = ensure_config(paths)
    bin_path = bin_path or resolve_manager_bin()
    service_path, timer_path = scheduler_paths(paths)
    write_text_file(service_path, f"""[Unit]
Description=Codex Manager maintenance

[Service]
Type=oneshot
ExecStart={bin_path} maintain --quiet
""")
    write_text_file(timer_path, f"""[Unit]
Description=Run Codex Manager maintenance

[Timer]
OnBootSec=5min
OnUnitActiveSec={config["maintain_interval"]}
RandomizedDelaySec={config["randomized_delay"]}
Persistent=true
Unit=codex-manager-maintain.service

[Install]
WantedBy=timers.target
""")

    if shutil.which("systemctl"):
        code, _ = run_command(["systemctl", "--user", "show-environment"], timeout=5)
        if code == 0:
            for command in (
                ["systemctl", "--user", "daemon-reload"],
                ["systemctl", "--user", "enable", "--now", "codex-manager-maintain.timer"],
            ):
                command_code, output = run_command(command, timeout=10)
                if command_code != 0:
                    raise ManagerError(f"{' '.join(command)} failed: {output}")
            return "systemd user timer: codex-manager-maintain.timer"

    if shutil.which("crontab"):
        return install_crontab(bin_path, config["maintain_interval"])

    return "not installed; neither systemd user timers nor crontab are available"


def cmd_scheduler_apply(args) -> int:
    status = apply_scheduler(Paths(), args.bin)
    if args.quiet:
        print(status)
    else:
        print(f"scheduler applied: {status}")
    return 0


def print_command_output(label: str, command: list[str], timeout: int = 5) -> None:
    code, output = run_command(command, timeout=timeout)
    status = ok("ok") if code == 0 else warn(f"exit={code}") if code is not None else bad("unavailable")
    print(f"{style(label, 'bold')}: {dim(' '.join(command))} [{status}]")
    print(output if output else dim("(no output)"))


def cmd_doctor(args) -> int:
    paths = Paths()
    ensure_dirs(paths)
    config = ensure_config(paths)
    state = load_state(paths)
    active = state.get("active")
    accounts = list_accounts(paths)

    section("Summary")
    selected = ok(active) if active else warn("(none)")
    print(f"{style('Manager home', 'bold'):<22} {paths.manager_home}")
    print(f"{style('Active auth', 'bold'):<22} {paths.codex_auth}")
    print(f"{style('Config', 'bold'):<22} {paths.config_file}")
    print(f"{style('Proxy', 'bold'):<22} {redact_url(config.get('proxy'))}")
    print(f"{style('Job interval', 'bold'):<22} {config['maintain_interval']} (+ random {config['randomized_delay']})")
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
            limits = format_rate_limits_summary(s.get("rate_limits"))
            print(
                f"{style(name, 'bold'):<18} {badge(state_name, state_name):<24} "
                f"{s.get('message') or ''} {dim(limits)} {dim(s.get('last_checked_at') or '')}"
            )
        else:
            print(f"{style(name, 'bold'):<18} {warn('● no status')}          no status file yet")

    section("Files")
    print(f"{colored_mode(paths.codex_auth, '600')}  {paths.codex_auth}")
    print(f"{colored_mode(paths.config_file, '600')}  {paths.config_file}")
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
