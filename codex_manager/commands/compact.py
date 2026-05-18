from __future__ import annotations

import contextlib
import json
import sys
from pathlib import Path

from .accounts import sync_active
from ..auth import account_metadata, read_auth, refresh_auth
from ..codex.app_server import CodexAppServer
from ..config import ensure_config
from ..errors import ManagerError
from ..paths import Paths, account_path, ensure_dirs, list_accounts, sanitize_name
from ..storage import atomic_write_json, load_state, manager_lock, read_json, save_state, write_log
from ..terminal import bad, dim, ok, style, warn
from ..views import describe_account, print_accounts


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


def restore_previously_active_auth(
    paths: Paths,
    previous_active: str | None,
    original_exists: bool,
    original_auth: dict | None,
) -> None:
    state = load_state(paths)
    state["active"] = previous_active
    save_state(paths, state)

    target_auth = paths.codex_app_auth
    if previous_active:
        previous_path = account_path(paths, previous_active)
        if previous_path.exists():
            target_auth.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            atomic_write_json(target_auth, read_auth(previous_path))
            write_log(paths, f"restored active auth to {previous_active} after compact")
            return

    if original_exists and original_auth is not None:
        target_auth.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        atomic_write_json(target_auth, original_auth)
        write_log(paths, "restored original auth file after compact")
        return

    target_auth.unlink(missing_ok=True)
    write_log(paths, "removed temporary app-server auth after compact")


@contextlib.contextmanager
def checked_out_app_server_auth(paths: Paths, account: str):
    target_auth = paths.codex_app_auth
    selected_path = account_path(paths, account)
    config = ensure_config(paths)
    with manager_lock(paths):
        ensure_dirs(paths)
        previous_active = load_state(paths).get("active")
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

            restore_previously_active_auth(
                paths,
                previous_active,
                original_exists,
                original_auth,
            )


def cmd_compact(args) -> int:
    paths = Paths()
    ensure_dirs(paths)
    config = ensure_config(paths)
    thread_id = resolve_session_id(args.session_id)
    account = select_account_for_compact(paths, args.account)
    print(f"compact session: {thread_id}")
    print(f"account: {account}")
    with checked_out_app_server_auth(paths, account) as refresh_handler:
        server = CodexAppServer(
            paths.codex_home,
            codex_bin=args.codex_bin,
            auth_refresh_handler=refresh_handler,
            proxy_url=config.get("proxy"),
        )
        print(dim(f"Starting Codex app-server with {server.executable_label()}..."))
        with server:
            server.initialize()
            print(dim("Resuming session in app-server..."))
            server.resume_thread(thread_id)
            server.compact_thread(thread_id)
            print(dim("Compaction started; waiting for completion..."))
            result = server.wait_for_compaction(thread_id, timeout=args.timeout)
    detail = f"turn {result['turn_id']}" if result.get("turn_id") else "compact turn"
    print(ok(f"compaction completed for {thread_id} with {account} ({detail})"))
    return 0
