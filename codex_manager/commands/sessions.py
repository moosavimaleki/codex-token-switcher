from __future__ import annotations

from ..auth import account_metadata, read_auth
from ..chatgpt_sessions import ChatGPTSessionClient, ChromeProfile, ProfileNotSignedIn, chatgpt_switch_accounts, chrome_account_email, codex_sessions, discover_chrome_profiles, load_chatgpt_cookies, session_time
from ..config import ensure_config
from ..errors import ManagerError
from ..paths import Paths, account_path, list_accounts, status_path
from ..storage import atomic_write_json, manager_lock, read_json, write_log
from ..time_utils import iso_now


def cache_chrome_profile(
    paths: Paths,
    email: str | None,
    profile: ChromeProfile,
    switch_accounts: list[str] | None = None,
) -> str | None:
    if not email:
        return None
    for name in list_accounts(paths):
        try:
            stored_email = account_metadata(read_auth(account_path(paths, name))).get("email")
        except ManagerError:
            continue
        if isinstance(stored_email, str) and stored_email.lower() == email:
            path = status_path(paths, name)
            existing = read_json(path) if path.exists() else {}
            existing["chrome_profile"] = {
                "directory": profile.directory,
                "display_name": profile.display_name,
                "chrome_root": str(profile.chrome_root) if profile.chrome_root else None,
                "chatgpt_accounts": switch_accounts or [],
                "updated_at": iso_now(),
            }
            atomic_write_json(path, existing)
            return name
    return None


def session_monitor_is_disabled(paths: Paths, account: str | None) -> bool:
    if not account:
        return False
    try:
        return read_json(status_path(paths, account)).get("session_monitor_disabled") is True
    except ManagerError:
        return False


def record_session_monitor_status(
    paths: Paths,
    account: str | None,
    *,
    devices: int,
    codex_sessions: int,
    excess: int,
    revoked: int,
    revocation_disabled: bool,
    current_device_protected: bool,
) -> None:
    if not account:
        return
    path = status_path(paths, account)
    existing = read_json(path) if path.exists() else {}
    previous = existing.get("session_monitor")
    previous_total = previous.get("revoked_total") if isinstance(previous, dict) else 0
    if not isinstance(previous_total, int) or previous_total < 0:
        previous_total = 0
    existing["session_monitor"] = {
        "last_checked_at": iso_now(),
        "devices": devices,
        "codex_sessions": codex_sessions,
        "excess_codex_sessions": excess,
        "revoked_last_run": revoked,
        "revoked_total": previous_total + revoked,
        "revocation_disabled": revocation_disabled,
        "current_device_protected": current_device_protected,
    }
    atomic_write_json(path, existing)


def session_result_message(result: dict, *, dry_run: bool = False) -> str:
    profile = result.get("profile_label") or result.get("profile") or "unknown profile"
    email = result.get("email") or "unknown"
    switch_accounts = result.get("switch_accounts")
    switch_summary = ""
    if isinstance(switch_accounts, list) and len(switch_accounts) > 1:
        switch_summary = (
            f"; WARNING: {len(switch_accounts)} saved ChatGPT accounts: {', '.join(switch_accounts)}"
            "; session operations apply only to the active email above"
        )
    if result.get("error"):
        return f"{profile}: email {email}; error {result['error']}{switch_summary}"
    if result.get("not_signed_in"):
        return f"{profile}: email {email}; ChatGPT session unavailable; skipped{switch_summary}"
    account = result.get("account") or "unmanaged"
    devices = result.get("devices", 0)
    codex = result.get("codex_sessions", 0)
    protected = "; current device protected" if result.get("current_device_protected") else ""
    revoked = result.get("revoked", 0)
    if result.get("revocation_disabled"):
        action = f"revocation disabled; {result.get('excess', 0)} excess left unchanged"
    else:
        action = f"would revoke {result.get('excess', 0)}" if dry_run else f"revoked {revoked}"
    return f"{profile}: email {email}; account {account}; devices {devices}; Codex {codex}{protected}; {action}{switch_summary}"


def monitor_sessions(paths: Paths, *, dry_run: bool = False) -> dict:
    config = ensure_config(paths)
    profiles = discover_chrome_profiles(config.get("chrome_root"))
    results = []
    failures = 0
    revoked = 0
    with manager_lock(paths):
        for profile in profiles:
            email = None
            mapped_account = None
            switch_accounts = chatgpt_switch_accounts(profile)
            try:
                cookies = load_chatgpt_cookies(profile)
                email = chrome_account_email(cookies)
                mapped_account = cache_chrome_profile(paths, email, profile, switch_accounts)
                revocation_disabled = session_monitor_is_disabled(paths, mapped_account)
                client = ChatGPTSessionClient(cookies, proxy_url=config.get("proxy"))
                devices = client.devices()
                sessions = codex_sessions(devices)
                current = [device for device in sessions if device.get("is_current_device") is True]
                if current:
                    # The browser's current device is never revoked, even when it
                    # also reports a ChatGPT Web application session.
                    extras = [device for device in sessions if device not in current]
                    keep = current
                else:
                    windows = [device for device in sessions if device.get("platform") == "windows"]
                    non_windows = [device for device in sessions if device.get("platform") != "windows"]
                    extras = windows if len(sessions) > 1 else []
                    remaining = non_windows if extras else sessions
                    if len(remaining) > 1:
                        extras = [*extras, *remaining[1:]]
                    keep = [device for device in sessions if device not in extras][:1]
                revoked_for_account = 0
                if not revocation_disabled:
                    for device in extras:
                        session_id = device.get("session_id")
                        if not isinstance(session_id, str) or not session_id:
                            raise ManagerError("Linux Codex session has no session id")
                        if not dry_run:
                            client.revoke(session_id)
                            revoked += 1
                            revoked_for_account += 1
                record_session_monitor_status(
                    paths,
                    mapped_account,
                    devices=len(devices),
                    codex_sessions=max(0, len(sessions) - revoked_for_account),
                    excess=len(extras),
                    revoked=revoked_for_account,
                    revocation_disabled=revocation_disabled,
                    current_device_protected=bool(current),
                )
                result = {
                    "profile": profile.name,
                    "profile_label": profile.label,
                    "account": mapped_account,
                    "email": email,
                    "switch_accounts": switch_accounts,
                    "devices": len(devices),
                    "codex_sessions": len(sessions),
                    "current_device_protected": bool(current),
                    "revocation_disabled": revocation_disabled,
                    "kept_at": session_time(keep[0]) if keep else None,
                    "excess": len(extras),
                    "revoked": revoked_for_account,
                }
                results.append(result)
                write_log(paths, f"Chrome session monitor: {session_result_message(result, dry_run=dry_run)}")
            except ProfileNotSignedIn:
                result = {
                    "profile": profile.name,
                    "profile_label": profile.label,
                    "email": email,
                    "switch_accounts": switch_accounts,
                    "not_signed_in": True,
                }
                results.append(result)
                write_log(paths, f"Chrome session monitor: {session_result_message(result, dry_run=dry_run)}")
            except ManagerError as exc:
                failures += 1
                result = {
                    "profile": profile.name,
                    "profile_label": profile.label,
                    "email": email,
                    "switch_accounts": switch_accounts,
                    "error": str(exc),
                }
                results.append(result)
                write_log(paths, f"Chrome session monitor: {session_result_message(result, dry_run=dry_run)}")
    return {"profiles": len(profiles), "results": results, "revoked": revoked, "failures": failures}


def cmd_sessions(args) -> int:
    paths = Paths()
    config = ensure_config(paths)
    if args.quiet and not config.get("session_monitor_enabled"):
        return 0
    summary = monitor_sessions(paths, dry_run=args.dry_run)
    if not args.quiet:
        if not summary["profiles"]:
            print("no Chrome profiles with a Cookies database found")
        for result in summary["results"]:
            print(session_result_message(result, dry_run=args.dry_run))
        print(f"checked {summary['profiles']} Chrome profile(s); revoked {summary['revoked']}; failures {summary['failures']}")
    return 1 if summary["failures"] else 0
