from __future__ import annotations

from ..chatgpt_sessions import ChatGPTSessionClient, ProfileNotSignedIn, codex_sessions, discover_chrome_profiles, load_chatgpt_cookies, session_time
from ..config import ensure_config
from ..errors import ManagerError
from ..paths import Paths
from ..storage import manager_lock, write_log


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
                client = ChatGPTSessionClient(cookies, proxy_url=config.get("proxy"))
                sessions = codex_sessions(client.devices())
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
            text = f"{result['profile']}: Codex {result['codex_sessions']}"
            if result["excess"]:
                action = "would revoke" if args.dry_run else "revoked"
                text += f"; {action} {result['revoked'] if not args.dry_run else result['excess']} excess"
            print(text)
        print(f"checked {summary['profiles']} Chrome profile(s); revoked {summary['revoked']}; failures {summary['failures']}")
    return 1 if summary["failures"] else 0
