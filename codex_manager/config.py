from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from .constants import (
    DEFAULT_HISTORY_RETENTION_DAYS,
    DEFAULT_MAINTAIN_INTERVAL,
    DEFAULT_MONITOR_INTERVAL,
    DEFAULT_RANDOMIZED_DELAY,
    DEFAULT_SESSION_MONITOR_INTERVAL,
)
from .errors import ManagerError
from .paths import Paths, ensure_dirs
from .storage import atomic_write_json, read_json

DEFAULT_CONFIG: dict[str, Any] = {
    "proxy": None,
    "maintain_interval": DEFAULT_MAINTAIN_INTERVAL,
    "monitor_interval": DEFAULT_MONITOR_INTERVAL,
    "session_monitor_enabled": True,
    "session_monitor_interval": DEFAULT_SESSION_MONITOR_INTERVAL,
    "chrome_root": None,
    "randomized_delay": DEFAULT_RANDOMIZED_DELAY,
    "history_retention_days": DEFAULT_HISTORY_RETENTION_DAYS,
}

_DURATION_RE = re.compile(r"^\s*(\d+)\s*([A-Za-z]+)?\s*$")


def parse_duration_seconds(value: Any, field: str, allow_zero: bool = False) -> int:
    if isinstance(value, int):
        seconds = value
    elif isinstance(value, str):
        match = _DURATION_RE.fullmatch(value)
        if not match:
            raise ManagerError(f"{field} must look like 30m, 6h, or 1d")
        amount = int(match.group(1))
        unit = (match.group(2) or "s").lower()
        if unit in {"s", "sec", "secs", "second", "seconds"}:
            seconds = amount
        elif unit in {"m", "min", "mins", "minute", "minutes"}:
            seconds = amount * 60
        elif unit in {"h", "hr", "hrs", "hour", "hours"}:
            seconds = amount * 3600
        elif unit in {"d", "day", "days"}:
            seconds = amount * 86400
        else:
            raise ManagerError(f"{field} uses unsupported duration unit: {unit}")
    else:
        raise ManagerError(f"{field} must be a duration string")

    if seconds < 0 or (seconds == 0 and not allow_zero):
        raise ManagerError(f"{field} must be greater than zero")
    return seconds


def format_duration(seconds: int) -> str:
    if seconds == 0:
        return "0s"
    if seconds % 86400 == 0:
        return f"{seconds // 86400}d"
    if seconds % 3600 == 0:
        return f"{seconds // 3600}h"
    if seconds % 60 == 0:
        return f"{seconds // 60}min"
    return f"{seconds}s"


def normalize_duration(value: Any, field: str, allow_zero: bool = False) -> str:
    return format_duration(parse_duration_seconds(value, field, allow_zero=allow_zero))


def normalize_proxy(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ManagerError("proxy must be a URL string or null")
    value = value.strip()
    if not value or value.lower() in {"none", "null", "off", "false"}:
        return None
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ManagerError("proxy must be an http:// or https:// URL")
    return value


def normalize_chrome_root(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ManagerError("chrome_root must be a directory path or null")
    value = value.strip()
    if not value or value.lower() in {"none", "null", "off", "false"}:
        return None
    return value


def normalize_config(config: dict[str, Any]) -> dict[str, Any]:
    merged = dict(DEFAULT_CONFIG)
    merged.update(config)
    retention_days = merged.get("history_retention_days")
    if not isinstance(retention_days, int) or retention_days < 1:
        raise ManagerError("history_retention_days must be a positive integer")
    session_monitor_enabled = merged.get("session_monitor_enabled")
    if not isinstance(session_monitor_enabled, bool):
        raise ManagerError("session_monitor_enabled must be true or false")
    return {
        "proxy": normalize_proxy(merged.get("proxy")),
        "maintain_interval": normalize_duration(merged.get("maintain_interval"), "maintain_interval"),
        "monitor_interval": normalize_duration(merged.get("monitor_interval"), "monitor_interval"),
        "session_monitor_enabled": session_monitor_enabled,
        "session_monitor_interval": normalize_duration(
            merged.get("session_monitor_interval"),
            "session_monitor_interval",
        ),
        "chrome_root": normalize_chrome_root(merged.get("chrome_root")),
        "randomized_delay": normalize_duration(
            merged.get("randomized_delay"),
            "randomized_delay",
            allow_zero=True,
        ),
        "history_retention_days": retention_days,
    }


def load_config(paths: Paths) -> dict[str, Any]:
    if not paths.config_file.exists():
        return dict(DEFAULT_CONFIG)
    return normalize_config(read_json(paths.config_file))


def ensure_config(paths: Paths) -> dict[str, Any]:
    ensure_dirs(paths)
    config = load_config(paths)
    if not paths.config_file.exists():
        atomic_write_json(paths.config_file, config)
    return config


def reset_config(paths: Paths) -> dict[str, Any]:
    """Replace the persisted config with the defaults from the current release."""
    ensure_dirs(paths)
    config = normalize_config(dict(DEFAULT_CONFIG))
    atomic_write_json(paths.config_file, config)
    return config


def save_config(paths: Paths, updates: dict[str, Any]) -> dict[str, Any]:
    config = load_config(paths)
    config.update(updates)
    config = normalize_config(config)
    atomic_write_json(paths.config_file, config)
    return config


def redact_url(value: str | None) -> str:
    if not value:
        return "(none)"
    parsed = urlsplit(value)
    if parsed.password is None:
        return value
    user = parsed.username or ""
    host = parsed.hostname or ""
    if parsed.port:
        host = f"{host}:{parsed.port}"
    return urlunsplit((parsed.scheme, f"{user}:***@{host}", parsed.path, parsed.query, parsed.fragment))


def cron_expression(interval: str) -> str:
    seconds = parse_duration_seconds(interval, "maintain_interval")
    if seconds % 86400 == 0:
        days = seconds // 86400
        if days == 1:
            return "17 0 * * *"
        if days <= 31:
            return f"17 0 */{days} * *"
    if seconds % 3600 == 0:
        hours = seconds // 3600
        if hours == 1:
            return "17 * * * *"
        if 24 % hours == 0:
            return f"17 */{hours} * * *"
    if seconds % 60 == 0:
        minutes = seconds // 60
        if minutes == 1:
            return "* * * * *"
        if minutes < 60 and 60 % minutes == 0:
            return f"*/{minutes} * * * *"
    raise ManagerError("crontab fallback supports intervals that divide 60 minutes or 24 hours")
