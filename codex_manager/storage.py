from __future__ import annotations

import contextlib
import fcntl
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from .errors import ManagerError
from .paths import Paths, ensure_dirs
from .time_utils import iso_now


def atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)


def read_json(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as f:
            value = json.load(f)
    except FileNotFoundError as exc:
        raise ManagerError(f"file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ManagerError(f"invalid JSON in {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ManagerError(f"expected JSON object in {path}")
    return value


def load_state(paths: Paths) -> dict[str, Any]:
    if not paths.state_file.exists():
        return {
            "active": None,
            "codex_auth_path": str(paths.codex_auth),
            "created_at": iso_now(),
        }
    state = read_json(paths.state_file)
    state.setdefault("active", None)
    state.setdefault("codex_auth_path", str(paths.codex_auth))
    return state


def save_state(paths: Paths, state: dict[str, Any]) -> None:
    state["codex_auth_path"] = str(paths.codex_auth)
    atomic_write_json(paths.state_file, state)


@contextlib.contextmanager
def manager_lock(paths: Paths):
    ensure_dirs(paths)
    with paths.lock_file.open("a+", encoding="utf-8") as f:
        os.chmod(paths.lock_file, 0o600)
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def file_mode(path: Path) -> str:
    try:
        stat = path.stat()
    except FileNotFoundError:
        return "missing"
    return f"{stat.st_mode & 0o777:o}"


def tail_lines(path: Path, count: int) -> list[str]:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except FileNotFoundError:
        return []
    return lines[-count:]


def write_log(paths: Paths, message: str) -> None:
    ensure_dirs(paths)
    with paths.log_file.open("a", encoding="utf-8") as f:
        f.write(f"{iso_now()} {message}\n")
    os.chmod(paths.log_file, 0o600)
