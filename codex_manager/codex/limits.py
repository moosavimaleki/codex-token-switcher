from __future__ import annotations

import datetime as dt
import json
import urllib.error
import urllib.request
from typing import Any

from ..auth import account_metadata
from ..constants import CHATGPT_USAGE_URL
from ..errors import ManagerError
from ..time_utils import human_delta, iso_now, utcnow


class LimitFetchError(ManagerError):
    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def _open(req: urllib.request.Request, proxy_url: str | None):
    if proxy_url:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url})
        )
        return opener.open(req, timeout=30)
    return urllib.request.urlopen(req, timeout=30)


def _token_value(auth: dict[str, Any], key: str) -> str | None:
    tokens = auth.get("tokens") if isinstance(auth.get("tokens"), dict) else {}
    value = tokens.get(key)
    return value if isinstance(value, str) and value else None


def _headers(auth: dict[str, Any]) -> dict[str, str]:
    token = _token_value(auth, "access_token")
    if not token:
        raise LimitFetchError("cannot fetch limits: missing tokens.access_token")

    headers = {
        "Authorization": f"Bearer {token}",
        "User-Agent": "codex-cli",
        "Accept": "application/json",
    }
    account_id = account_metadata(auth).get("account_id")
    if account_id:
        headers["ChatGPT-Account-ID"] = account_id
    return headers


def _window(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    used_percent = value.get("used_percent")
    window_seconds = value.get("limit_window_seconds")
    reset_after_seconds = value.get("reset_after_seconds")
    reset_at = value.get("reset_at")
    if not isinstance(used_percent, (int, float)):
        return None
    window_minutes = int(window_seconds / 60) if isinstance(window_seconds, (int, float)) else None
    return {
        "used_percent": float(used_percent),
        "remaining_percent": max(0.0, min(100.0, 100.0 - float(used_percent))),
        "window_minutes": window_minutes,
        "reset_after_seconds": reset_after_seconds if isinstance(reset_after_seconds, int) else None,
        "reset_at": reset_at if isinstance(reset_at, int) else None,
    }


def _credits(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    return {
        "has_credits": bool(value.get("has_credits")),
        "unlimited": bool(value.get("unlimited")),
        "balance": value.get("balance") if isinstance(value.get("balance"), str) else None,
    }


def _snapshot(
    limit_id: str,
    limit_name: str | None,
    details: Any,
    credits: Any,
    plan_type: Any,
) -> dict[str, Any] | None:
    if not isinstance(details, dict):
        return None
    primary = _window(details.get("primary_window"))
    secondary = _window(details.get("secondary_window"))
    credit_snapshot = _credits(credits)
    if primary is None and secondary is None and credit_snapshot is None:
        return None
    return {
        "limit_id": limit_id,
        "limit_name": limit_name,
        "allowed": bool(details.get("allowed")),
        "limit_reached": bool(details.get("limit_reached")),
        "primary": primary,
        "secondary": secondary,
        "credits": credit_snapshot,
        "plan_type": plan_type if isinstance(plan_type, str) else None,
    }


def normalize_rate_limits(payload: dict[str, Any]) -> dict[str, Any]:
    snapshots = []
    credits = payload.get("credits")
    plan_type = payload.get("plan_type")
    default = _snapshot("codex", None, payload.get("rate_limit"), credits, plan_type)
    if default:
        snapshots.append(default)

    additional = payload.get("additional_rate_limits")
    if isinstance(additional, list):
        for item in additional:
            if not isinstance(item, dict):
                continue
            limit_name = item.get("limit_name")
            if not isinstance(limit_name, str) or not limit_name:
                continue
            metered_feature = item.get("metered_feature")
            normalized_name = (
                metered_feature if isinstance(metered_feature, str) and metered_feature else limit_name
            ).strip().lower().replace("-", "_")
            snapshot = _snapshot(
                normalized_name,
                limit_name,
                item.get("rate_limit"),
                None,
                plan_type,
            )
            if snapshot:
                snapshots.append(snapshot)

    return {
        "fetched_at": iso_now(),
        "plan_type": plan_type if isinstance(plan_type, str) else None,
        "rate_limit_reached_type": _rate_limit_reached_type(payload.get("rate_limit_reached_type")),
        "snapshots": snapshots,
    }


def _rate_limit_reached_type(value: Any) -> str | None:
    if isinstance(value, dict):
        kind = value.get("type")
        return kind if isinstance(kind, str) else None
    return value if isinstance(value, str) else None


def fetch_rate_limits(auth: dict[str, Any], proxy_url: str | None = None) -> dict[str, Any]:
    req = urllib.request.Request(CHATGPT_USAGE_URL, headers=_headers(auth), method="GET")
    try:
        with _open(req, proxy_url) as resp:
            raw = resp.read()
            payload = json.loads(raw.decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:500]
        raise LimitFetchError(f"limits fetch failed: HTTP {exc.code}: {body}", exc.code) from exc
    except Exception as exc:
        raise LimitFetchError(f"limits fetch failed: {exc}") from exc

    if not isinstance(payload, dict):
        raise LimitFetchError("limits fetch failed: response was not an object")
    return normalize_rate_limits(payload)


def _window_label(window: dict[str, Any] | None, fallback: str) -> str | None:
    if not window:
        return None
    minutes = window.get("window_minutes")
    if isinstance(minutes, int) and minutes > 0:
        if minutes % (60 * 24 * 7) == 0:
            return "weekly"
        if minutes % 60 == 0:
            return f"{minutes // 60}h"
        return human_delta(dt.timedelta(minutes=minutes))
    return fallback


def _window_text(window: dict[str, Any] | None, fallback: str, compact: bool = False) -> str | None:
    if not window:
        return None
    label = _window_label(window, fallback)
    remaining = window.get("remaining_percent")
    if not isinstance(remaining, (int, float)):
        return None
    if compact:
        return f"{label} {remaining:.0f}%"
    reset_after = window.get("reset_after_seconds")
    reset_text = ""
    if isinstance(reset_after, int):
        reset_text = f", resets {human_delta(dt.timedelta(seconds=reset_after))}"
    return f"{label} {remaining:.0f}% left{reset_text}"


def _window_reset_at(window: dict[str, Any] | None, fetched_at: dt.datetime | None) -> dt.datetime | None:
    if not isinstance(window, dict):
        return None
    reset_at = window.get("reset_at")
    if isinstance(reset_at, (int, float)):
        timestamp = float(reset_at)
        if timestamp > 10_000_000_000:
            timestamp /= 1000.0
        try:
            return dt.datetime.fromtimestamp(timestamp, dt.timezone.utc)
        except (ValueError, OSError, OverflowError):
            return None
    reset_after = window.get("reset_after_seconds")
    if isinstance(reset_after, (int, float)):
        return (fetched_at or utcnow()) + dt.timedelta(seconds=float(reset_after))
    return None


def _calendar_label(target: dt.datetime, now: dt.datetime) -> str:
    local_target = target.astimezone()
    local_now = now.astimezone()
    day_delta = (local_target.date() - local_now.date()).days
    if day_delta == 0:
        day_label = "today"
    elif day_delta == 1:
        day_label = "tomorrow"
    else:
        day_label = local_target.strftime("%a %Y-%m-%d")
    zone = local_target.strftime("%Z") or local_target.strftime("%z")
    return f"{day_label} at {local_target.strftime('%H:%M')} {zone}".strip()


def format_rate_limit_resets(rate_limits: dict[str, Any] | None, now: dt.datetime | None = None) -> list[str]:
    if not isinstance(rate_limits, dict):
        return []
    snapshots = rate_limits.get("snapshots")
    if not isinstance(snapshots, list) or not snapshots:
        return []
    codex = next(
        (item for item in snapshots if isinstance(item, dict) and item.get("limit_id") == "codex"),
        snapshots[0],
    )
    if not isinstance(codex, dict):
        return []
    fetched_at_raw = rate_limits.get("fetched_at")
    fetched_at = None
    if isinstance(fetched_at_raw, str):
        try:
            fetched_at = dt.datetime.fromisoformat(fetched_at_raw.replace("Z", "+00:00")).astimezone(dt.timezone.utc)
        except ValueError:
            fetched_at = None
    current = now or utcnow()
    lines = []
    for prefix, window, fallback in (
        ("5h reset", codex.get("primary"), "5h"),
        ("Weekly reset", codex.get("secondary"), "weekly"),
    ):
        label = _window_label(window, fallback)
        reset_at = _window_reset_at(window, fetched_at)
        if label and reset_at is not None:
            lines.append(f"{prefix}: {_calendar_label(reset_at, current)}")
    return lines


def describe_rate_limit_windows(rate_limits: dict[str, Any] | None, now: dt.datetime | None = None) -> list[dict[str, Any]]:
    if not isinstance(rate_limits, dict):
        return []
    snapshots = rate_limits.get("snapshots")
    if not isinstance(snapshots, list) or not snapshots:
        return []
    codex = next(
        (item for item in snapshots if isinstance(item, dict) and item.get("limit_id") == "codex"),
        snapshots[0],
    )
    if not isinstance(codex, dict):
        return []
    fetched_at_raw = rate_limits.get("fetched_at")
    fetched_at = None
    if isinstance(fetched_at_raw, str):
        try:
            fetched_at = dt.datetime.fromisoformat(fetched_at_raw.replace("Z", "+00:00")).astimezone(dt.timezone.utc)
        except ValueError:
            fetched_at = None
    current = now or utcnow()
    windows: list[dict[str, Any]] = []
    for key, fallback in (("primary", "5h"), ("secondary", "weekly")):
        window = codex.get(key)
        if not isinstance(window, dict):
            continue
        label = _window_label(window, fallback)
        remaining = window.get("remaining_percent")
        used = window.get("used_percent")
        reset_at = _window_reset_at(window, fetched_at)
        reached = bool(codex.get("limit_reached")) or (isinstance(remaining, (int, float)) and float(remaining) <= 0.0)
        windows.append(
            {
                "key": key,
                "label": label or fallback,
                "remaining_percent": float(remaining) if isinstance(remaining, (int, float)) else None,
                "used_percent": float(used) if isinstance(used, (int, float)) else None,
                "reset_at": reset_at,
                "reset_text": _calendar_label(reset_at, current) if reset_at is not None else None,
                "reset_after_seconds": window.get("reset_after_seconds")
                if isinstance(window.get("reset_after_seconds"), (int, float))
                else None,
                "reached": reached,
            }
        )
    return windows


def format_rate_limits_summary(rate_limits: dict[str, Any] | None, compact: bool = False) -> str:
    if not isinstance(rate_limits, dict):
        return "limits unknown"
    snapshots = rate_limits.get("snapshots")
    if not isinstance(snapshots, list) or not snapshots:
        return "limits unavailable"
    codex = next(
        (item for item in snapshots if isinstance(item, dict) and item.get("limit_id") == "codex"),
        snapshots[0],
    )
    if not isinstance(codex, dict):
        return "limits unavailable"
    parts = [
        text
        for text in (
            _window_text(codex.get("primary"), "5h", compact=compact),
            _window_text(codex.get("secondary"), "weekly", compact=compact),
        )
        if text
    ]
    fetched_at = rate_limits.get("fetched_at")
    stale = ""
    if isinstance(fetched_at, str):
        try:
            parsed = dt.datetime.fromisoformat(fetched_at.replace("Z", "+00:00"))
            if utcnow() - parsed.astimezone(dt.timezone.utc) > dt.timedelta(minutes=15):
                stale = " (stale)"
        except ValueError:
            pass
    return "; ".join(parts) + stale if parts else "limits unavailable"
