from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

from codex_manager.chatgpt_sessions import ChromeProfile, codex_sessions
from codex_manager.commands.sessions import monitor_sessions
from codex_manager.paths import Paths


def device(*, client_name: str, platform: str, timestamp: int, session_id: str, current: bool = False) -> dict:
    return {
        "platform": platform,
        "last_signed_in_timestamp_second": timestamp,
        "session_id": session_id,
        "is_current_device": current,
        "app_sessions": [{"client_name": client_name}],
    }


class CodexSessionTests(unittest.TestCase):
    def test_only_codex_sessions_are_selected(self) -> None:
        sessions = codex_sessions([
            device(client_name="Codex", platform="linux", timestamp=10, session_id="linux-old"),
            device(client_name="Codex", platform="linux", timestamp=20, session_id="linux-new"),
            device(client_name="Codex", platform="windows", timestamp=30, session_id="windows"),
            device(client_name="ChatGPT Web", platform="linux", timestamp=40, session_id="web"),
            device(client_name="ChatGPT Android App", platform="android", timestamp=50, session_id="mobile"),
        ])

        self.assertEqual(["linux-old", "linux-new", "windows"], [item["session_id"] for item in sessions])

    def test_monitor_revokes_windows_before_other_codex_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with mock.patch.dict(os.environ, {"CODEX_MANAGER_HOME": f"{tmpdir}/manager"}, clear=False):
                paths = Paths()
                fake_client = mock.Mock()
                fake_client.devices.return_value = [
                    device(client_name="Codex", platform="linux", timestamp=10, session_id="keep"),
                    device(client_name="Codex", platform="linux", timestamp=20, session_id="newer-linux"),
                    device(client_name="Codex", platform="windows", timestamp=30, session_id="windows"),
                    device(client_name="ChatGPT Web", platform="linux", timestamp=40, session_id="web"),
                ]
                profile = ChromeProfile("google-chrome/Default", paths.manager_home / "fake-Cookies")
                with (
                    mock.patch("codex_manager.commands.sessions.discover_chrome_profiles", return_value=[profile]),
                    mock.patch("codex_manager.commands.sessions.load_chatgpt_cookies", return_value=object()),
                    mock.patch("codex_manager.commands.sessions.ChatGPTSessionClient", return_value=fake_client),
                ):
                    summary = monitor_sessions(paths)

        self.assertEqual(2, summary["revoked"])
        self.assertEqual([mock.call("windows"), mock.call("newer-linux")], fake_client.revoke.call_args_list)

    def test_dry_run_never_revokes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with mock.patch.dict(os.environ, {"CODEX_MANAGER_HOME": f"{tmpdir}/manager"}, clear=False):
                paths = Paths()
                fake_client = mock.Mock()
                fake_client.devices.return_value = [
                    device(client_name="Codex", platform="linux", timestamp=10, session_id="keep"),
                    device(client_name="Codex", platform="linux", timestamp=20, session_id="extra"),
                ]
                profile = ChromeProfile("google-chrome/Default", paths.manager_home / "fake-Cookies")
                with (
                    mock.patch("codex_manager.commands.sessions.discover_chrome_profiles", return_value=[profile]),
                    mock.patch("codex_manager.commands.sessions.load_chatgpt_cookies", return_value=object()),
                    mock.patch("codex_manager.commands.sessions.ChatGPTSessionClient", return_value=fake_client),
                ):
                    summary = monitor_sessions(paths, dry_run=True)

        self.assertEqual(0, summary["revoked"])
        fake_client.revoke.assert_not_called()


if __name__ == "__main__":
    unittest.main()
