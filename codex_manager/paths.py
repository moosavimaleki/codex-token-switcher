from __future__ import annotations

import os
from pathlib import Path

from .errors import ManagerError


class Paths:
    def __init__(self) -> None:
        self.home = Path.home()
        self.manager_home = Path(
            os.environ.get("CODEX_MANAGER_HOME", str(self.home / ".codex-manager"))
        ).expanduser()
        self.accounts_dir = self.manager_home / "accounts"
        self.status_dir = self.manager_home / "status"
        self.config_file = self.manager_home / "config.json"
        self.state_file = self.manager_home / "state.json"
        self.lock_file = self.manager_home / "lock"
        self.log_file = self.manager_home / "log.txt"
        self.codex_auth = Path(
            os.environ.get("CODEX_AUTH_PATH", str(self.home / ".codex" / "auth.json"))
        ).expanduser()


def ensure_dirs(paths: Paths) -> None:
    paths.manager_home.mkdir(mode=0o700, parents=True, exist_ok=True)
    paths.accounts_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    paths.status_dir.mkdir(mode=0o700, parents=True, exist_ok=True)


def sanitize_name(name: str) -> str:
    import re

    name = name.strip()
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,80}", name):
        raise ManagerError("account name must match [A-Za-z0-9._-] and be <= 80 chars")
    return name


def account_path(paths: Paths, name: str) -> Path:
    return paths.accounts_dir / f"{sanitize_name(name)}.json"


def status_path(paths: Paths, name: str) -> Path:
    return paths.status_dir / f"{sanitize_name(name)}.json"


def list_accounts(paths: Paths) -> list[str]:
    ensure_dirs(paths)
    return sorted(p.stem for p in paths.accounts_dir.glob("*.json"))
