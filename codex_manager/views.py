from __future__ import annotations

from pathlib import Path

from .auth import access_expiry, account_metadata, read_auth, should_refresh
from .errors import ManagerError
from .codex.limits import format_rate_limits_summary
from .paths import Paths, list_accounts, status_path
from .storage import file_mode, load_state, read_json
from .terminal import bad, dim, info, ok, style, warn
from .time_utils import human_delta, utcnow


def describe_account(paths: Paths, name: str, active: str | None) -> dict[str, str]:
    limits = "limits unknown"
    status_state = None
    status_message = None
    chrome_profile = "-"
    plan = "unknown"
    sp = status_path(paths, name)
    if sp.exists():
        try:
            status = read_json(sp)
            limits = format_rate_limits_summary(status.get("rate_limits"), compact=True)
            status_state = str(status.get("state") or "")
            status_message = str(status.get("message") or "")
            profile = status.get("chrome_profile")
            if isinstance(profile, dict):
                directory = profile.get("directory")
                display_name = profile.get("display_name")
                if isinstance(directory, str) and isinstance(display_name, str):
                    chrome_profile = f"{display_name} ({directory})"
        except ManagerError:
            limits = "limits unknown"
    try:
        auth = read_auth(paths.accounts_dir / f"{name}.json")
        meta = account_metadata(auth)
        plan = str(meta.get("plan") or "unknown").lower()
        exp = access_expiry(auth)
        need, reason = should_refresh(auth)
        state = "active" if name == active else "refresh soon" if need else "ok"
        if status_state in {"warning", "needs_login"}:
            state = status_state
            if status_message:
                reason = status_message
        expires = human_delta(exp - utcnow()) if exp else "unknown"
        return {
            "name": name,
            "mark": "●" if name == active else " ",
            "state": state,
            "expires": expires,
            "email": meta.get("email") or "unknown",
            "account": meta.get("account_id") or "unknown",
            "reason": reason,
            "limits": limits,
            "chrome_profile": chrome_profile,
            "plan": plan,
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
            "limits": limits,
            "chrome_profile": chrome_profile,
            "plan": plan,
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
    print(
        f"  {style('Account', 'bold'):<18} {style('State', 'bold'):<16} "
        f"{style('Expires', 'bold'):<12} {style('Limits', 'bold'):<36} {style('Email', 'bold')}"
    )
    for row in rows:
        state_label = row["state"]
        state_text = {
            "active": ok("active"),
            "ok": ok("ok"),
            "warning": warn("warning"),
            "refresh soon": warn("refresh soon"),
            "error": bad("error"),
            "needs_login": bad("needs_login"),
        }.get(state_label, state_label)
        marker = ok("●") if row["name"] == active else dim("○")
        limits = row["limits"][:35]
        print(f"{marker} {row['name']:<18} {state_text:<25} {row['expires']:<12} {limits:<36} {row['email']}")


def colored_mode(path: Path, expected: str | None = None) -> str:
    mode = file_mode(path)
    if mode == "missing":
        return warn(mode)
    if expected and mode != expected:
        return warn(mode)
    return ok(mode)
