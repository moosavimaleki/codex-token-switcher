from __future__ import annotations

import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from ..auth import account_metadata, read_auth
from ..commands.accounts import add_account
from ..config import ensure_config
from ..errors import ManagerError
from ..paths import Paths, account_path, ensure_dirs, sanitize_name
from .app_server import CodexAppServer


@dataclass(frozen=True)
class DeviceLoginCode:
    login_id: str
    verification_url: str
    user_code: str


@dataclass(frozen=True)
class DeviceLoginResult:
    name: str
    email: str | None
    account_id: str | None


DeviceCodeCallback = Callable[[DeviceLoginCode], None]
DevicePollCallback = Callable[[int, float], None]


def login_with_device_code(
    paths: Paths,
    name: str,
    *,
    on_code: DeviceCodeCallback | None = None,
    on_poll: DevicePollCallback | None = None,
    cancel_event: threading.Event | None = None,
    timeout: float = 900.0,
    codex_bin: str | None = None,
) -> DeviceLoginResult:
    clean_name = sanitize_name(name)
    ensure_dirs(paths)
    if account_path(paths, clean_name).exists():
        raise ManagerError(f"account already exists: {clean_name}")

    config = ensure_config(paths)
    with tempfile.TemporaryDirectory(prefix="codex-manager-login-") as tmpdir:
        codex_home = Path(tmpdir)
        with CodexAppServer(
            codex_home,
            codex_bin=codex_bin,
            proxy_url=config.get("proxy"),
        ) as server:
            server.initialize()
            response = server.start_chatgpt_device_login()
            code = _device_login_code(response)
            if on_code is not None:
                on_code(code)
            server.wait_for_login_completion_with_progress(
                code.login_id,
                timeout=timeout,
                on_poll=on_poll,
                cancel_requested=cancel_event.is_set if cancel_event is not None else None,
            )

        auth_path = codex_home / "auth.json"
        auth = read_auth(auth_path)
        meta = account_metadata(auth)
        add_account(paths, clean_name, str(auth_path))
        return DeviceLoginResult(
            name=clean_name,
            email=meta.get("email"),
            account_id=meta.get("account_id"),
        )


def _device_login_code(response: dict) -> DeviceLoginCode:
    if response.get("type") != "chatgptDeviceCode":
        raise ManagerError(f"unexpected login response: {response.get('type') or response!r}")
    login_id = response.get("loginId")
    verification_url = response.get("verificationUrl")
    user_code = response.get("userCode")
    if not all(isinstance(value, str) and value for value in (login_id, verification_url, user_code)):
        raise ManagerError("device login response was missing loginId, verificationUrl, or userCode")
    return DeviceLoginCode(
        login_id=login_id,
        verification_url=verification_url,
        user_code=user_code,
    )
