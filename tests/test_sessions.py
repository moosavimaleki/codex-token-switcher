from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from http.cookiejar import CookieJar

from codex_manager.chatgpt_sessions import ChatGPTSessionClient, ChromeProfile, ProfileNotSignedIn, ProfilePartiallySignedIn, chatgpt_switch_accounts, codex_sessions
from codex_manager.commands.accounts import write_status
from codex_manager.commands.sessions import cache_chrome_profile, cached_chrome_profile_account, monitor_sessions, record_session_monitor_status, scan_chrome_profiles, session_result_message
from codex_manager.errors import ManagerError
from codex_manager.paths import Paths, account_path, status_path
from codex_manager.storage import atomic_write_json, read_json, save_state
from codex_manager.textual_ui import account_rank_sort_key
from codex_manager.views import describe_account


def device(*, client_name: str, platform: str, timestamp: int, session_id: str, current: bool = False) -> dict:
    return {
        "platform": platform,
        "last_signed_in_timestamp_second": timestamp,
        "session_id": session_id,
        "is_current_device": current,
        "app_sessions": [{"client_name": client_name}],
    }


class CodexSessionTests(unittest.TestCase):
    def test_authenticated_account_email_requires_complete_account_data(self) -> None:
        client = ChatGPTSessionClient(CookieJar())
        with mock.patch.object(client, "_request", side_effect=[
            {"accessToken": "token"},
            {"account_ordering": ["account-id"]},
            {
                "id": "user-id",
                "email": "active@example.com",
                "client_id": "client-id",
                "orgs": {"data": [{"id": "org-id"}]},
            },
        ]):
            email = client.authenticated_account_email()

        self.assertEqual("active@example.com", email)

    def test_authenticated_account_email_rejects_partial_sign_in(self) -> None:
        client = ChatGPTSessionClient(CookieJar())
        with mock.patch.object(client, "_request", side_effect=[
            {"accessToken": "token"},
            {"account_ordering": []},
        ]):
            with self.assertRaisesRegex(ProfilePartiallySignedIn, "account selection is missing"):
                client.authenticated_account_email()

    def test_chrome_profile_scan_marks_cookie_without_complete_login_as_partial(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with mock.patch.dict(os.environ, {"CODEX_MANAGER_HOME": f"{tmpdir}/manager"}, clear=False):
                paths = Paths()
                profile = ChromeProfile("google-chrome/Profile 2", paths.manager_home / "fake-Cookies", "Profile 2", "Partial", Path("/chrome"))
                client = mock.Mock()
                client.authenticated_account_email.side_effect = ProfilePartiallySignedIn("account selection is missing")
                with (
                    mock.patch("codex_manager.commands.sessions.discover_chrome_profiles", return_value=[profile]),
                    mock.patch("codex_manager.commands.sessions.chatgpt_switch_accounts", return_value=["saved@example.com"]),
                    mock.patch("codex_manager.commands.sessions.load_chatgpt_cookies", return_value=CookieJar()),
                    mock.patch("codex_manager.commands.sessions.chrome_account_email", return_value="cookie@example.com"),
                    mock.patch("codex_manager.commands.sessions.ChatGPTSessionClient", return_value=client),
                ):
                    results = scan_chrome_profiles(paths)

        self.assertEqual("partial", results[0]["outcome"])
        self.assertEqual("cookie@example.com", results[0]["cookie_email"])
        self.assertEqual("account selection is missing", results[0]["reason"])

    def test_free_accounts_sort_after_paid_accounts(self) -> None:
        ranked = sorted(
            [("free", 100.0, "free"), ("plus", 1.0, "plus")],
            key=lambda item: account_rank_sort_key(*item),
        )

        self.assertEqual(["plus", "free"], [item[2] for item in ranked])

    def test_session_monitor_status_tracks_counts_and_revoke_total(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with mock.patch.dict(os.environ, {"CODEX_MANAGER_HOME": f"{tmpdir}/manager"}, clear=False):
                paths = Paths()
                with mock.patch("codex_manager.commands.sessions.iso_now", side_effect=[
                    "2026-08-17T10:00:00Z",
                    "2026-08-17T11:00:00Z",
                ]):
                    record_session_monitor_status(
                        paths, "account", devices=12, codex_sessions=2, excess=1, revoked=1,
                        revocation_disabled=False, current_device_protected=True,
                    )
                    record_session_monitor_status(
                        paths, "account", devices=10, codex_sessions=1, excess=0, revoked=0,
                        revocation_disabled=True, current_device_protected=False,
                    )
                status = read_json(status_path(paths, "account"))["session_monitor"]

        self.assertEqual(10, status["devices"])
        self.assertEqual(1, status["codex_sessions"])
        self.assertEqual(1, status["revoked_total"])
        self.assertTrue(status["revocation_disabled"])
        self.assertEqual(["2026-08-17T11:00:00Z", "2026-08-17T10:00:00Z"], [item["checked_at"] for item in status["check_history"]])

    def test_chatgpt_switch_accounts_reads_emails_without_retaining_tokens(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            storage = root / "Profile 2" / "Local Storage" / "leveldb"
            storage.mkdir(parents=True)
            storage.joinpath("000001.ldb").write_bytes(
                b"prefix" + b"oai/apps/accountSwitchSessions" + json.dumps([
                    {"email": "first@example.com", "sessionToken": "secret-1", "lastLoggedInAt": 10},
                    {"email": "second@example.com", "sessionToken": "secret-2", "lastLoggedInAt": 20},
                ]).encode() + b"suffix"
            )
            profile = ChromeProfile("google-chrome/Profile 2", root / "Profile 2" / "Cookies", "Profile 2", chrome_root=root)

            accounts = chatgpt_switch_accounts(profile)

        self.assertEqual(["first@example.com", "second@example.com"], accounts)

    def test_session_result_message_reports_email_devices_and_revocations(self) -> None:
        message = session_result_message({
            "profile_label": "moosavi.eruka (Profile 2)",
            "email": "xuylpino008+5@gmail.com",
            "account": "new2-gh",
            "devices": 12,
            "codex_sessions": 2,
            "current_device_protected": True,
            "excess": 1,
            "revoked": 1,
        })

        self.assertEqual(
            "moosavi.eruka (Profile 2): email xuylpino008+5@gmail.com; account new2-gh; "
            "devices 12; Codex 2; current device protected; revoked 1",
            message,
        )

    def test_session_result_message_warns_for_multiple_saved_accounts(self) -> None:
        message = session_result_message({
            "profile_label": "moosavi.eruka (Profile 2)",
            "email": "xuylpino008+5@gmail.com",
            "account": "gh-pppp",
            "skipped": True,
            "switch_accounts": ["moosavi.eruka@gmail.com", "xuylpino008+5@gmail.com"],
        })

        self.assertIn("WARNING: 2 saved ChatGPT accounts", message)
        self.assertIn("session operations apply only to the active email above", message)

    def test_describe_account_exposes_free_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with mock.patch.dict(os.environ, {"CODEX_MANAGER_HOME": f"{tmpdir}/manager"}, clear=False):
                paths = Paths()
                atomic_write_json(account_path(paths, "free"), {
                    "tokens": {
                        "refresh_token": "refresh-token",
                        "id_token": {
                            "email": "free@example.com",
                            "https://api.openai.com/auth": {"chatgpt_plan_type": "free"},
                        },
                    },
                })
                save_state(paths, {"active": None})

                row = describe_account(paths, "free", None)

        self.assertEqual("free", row["plan"])

    def test_needs_login_account_does_not_show_stale_token_plan_or_chrome_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with mock.patch.dict(os.environ, {"CODEX_MANAGER_HOME": f"{tmpdir}/manager"}, clear=False):
                paths = Paths()
                atomic_write_json(account_path(paths, "stale"), {
                    "tokens": {
                        "refresh_token": "refresh-token",
                        "id_token": {
                            "email": "stale@example.com",
                            "https://api.openai.com/auth": {"chatgpt_plan_type": "plus"},
                        },
                    },
                })
                atomic_write_json(status_path(paths, "stale"), {
                    "state": "needs_login",
                    "chrome_profile": {"directory": "Default", "display_name": "Default"},
                })
                save_state(paths, {"active": None})

                row = describe_account(paths, "stale", None)

        self.assertEqual("unknown", row["plan"])
        self.assertEqual("-", row["chrome_profile"])

    def test_verified_profile_mapping_removes_former_account_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with mock.patch.dict(os.environ, {"CODEX_MANAGER_HOME": f"{tmpdir}/manager"}, clear=False):
                paths = Paths()
                for name, email in (("former", "former@example.com"), ("active", "active@example.com")):
                    atomic_write_json(account_path(paths, name), {
                        "tokens": {"refresh_token": "refresh-token", "id_token": {"email": email}},
                    })
                atomic_write_json(status_path(paths, "former"), {
                    "chrome_profile": {"directory": "Default", "chrome_root": "/chrome"},
                })
                profile = ChromeProfile("google-chrome/Default", paths.manager_home / "Cookies", "Default", "Default", Path("/chrome"))

                mapped = cache_chrome_profile(paths, "active@example.com", profile, ["active@example.com"])

                former = read_json(status_path(paths, "former"))
                active = read_json(status_path(paths, "active"))

        self.assertEqual("active", mapped)
        self.assertNotIn("chrome_profile", former)
        self.assertEqual("active@example.com", active["chrome_profile"]["active_email"])

    def test_plus_account_without_session_monitor_is_an_alert(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with mock.patch.dict(os.environ, {"CODEX_MANAGER_HOME": f"{tmpdir}/manager"}, clear=False):
                paths = Paths()
                atomic_write_json(account_path(paths, "plus"), {
                    "tokens": {
                        "refresh_token": "refresh-token",
                        "id_token": {
                            "email": "plus@example.com",
                            "https://api.openai.com/auth": {"chatgpt_plan_type": "plus"},
                        },
                    },
                })
                atomic_write_json(paths.config_file, {
                    "session_monitor_enabled": True,
                    "session_monitor_interval": "10min",
                })
                save_state(paths, {"active": None})

                row = describe_account(paths, "plus", None)

        self.assertEqual("session alert", row["state"])
        self.assertIn("has not reported", row["reason"])

    def test_plus_account_with_session_monitor_error_is_an_alert(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with mock.patch.dict(os.environ, {"CODEX_MANAGER_HOME": f"{tmpdir}/manager"}, clear=False):
                paths = Paths()
                atomic_write_json(account_path(paths, "plus"), {
                    "tokens": {
                        "refresh_token": "refresh-token",
                        "id_token": {
                            "email": "plus@example.com",
                            "https://api.openai.com/auth": {"chatgpt_plan_type": "plus"},
                        },
                    },
                })
                atomic_write_json(paths.config_file, {
                    "session_monitor_enabled": True,
                    "session_monitor_interval": "10min",
                })
                atomic_write_json(status_path(paths, "plus"), {
                    "session_monitor": {
                        "outcome": "error",
                        "error": "ChatGPT sessions API request failed: TimeoutError",
                    },
                })
                save_state(paths, {"active": None})

                row = describe_account(paths, "plus", None)

        self.assertEqual("session alert", row["state"])
        self.assertIn("TimeoutError", row["reason"])

    def test_account_check_preserves_chrome_profile_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with mock.patch.dict(os.environ, {"CODEX_MANAGER_HOME": f"{tmpdir}/manager"}, clear=False):
                paths = Paths()
                atomic_write_json(status_path(paths, "account"), {
                    "chrome_profile": {
                        "directory": "Profile 8",
                        "display_name": "new-5",
                    },
                    "session_monitor": {"codex_sessions": 1, "revoked_total": 3},
                    "state": "ok",
                })

                write_status(paths, "account", "ok", "checked", rate_limits={"weekly": 64})

                status = read_json(status_path(paths, "account"))

        self.assertEqual("Profile 8", status["chrome_profile"]["directory"])
        self.assertEqual("new-5", status["chrome_profile"]["display_name"])
        self.assertEqual({"codex_sessions": 1, "revoked_total": 3}, status["session_monitor"])
        self.assertEqual({"weekly": 64}, status["rate_limits"])

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

    def test_monitor_never_revokes_current_codex_device(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with mock.patch.dict(os.environ, {"CODEX_MANAGER_HOME": f"{tmpdir}/manager"}, clear=False):
                paths = Paths()
                fake_client = mock.Mock()
                fake_client.devices.return_value = [
                    device(client_name="Codex", platform="linux", timestamp=20, session_id="current", current=True),
                    device(client_name="Codex", platform="linux", timestamp=10, session_id="old-linux"),
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
        self.assertEqual([mock.call("old-linux"), mock.call("windows")], fake_client.revoke.call_args_list)

    def test_monitor_records_but_does_not_revoke_when_session_monitor_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with mock.patch.dict(os.environ, {"CODEX_MANAGER_HOME": f"{tmpdir}/manager"}, clear=False):
                paths = Paths()
                profile = ChromeProfile("google-chrome/Default", paths.manager_home / "fake-Cookies")
                fake_client = mock.Mock()
                fake_client.devices.return_value = [
                    device(client_name="Codex", platform="linux", timestamp=10, session_id="keep"),
                    device(client_name="Codex", platform="linux", timestamp=20, session_id="extra"),
                ]
                with (
                    mock.patch("codex_manager.commands.sessions.discover_chrome_profiles", return_value=[profile]),
                    mock.patch("codex_manager.commands.sessions.load_chatgpt_cookies", return_value=object()),
                    mock.patch("codex_manager.commands.sessions.cache_chrome_profile", return_value="account"),
                    mock.patch("codex_manager.commands.sessions.session_monitor_is_disabled", return_value=True),
                    mock.patch("codex_manager.commands.sessions.ChatGPTSessionClient", return_value=fake_client),
                ):
                    summary = monitor_sessions(paths)

        self.assertEqual(0, summary["revoked"])
        self.assertTrue(summary["results"][0]["revocation_disabled"])
        self.assertEqual(2, summary["results"][0]["codex_sessions"])
        fake_client.revoke.assert_not_called()

    def test_monitor_records_unavailable_chatgpt_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with mock.patch.dict(os.environ, {"CODEX_MANAGER_HOME": f"{tmpdir}/manager"}, clear=False):
                paths = Paths()
                profile = ChromeProfile("google-chrome/Profile 8", paths.manager_home / "fake-Cookies")
                fake_client = mock.Mock()
                fake_client.devices.side_effect = ProfileNotSignedIn("not signed in to ChatGPT")
                with (
                    mock.patch("codex_manager.commands.sessions.discover_chrome_profiles", return_value=[profile]),
                    mock.patch("codex_manager.commands.sessions.load_chatgpt_cookies", return_value=object()),
                    mock.patch("codex_manager.commands.sessions.cache_chrome_profile", return_value="account"),
                    mock.patch("codex_manager.commands.sessions.ChatGPTSessionClient", return_value=fake_client),
                ):
                    summary = monitor_sessions(paths)

                status = read_json(status_path(paths, "account"))["session_monitor"]

        self.assertEqual(0, summary["failures"])
        self.assertEqual("unavailable", status["outcome"])
        self.assertIsNone(status["codex_sessions"])
        self.assertEqual(1, len(status["check_history"]))

    def test_monitor_records_network_failure_against_cached_profile_account(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with mock.patch.dict(os.environ, {"CODEX_MANAGER_HOME": f"{tmpdir}/manager"}, clear=False):
                paths = Paths()
                atomic_write_json(account_path(paths, "account"), {"tokens": {"id_token": {}}})
                atomic_write_json(status_path(paths, "account"), {
                    "chrome_profile": {"directory": "Profile 8", "chrome_root": None},
                })
                profile = ChromeProfile("google-chrome/Profile 8", paths.manager_home / "fake-Cookies", "Profile 8")
                fake_client = mock.Mock()
                fake_client.devices.side_effect = ManagerError("ChatGPT sessions API request failed: TimeoutError")
                with (
                    mock.patch("codex_manager.commands.sessions.discover_chrome_profiles", return_value=[profile]),
                    mock.patch("codex_manager.commands.sessions.load_chatgpt_cookies", return_value=object()),
                    mock.patch("codex_manager.commands.sessions.ChatGPTSessionClient", return_value=fake_client),
                ):
                    summary = monitor_sessions(paths)

                status = read_json(status_path(paths, "account"))["session_monitor"]

        self.assertEqual(1, summary["failures"])
        self.assertEqual("account", summary["results"][0]["account"])
        self.assertEqual("error", status["outcome"])
        self.assertEqual("ChatGPT sessions API request failed: TimeoutError", status["error"])
        self.assertIsNone(status["codex_sessions"])

    def test_cached_chrome_profile_account_uses_matching_profile_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with mock.patch.dict(os.environ, {"CODEX_MANAGER_HOME": f"{tmpdir}/manager"}, clear=False):
                paths = Paths()
                atomic_write_json(account_path(paths, "account"), {"tokens": {"id_token": {}}})
                atomic_write_json(status_path(paths, "account"), {
                    "chrome_profile": {"directory": "Profile 8", "chrome_root": "/chrome"},
                })
                profile = ChromeProfile("google-chrome/Profile 8", paths.manager_home / "fake-Cookies", "Profile 8", chrome_root=Path("/chrome"))

                account = cached_chrome_profile_account(paths, profile)

        self.assertEqual("account", account)

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
