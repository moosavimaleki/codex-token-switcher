from __future__ import annotations

from ..auth import account_metadata, read_auth
from ..chatgpt_sessions import ChatGPTSessionClient, ChromeProfile, ProfileNotSignedIn, chrome_account_email, codex_sessions, discover_chrome_profiles, load_chatgpt_cookies, session_time
from ..config import ensure_config
from ..errors import ManagerError
from ..paths import Paths, account_path, list_accounts, status_path
from ..storage import atomic_write_json, manager_lock, read_json, write_log
from ..time_utils import iso_now


def cache_chrome_profile(paths: Paths, email: str | None, profile: ChromeProfile) -> str | None:
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


def monitor_sessions(paths: Paths, *, dry_run: bool = False) -> dict:
    config = ensure_config(paths)
    profiles = discover_chrome_profiles(config.get("chrome_root"))
    results = []
    failures = 0
    revoked = 0
    with manager_lock(paths):
        for profile in profiles:
            try:
                cookies = load_chatgpt_cookies(profile)
                mapped_account = cache_chrome_profile(paths, chrome_account_email(cookies), profile)
                if session_monitor_is_disabled(paths, mapped_account):
                    results.append({
                        "profile": profile.name,
                        "profile_label": profile.label,
                        "account": mapped_account,
                        "skipped": True,
                    })
                    write_log(paths, f"Chrome session monitor skipped {profile.name}: disabled for account {mapped_account}")
                    continue
                client = ChatGPTSessionClient(cookies, proxy_url=config.get("proxy"))
                sessions = codex_sessions(client.devices())
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
                for device in extras:
                    session_id = device.get("session_id")
                    if not isinstance(session_id, str) or not session_id:
                        raise ManagerError("Linux Codex session has no session id")
                    if not dry_run:
                        client.revoke(session_id)
                        revoked += 1
                result = {
                    "profile": profile.name,
                    "profile_label": profile.label,
                    "account": mapped_account,
                    "codex_sessions": len(sessions),
                    "kept_at": session_time(keep[0]) if keep else None,
                    "excess": len(extras),
                    "revoked": 0 if dry_run else len(extras),
                }
                results.append(result)
                if extras:
                    action = "would revoke" if dry_run else "revoked"
                    write_log(paths, f"Chrome session monitor {action} {result['revoked'] if not dry_run else len(extras)} excess Codex session(s) for {profile.name}")
            except ProfileNotSignedIn:
                results.append({"profile": profile.name, "not_signed_in": True})
            except ManagerError as exc:
                failures += 1
                results.append({"profile": profile.name, "error": str(exc)})
                write_log(paths, f"Chrome session monitor failed for {profile.name}: {exc}")
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
            if result.get("error"):
                print(f"{result['profile']}: {result['error']}")
                continue
            if result.get("not_signed_in"):
                print(f"{result['profile']}: not signed in")
                continue
            if result.get("skipped"):
                print(f"{result.get('profile_label', result['profile'])}: skipped (disabled for {result['account']})")
                continue
            text = f"{result.get('profile_label', result['profile'])}: Codex {result['codex_sessions']}"
            if result["excess"]:
                action = "would revoke" if args.dry_run else "revoked"
                text += f"; {action} {result['revoked'] if not args.dry_run else result['excess']} excess"
            print(text)
        print(f"checked {summary['profiles']} Chrome profile(s); revoked {summary['revoked']}; failures {summary['failures']}")
    return 1 if summary["failures"] else 0
