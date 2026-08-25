from __future__ import annotations

import contextlib
import datetime as dt
from dataclasses import dataclass
from math import inf
from typing import Any

from .errors import ManagerError
from .paths import Paths, status_path
from .storage import read_json
from .time_utils import human_delta, parse_datetime, utcnow


WEEKLY_PERIOD = dt.timedelta(days=7)
STALE_AFTER = dt.timedelta(minutes=15)
VERY_STALE_AFTER = dt.timedelta(hours=1)
WEEKLY_NEAR_RESET = dt.timedelta(hours=12)
WEEKLY_SOFT_BUFFER = 5.0


@dataclass
class AccountRecommendation:
    name: str
    label: str
    reason: str
    score: float
    recommendable: bool = False
    is_best: bool = False
    weekly_remaining: float | None = None
    weekly_target: float | None = None
    weekly_health: float | None = None


@dataclass(frozen=True)
class _Window:
    remaining: float
    reset_at: dt.datetime | None
    period: dt.timedelta


def account_rank_sort_key(plan: str, score: float, name: str) -> tuple[bool, float, str]:
    """Keep Free accounts below paid accounts while preserving recommendation rank."""
    return (plan == "free", -score, name.lower())


def account_recommendations(
    paths: Paths,
    names: list[str],
    *,
    now: dt.datetime | None = None,
) -> dict[str, AccountRecommendation]:
    current = now or utcnow()
    statuses = {name: _load_status(paths, name) for name in names}
    healthy_count = max(
        1,
        sum(
            1
            for status in statuses.values()
            if str(status.get("state") or "") not in {"needs_login", "error"}
            and _has_codex_limits(status)
        ),
    )
    recommendations = {
        name: _score_account(name, statuses[name], healthy_count=healthy_count, now=current)
        for name in names
    }
    ranked = sorted(recommendations.values(), key=lambda item: item.score, reverse=True)
    best = next((item for item in ranked if item.recommendable), ranked[0] if ranked else None)
    if best is not None and best.score > -inf:
        best.is_best = True
        best.label = "BEST" if best.recommendable else "RISK"
    return recommendations


def _load_status(paths: Paths, name: str) -> dict[str, Any]:
    with contextlib.suppress(ManagerError):
        return read_json(status_path(paths, name))
    return {}


def _has_codex_limits(status: dict[str, Any]) -> bool:
    return _codex_snapshot(status.get("rate_limits")) is not None


def _score_account(
    name: str,
    status: dict[str, Any],
    *,
    healthy_count: int,
    now: dt.datetime,
) -> AccountRecommendation:
    status_state = str(status.get("state") or "")
    if status_state in {"needs_login", "error"}:
        return AccountRecommendation(name, "LOGIN", status_state.replace("_", " "), -inf)

    rate_limits = status.get("rate_limits")
    snapshot = _codex_snapshot(rate_limits)
    if snapshot is None:
        return AccountRecommendation(name, "CHECK", "run Check Now to refresh limits", -inf)

    fetched_at = parse_datetime(rate_limits.get("fetched_at")) if isinstance(rate_limits, dict) else None
    stale_age = now - fetched_at if fetched_at is not None else None
    stale = stale_age is None or stale_age > STALE_AFTER
    very_stale = stale_age is None or stale_age > VERY_STALE_AFTER

    weekly = _window(_weekly_window(snapshot), fetched_at, now, WEEKLY_PERIOD)
    if weekly is None:
        return AccountRecommendation(name, "CHECK", "missing weekly limit data", -inf)

    if bool(snapshot.get("limit_reached")) or weekly.remaining <= 0:
        return AccountRecommendation(
            name,
            "SAVE",
            "weekly limit reached",
            -inf,
            weekly_remaining=weekly.remaining,
        )

    weekly_remaining_time = _remaining_time(weekly, now)
    weekly_target = _weekly_target(weekly_remaining_time, weekly.period, healthy_count, stale)
    weekly_health = weekly.remaining - weekly_target
    weekly_protected = weekly_health < -WEEKLY_SOFT_BUFFER and not (
        weekly_remaining_time is not None and weekly_remaining_time <= WEEKLY_NEAR_RESET
    )

    # A quota that resets sooner is cheaper to spend now. This keeps account
    # rotation aligned with the next reset date rather than remaining percent alone.
    reset_urgency = 0.0
    if weekly_remaining_time is not None:
        reset_urgency = 100.0 * (1.0 - weekly_remaining_time / weekly.period)
    score = weekly_health * 3.0 + weekly.remaining * 0.15 + reset_urgency * 0.35
    if weekly_protected:
        score -= 120.0
    if status_state == "warning":
        score -= 30.0
    if stale_age is None:
        score -= 35.0
    elif stale_age > STALE_AFTER:
        score -= min(35.0, (stale_age - STALE_AFTER).total_seconds() / 180.0)

    label = "OK"
    if weekly_protected:
        label = "SAVE"
    if stale:
        label = "STALE" if label == "OK" else label

    reason_parts = [
        f"weekly {weekly.remaining:.0f}% vs target {weekly_target:.0f}% ({weekly_health:+.0f})",
    ]
    if weekly_remaining_time is not None:
        reason_parts.append(f"weekly reset {human_delta(weekly_remaining_time)}")
        reason_parts.append(f"reset priority {reset_urgency:.0f}")
    if weekly_protected:
        reason_parts.append("protect weekly pace")
    elif stale:
        reason_parts.append("stale sample")

    return AccountRecommendation(
        name=name,
        label=label,
        reason=", ".join(reason_parts),
        score=score,
        recommendable=not weekly_protected and not very_stale,
        weekly_remaining=weekly.remaining,
        weekly_target=weekly_target,
        weekly_health=weekly_health,
    )


def _codex_snapshot(rate_limits: Any) -> dict[str, Any] | None:
    if not isinstance(rate_limits, dict):
        return None
    snapshots = rate_limits.get("snapshots")
    if not isinstance(snapshots, list):
        return None
    return next(
        (
            item
            for item in snapshots
            if isinstance(item, dict) and item.get("limit_id") == "codex"
        ),
        next((item for item in snapshots if isinstance(item, dict)), None),
    )


def _weekly_window(snapshot: dict[str, Any]) -> Any:
    secondary = snapshot.get("secondary")
    if isinstance(secondary, dict):
        return secondary
    primary = snapshot.get("primary")
    return primary if isinstance(primary, dict) else None


def _window(
    value: Any,
    fetched_at: dt.datetime | None,
    now: dt.datetime,
    fallback_period: dt.timedelta,
) -> _Window | None:
    if not isinstance(value, dict):
        return None
    remaining = value.get("remaining_percent")
    if not isinstance(remaining, (int, float)):
        return None
    window_minutes = value.get("window_minutes")
    period = (
        dt.timedelta(minutes=float(window_minutes))
        if isinstance(window_minutes, (int, float)) and window_minutes > 0
        else fallback_period
    )
    return _Window(
        remaining=max(0.0, min(100.0, float(remaining))),
        reset_at=_reset_at(value, fetched_at, now),
        period=period,
    )


def _reset_at(
    value: dict[str, Any],
    fetched_at: dt.datetime | None,
    now: dt.datetime,
) -> dt.datetime | None:
    reset_at = value.get("reset_at")
    if isinstance(reset_at, (int, float)):
        timestamp = float(reset_at)
        if timestamp > 10_000_000_000:
            timestamp /= 1000.0
        with contextlib.suppress(ValueError, OSError, OverflowError):
            return dt.datetime.fromtimestamp(timestamp, dt.timezone.utc)

    reset_after = value.get("reset_after_seconds")
    if isinstance(reset_after, (int, float)):
        base = fetched_at or now
        return base + dt.timedelta(seconds=float(reset_after))
    return None


def _remaining_time(window: _Window, now: dt.datetime) -> dt.timedelta | None:
    if window.reset_at is None:
        return None
    return max(dt.timedelta(0), min(window.period, window.reset_at - now))


def _weekly_target(
    remaining_time: dt.timedelta | None,
    period: dt.timedelta,
    healthy_count: int,
    stale: bool,
) -> float:
    if remaining_time is None:
        return 50.0
    period_seconds = max(1.0, period.total_seconds())
    ideal = 100.0 * max(0.0, min(1.0, remaining_time.total_seconds() / period_seconds))
    safety = 8.0
    if healthy_count <= 2:
        safety = 14.0
    elif healthy_count <= 4:
        safety = 10.0
    if stale:
        safety += 3.0
    return min(100.0, ideal + safety)
