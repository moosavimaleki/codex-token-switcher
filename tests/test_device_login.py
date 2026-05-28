from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

from codex_manager.codex.device_login import login_with_device_code
from codex_manager.codex.app_server import CodexAppServer
from codex_manager.errors import ManagerError
from codex_manager.paths import Paths, account_path, ensure_dirs
from codex_manager.storage import atomic_write_json, read_json

from test_accounts_sync import make_auth


class FakeAppServer:
    started = 0

    def __init__(self, codex_home, **_kwargs) -> None:
        self.codex_home = codex_home
        FakeAppServer.started += 1

    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        return None

    def initialize(self) -> dict:
        return {}

    def start_chatgpt_device_login(self) -> dict:
        return {
            "type": "chatgptDeviceCode",
            "loginId": "login-1",
            "verificationUrl": "https://auth.openai.com/codex/device",
            "userCode": "ABCD-EFGH",
        }

    def wait_for_login_completion_with_progress(
        self,
        login_id: str,
        *,
        timeout: float,
        poll_interval: float = 2.0,
        on_poll=None,
        cancel_requested=None,
    ) -> dict:
        self.login_id = login_id
        self.timeout = timeout
        if on_poll is not None:
            on_poll(1, 2.0)
        atomic_write_json(
            self.codex_home / "auth.json",
            make_auth(
                refresh_token="new-refresh",
                account_id="acct-new",
                email="new@example.com",
                subject="user-new",
            ),
        )
        return {"loginId": login_id, "success": True, "error": None}


class DeviceLoginTests(unittest.TestCase):
    def test_device_login_imports_auth_without_touching_live_auth(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with mock.patch.dict(
                os.environ,
                {"CODEX_MANAGER_HOME": f"{tmpdir}/manager", "CODEX_HOME": f"{tmpdir}/codex"},
                clear=False,
            ):
                paths = Paths()
                ensure_dirs(paths)
                atomic_write_json(paths.codex_auth, make_auth(refresh_token="live-refresh"))
                seen_codes = []

                with mock.patch("codex_manager.codex.device_login.CodexAppServer", FakeAppServer):
                    result = login_with_device_code(
                        paths,
                        "new-account",
                        on_code=seen_codes.append,
                        timeout=123,
                    )

                self.assertEqual("new-account", result.name)
                self.assertEqual("new@example.com", result.email)
                self.assertEqual("ABCD-EFGH", seen_codes[0].user_code)
                self.assertEqual("new-refresh", read_json(account_path(paths, "new-account"))["tokens"]["refresh_token"])
                self.assertEqual("live-refresh", read_json(paths.codex_auth)["tokens"]["refresh_token"])

    def test_existing_account_fails_before_starting_login(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with mock.patch.dict(
                os.environ,
                {"CODEX_MANAGER_HOME": f"{tmpdir}/manager", "CODEX_HOME": f"{tmpdir}/codex"},
                clear=False,
            ):
                paths = Paths()
                ensure_dirs(paths)
                atomic_write_json(account_path(paths, "taken"), make_auth(refresh_token="stored-refresh"))
                FakeAppServer.started = 0

                with mock.patch("codex_manager.codex.device_login.CodexAppServer", FakeAppServer):
                    with self.assertRaises(ManagerError):
                        login_with_device_code(paths, "taken")

                self.assertEqual(0, FakeAppServer.started)

    def test_next_notification_normalizes_read_timeout(self) -> None:
        server = object.__new__(CodexAppServer)
        server._notifications = []
        server._handle_server_request = lambda _msg: False
        server._read = mock.Mock(side_effect=ManagerError("timed out reading from app-server"))

        with self.assertRaisesRegex(ManagerError, "timed out waiting for app-server notification"):
            server.next_notification(timeout=0.01)


if __name__ == "__main__":
    unittest.main()
