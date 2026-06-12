from __future__ import annotations

import datetime as dt
import json
import os
import tempfile
from dataclasses import dataclass
from typing import Any

from .errors import ManagerError
from .paths import Paths, ensure_dirs
from .time_utils import iso_now, parse_datetime, utcnow


@dataclass(frozen=True)
class HistorySeries:
    account: str
    window_label: str
    timezone_label: str
    points: list[tuple[dt.datetime, float]]


@dataclass(frozen=True)
class HistoryWindow:
    account: str
    window_label: str
    offset_label: str
    timezone_label: str
    primary_points: list[tuple[dt.datetime, float]]
    secondary_points: list[tuple[dt.datetime, float]]


def append_rate_limit_history(paths: Paths, name: str, rate_limits: dict[str, Any] | None) -> None:
    entry = _build_history_entry(name, rate_limits)
    if entry is None:
        return
    ensure_dirs(paths)
    paths.history_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    existing = load_rate_limit_history(paths, account=name)
    if existing:
        last = existing[-1]
        if (
            last.get("recorded_at") == entry["recorded_at"]
            and last.get("primary_remaining_percent") == entry["primary_remaining_percent"]
            and last.get("secondary_remaining_percent") == entry["secondary_remaining_percent"]
        ):
            return
    with paths.history_file.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False))
        handle.write("\n")
    os.chmod(paths.history_file, 0o600)


def prune_rate_limit_history(paths: Paths, retention_days: int) -> None:
    if retention_days < 1 or not paths.history_file.exists():
        return
    cutoff = utcnow() - dt.timedelta(days=retention_days)
    kept = [
        entry
        for entry in load_rate_limit_history(paths)
        if (parse_datetime(entry.get("recorded_at")) or cutoff) >= cutoff
    ]
    tmp_path = paths.history_file.with_suffix(".tmp")
    with tmp_path.open("w", encoding="utf-8") as handle:
        for entry in kept:
            handle.write(json.dumps(entry, ensure_ascii=False))
            handle.write("\n")
    os.chmod(tmp_path, 0o600)
    os.replace(tmp_path, paths.history_file)


def load_rate_limit_history(paths: Paths, account: str | None = None) -> list[dict[str, Any]]:
    if not paths.history_file.exists():
        return []
    rows: list[dict[str, Any]] = []
    with paths.history_file.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            raw = line.strip()
            if not raw:
                continue
            try:
                value = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(value, dict):
                continue
            if account and value.get("account") != account:
                continue
            rows.append(value)
    return rows


def available_history_accounts(paths: Paths) -> list[str]:
    return sorted({str(entry.get("account")) for entry in load_rate_limit_history(paths) if entry.get("account")})


def rename_history_account(paths: Paths, old_name: str, new_name: str) -> int:
    rows = load_rate_limit_history(paths)
    renamed = 0
    for row in rows:
        if row.get("account") == old_name:
            row["account"] = new_name
            renamed += 1
    if renamed == 0:
        return 0
    ensure_dirs(paths)
    fd, tmp = tempfile.mkstemp(prefix=f".{paths.history_file.name}.", suffix=".tmp", dir=str(paths.history_dir))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False))
                handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, paths.history_file)
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
    return renamed


def parse_timezone_offset(value: str | None) -> dt.tzinfo:
    if value is None or not value.strip() or value.strip().lower() == "local":
        return dt.datetime.now().astimezone().tzinfo or dt.timezone.utc
    raw = value.strip().lower()
    if raw == "utc":
        return dt.timezone.utc
    sign = 1
    if raw[0] == "+":
        raw = raw[1:]
    elif raw[0] == "-":
        sign = -1
        raw = raw[1:]
    if ":" in raw:
        hours_text, minutes_text = raw.split(":", 1)
    else:
        hours_text, minutes_text = raw, "0"
    try:
        hours = int(hours_text)
        minutes = int(minutes_text)
    except ValueError as exc:
        raise ManagerError("offset must look like UTC, local, +03:30, or -07:00") from exc
    if hours > 23 or minutes > 59:
        raise ManagerError("offset is out of range")
    return dt.timezone(sign * dt.timedelta(hours=hours, minutes=minutes))


def build_history_series(
    paths: Paths,
    *,
    account: str,
    hours: int | None = None,
    days: int | None = None,
    offset: str | None = None,
    metric: str = "primary_remaining_percent",
) -> HistorySeries:
    if (hours is None) == (days is None):
        raise ManagerError("pick exactly one of --hours or --days")
    if hours is not None and hours < 1:
        raise ManagerError("hours must be greater than zero")
    if days is not None and days < 1:
        raise ManagerError("days must be greater than zero")
    tz = parse_timezone_offset(offset)
    now = utcnow()
    since = now - dt.timedelta(hours=hours) if hours is not None else now - dt.timedelta(days=days)
    window_label = f"{hours}h" if hours is not None else f"{days}d"
    points: list[tuple[dt.datetime, float]] = []
    for entry in load_rate_limit_history(paths, account=account):
        recorded_at = parse_datetime(entry.get("recorded_at"))
        value = entry.get(metric)
        if recorded_at is None or not isinstance(value, (int, float)):
            continue
        if recorded_at < since:
            continue
        points.append((recorded_at.astimezone(tz), float(value)))
    points.sort(key=lambda item: item[0])
    if not points:
        raise ManagerError(f"no history for {account} in the selected window")
    return HistorySeries(
        account=account,
        window_label=window_label,
        timezone_label=_timezone_label(tz),
        points=points,
    )


def build_history_window(
    paths: Paths,
    *,
    account: str,
    hours: int | None = None,
    days: int | None = None,
    window_offset: int = 0,
    timezone: str | None = None,
) -> HistoryWindow:
    if (hours is None) == (days is None):
        raise ManagerError("pick exactly one of --hours or --days")
    size = hours if hours is not None else days
    unit_label = "h" if hours is not None else "d"
    if size is None or size < 1:
        raise ManagerError("window size must be greater than zero")
    if window_offset < 0:
        raise ManagerError("window offset must be zero or greater")
    tz = parse_timezone_offset(timezone)
    delta = dt.timedelta(hours=size) if hours is not None else dt.timedelta(days=size)
    offset_delta = dt.timedelta(hours=window_offset) if hours is not None else dt.timedelta(days=window_offset)
    now = utcnow()
    window_end = now - offset_delta
    window_start = window_end - delta
    primary_points: list[tuple[dt.datetime, float]] = []
    secondary_points: list[tuple[dt.datetime, float]] = []
    for entry in load_rate_limit_history(paths, account=account):
        recorded_at = parse_datetime(entry.get("recorded_at"))
        if recorded_at is None or recorded_at < window_start or recorded_at > window_end:
            continue
        localized = recorded_at.astimezone(tz)
        primary = entry.get("primary_remaining_percent")
        secondary = entry.get("secondary_remaining_percent")
        if isinstance(primary, (int, float)):
            primary_points.append((localized, float(primary)))
        if isinstance(secondary, (int, float)):
            secondary_points.append((localized, float(secondary)))
    primary_points.sort(key=lambda item: item[0])
    secondary_points.sort(key=lambda item: item[0])
    if not primary_points and not secondary_points:
        raise ManagerError(f"no history for {account} in the selected window")
    return HistoryWindow(
        account=account,
        window_label=f"{size}{unit_label}",
        offset_label=f"{window_offset}{unit_label}",
        timezone_label=_timezone_label(tz),
        primary_points=primary_points,
        secondary_points=secondary_points,
    )


def _build_history_entry(name: str, rate_limits: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(rate_limits, dict):
        return None
    snapshots = rate_limits.get("snapshots")
    if not isinstance(snapshots, list):
        return None
    codex = next(
        (item for item in snapshots if isinstance(item, dict) and item.get("limit_id") == "codex"),
        None,
    )
    if not isinstance(codex, dict):
        return None
    primary = codex.get("primary") if isinstance(codex.get("primary"), dict) else {}
    secondary = codex.get("secondary") if isinstance(codex.get("secondary"), dict) else {}
    recorded_at = rate_limits.get("fetched_at") if isinstance(rate_limits.get("fetched_at"), str) else iso_now()
    return {
        "recorded_at": recorded_at,
        "account": name,
        "plan_type": rate_limits.get("plan_type"),
        "primary_remaining_percent": primary.get("remaining_percent"),
        "primary_used_percent": primary.get("used_percent"),
        "primary_window_minutes": primary.get("window_minutes"),
        "secondary_remaining_percent": secondary.get("remaining_percent"),
        "secondary_used_percent": secondary.get("used_percent"),
        "secondary_window_minutes": secondary.get("window_minutes"),
    }


def _timezone_label(tz: dt.tzinfo) -> str:
    now = dt.datetime.now(tz)
    offset = now.utcoffset() or dt.timedelta(0)
    total_minutes = int(offset.total_seconds() // 60)
    sign = "+" if total_minutes >= 0 else "-"
    total_minutes = abs(total_minutes)
    hours, minutes = divmod(total_minutes, 60)
    return f"UTC{sign}{hours:02d}:{minutes:02d}"
