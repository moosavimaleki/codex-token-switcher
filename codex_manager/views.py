from __future__ import annotations

from pathlib import Path

from .auth import access_expiry, account_metadata, read_auth, should_refresh
from .errors import ManagerError
from .paths import Paths, list_accounts
from .storage import file_mode, load_state
from .terminal import bad, dim, info, ok, style, warn
from .time_utils import human_delta, utcnow


def describe_account(paths: Paths, name: str, active: str | None) -> dict[str, str]:
    try:
        auth = read_auth(paths.accounts_dir / f"{name}.json")
        meta = account_metadata(auth)
        exp = access_expiry(auth)
        need, reason = should_refresh(auth)
        state = "active" if name == active else "refresh soon" if need else "ok"
        expires = human_delta(exp - utcnow()) if exp else "unknown"
        return {
            "name": name,
            "mark": "●" if name == active else " ",
            "state": state,
            "expires": expires,
            "email": meta.get("email") or "unknown",
            "account": meta.get("account_id") or "unknown",
            "reason": reason,
        }
    except ManagerError as exc:
        return {
            "name": name,
            "mark": "●" if name == active else " ",
            "state": "error",
            "expires": "unknown",
            "email": "unknown",
            "account": "unknown",
            "reason": str(exc),
        }


def print_accounts(paths: Paths) -> None:
    active = load_state(paths).get("active")
    rows = [describe_account(paths, name, active) for name in list_accounts(paths)]
    if not rows:
        print(warn("No accounts yet."))
        print(f"Add one with: {info('codex-manager add <name> <auth.json>')}")
        return
    print(f"{dim('Active auth')}  {paths.codex_auth}")
    print(f"{dim('Manager')}      {paths.manager_home}")
    print("")
    print(f"  {style('Account', 'bold'):<18} {style('State', 'bold'):<16} {style('Expires', 'bold'):<12} {style('Email', 'bold')}")
    for row in rows:
        state_label = row["state"]
        state_text = {
            "active": ok("active"),
            "ok": ok("ok"),
            "refresh soon": warn("refresh soon"),
            "error": bad("error"),
        }.get(state_label, state_label)
        marker = ok("●") if row["name"] == active else dim("○")
        print(f"{marker} {row['name']:<18} {state_text:<25} {row['expires']:<12} {row['email']}")


def colored_mode(path: Path, expected: str | None = None) -> str:
    mode = file_mode(path)
    if mode == "missing":
        return warn(mode)
    if expected and mode != expected:
        return warn(mode)
    return ok(mode)
