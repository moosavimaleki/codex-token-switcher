from __future__ import annotations

from .accounts import sync_active, sync_live_auth_to_matching_account, write_status
from ..auth import read_auth, refresh_auth, should_refresh
from ..codex.limits import LimitFetchError, fetch_rate_limits, format_rate_limits_summary
from ..config import ensure_config
from ..errors import ManagerError
from ..paths import Paths, account_path, ensure_dirs, list_accounts
from ..storage import atomic_write_json, load_state, manager_lock, write_log
from ..terminal import badge, dim


def run_account_checks(paths: Paths, include_active: bool, force_refresh: bool = False) -> dict:
    config = ensure_config(paths)
    results = []
    refreshed = 0
    failures = 0
    with manager_lock(paths):
        ensure_dirs(paths)
        if sync_live_auth_to_matching_account(paths) is None:
            sync_active(paths)
        active = load_state(paths).get("active")
        for name in list_accounts(paths):
            path = account_path(paths, name)
            try:
                auth = read_auth(path)
                needed, reason = should_refresh(auth)
                if force_refresh:
                    needed = True
                    reason = f"force refresh requested; {reason}"
                if name == active and not include_active:
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
                    can_refresh_after_limits_failure = (
                        exc.status_code in {401, 403}
                        and not refreshed_now
                        and (name != active or include_active)
                    )
                    if can_refresh_after_limits_failure:
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
    summary = run_account_checks(paths, include_active=args.refresh_active, force_refresh=args.force_refresh)
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
