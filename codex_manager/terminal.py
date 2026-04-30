from __future__ import annotations

import os
import sys
from typing import Any

ANSI = {
    "reset": "\033[0m",
    "bold": "\033[1m",
    "dim": "\033[2m",
    "red": "\033[31m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "blue": "\033[34m",
    "magenta": "\033[35m",
    "cyan": "\033[36m",
}


def color_enabled() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    return sys.stdout.isatty()


def style(text: Any, *names: str) -> str:
    value = str(text)
    if not color_enabled():
        return value
    prefix = "".join(ANSI[name] for name in names if name in ANSI)
    return f"{prefix}{value}{ANSI['reset']}" if prefix else value


def ok(text: Any) -> str:
    return style(text, "green")


def warn(text: Any) -> str:
    return style(text, "yellow")


def bad(text: Any) -> str:
    return style(text, "red")


def info(text: Any) -> str:
    return style(text, "cyan")


def dim(text: Any) -> str:
    return style(text, "dim")


def badge(label: str, state: str) -> str:
    normalized = state.lower()
    if normalized in {"ok", "active", "success", "enabled", "synced"}:
        return ok(f"● {label}")
    if normalized in {"warning", "refresh soon", "needs_login", "missing"}:
        return warn(f"● {label}")
    if normalized in {"error", "failed", "bad"}:
        return bad(f"● {label}")
    return info(f"● {label}")


def section(title: str) -> None:
    print("")
    print(style(f"╭─ {title}", "bold", "cyan"))
