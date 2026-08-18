from __future__ import annotations

import datetime as dt
from pathlib import Path

from .auth import access_expiry, account_metadata, read_auth, should_refresh
from .config import ensure_config, parse_duration_seconds
from .errors import ManagerError
from .codex.limits import format_rate_limits_summary
from .paths import Paths, list_accounts, status_path
from .storage import file_mode, load_state, read_json
from .terminal import bad, dim, info, ok, style, warn
from .time_utils import human_delta, parse_datetime, utcnow


def session_monitor_alert_reason(paths: Paths, plan: str, status: dict) -> str | None:
    """Return a visible alert when a Plus account is not being monitored."""
    if plan != "plus" or status.get("session_monitor_disabled") is True:
        return None
    config = ensure_config(paths)
    if not config.get("session_monitor_enabled"):
        return "session monitoring is disabled in configuration"

    monitor = status.get("session_monitor")
    if not isinstance(monitor, dict):
        return "session monitor has not reported for this Plus account"
    if monitor.get("outcome") == "unavailable":
        return "ChatGPT browser session is unavailable; Codex sessions cannot be monitored"
    if monitor.get("outcome") == "error":
        detail = monitor.get("error")
        if isinstance(detail, str) and detail:
            return f"session monitor failed: {detail}"
        return "session monitor failed; Codex sessions cannot be monitored"

    checked_at = parse_datetime(monitor.get("last_checked_at"))
    if checked_at is None:
        return "session monitor has no valid last-check timestamp"
    interval_seconds = parse_duration_seconds(config["session_monitor_interval"], "session_monitor_interval")
    if utcnow() - checked_at > dt.timedelta(seconds=interval_seconds * 2):
        return f"last session check was {human_delta(utcnow() - checked_at)} ago"
    return None


def describe_account(paths: Paths, name: str, active: str | None) -> dict[str, str]:
    limits = "limits unknown"
    status_state = None
    status_message = None
    chrome_profile = "-"
    plan = "unknown"
    codex_sessions = "not checked"
    revoked_total = "0"
    session_monitor_mode = "not checked yet"
    status: dict = {}
    sp = status_path(paths, name)
    if sp.exists():
        try:
            status = read_json(sp)
            limits = format_rate_limits_summary(status.get("rate_limits"), compact=True)
            status_state = str(status.get("state") or "")
            status_message = str(status.get("message") or "")
            session_monitor = status.get("session_monitor")
            if isinstance(session_monitor, dict):
                session_count = session_monitor.get("codex_sessions")
                revoke_count = session_monitor.get("revoked_total")
                if isinstance(session_count, int) and session_count >= 0:
                    codex_sessions = str(session_count)
                elif session_monitor.get("outcome") == "unavailable":
                    codex_sessions = "unavailable"
                elif session_monitor.get("outcome") == "error":
                    codex_sessions = "error"
                if isinstance(revoke_count, int) and revoke_count >= 0:
                    revoked_total = str(revoke_count)
                if session_monitor.get("outcome") == "unavailable":
                    session_monitor_mode = "unavailable"
                elif session_monitor.get("outcome") == "error":
                    session_monitor_mode = "error"
                else:
                    session_monitor_mode = "ignored" if session_monitor.get("revocation_disabled") is True else "enabled"
            profile = status.get("chrome_profile")
            if isinstance(profile, dict):
                directory = profile.get("directory")
                display_name = profile.get("display_name")
                if isinstance(directory, str) and isinstance(display_name, str):
                    chrome_profile = f"{display_name} ({directory})"
                    switch_accounts = profile.get("chatgpt_accounts")
                    if isinstance(switch_accounts, list) and len(switch_accounts) > 1:
                        chrome_profile += f" [{len(switch_accounts)} ChatGPT]"
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
        if session_alert := session_monitor_alert_reason(paths, plan, status):
            state = "session alert"
            reason = session_alert
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
            "codex_sessions": codex_sessions,
            "revoked_total": revoked_total,
            "session_monitor_mode": session_monitor_mode,
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
            "codex_sessions": codex_sessions,
            "revoked_total": revoked_total,
            "session_monitor_mode": session_monitor_mode,
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
            "session alert": bad("session alert"),
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
