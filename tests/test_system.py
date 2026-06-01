from __future__ import annotations

import subprocess
import tempfile
import unittest
from unittest import mock

from codex_manager.errors import ManagerError
from codex_manager.paths import Paths, ensure_dirs, status_path
from codex_manager.storage import atomic_write_json
from codex_manager.system import copy_text_to_clipboard
from codex_manager.textual_ui import latest_account_refresh, run_check_command


class ClipboardTests(unittest.TestCase):
    def test_copy_text_to_clipboard_uses_first_available_command(self) -> None:
        with (
            mock.patch("codex_manager.system.shutil.which") as which,
            mock.patch("codex_manager.system.subprocess.run") as run,
        ):
            which.side_effect = lambda command: "/usr/bin/xclip" if command == "xclip" else None
            run.return_value = subprocess.CompletedProcess(["xclip"], 0)

            copied = copy_text_to_clipboard("ABCD-EFGH")

        self.assertTrue(copied)
        run.assert_called_once_with(
            ["xclip", "-selection", "clipboard"],
            input="ABCD-EFGH",
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5,
        )

    def test_copy_text_to_clipboard_returns_false_without_supported_commands(self) -> None:
        with mock.patch("codex_manager.system.shutil.which", return_value=None):
            self.assertFalse(copy_text_to_clipboard("ABCD-EFGH"))


class RefreshSummaryTests(unittest.TestCase):
    def test_latest_account_refresh_returns_newest_status_timestamp(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with mock.patch.dict(
                "os.environ",
                {
                    "CODEX_HOME": f"{tmpdir}/codex",
                    "CODEX_MANAGER_HOME": f"{tmpdir}/manager",
                },
                clear=False,
            ):
                paths = Paths()
                ensure_dirs(paths)
                (paths.accounts_dir / "main.json").write_text("{}", encoding="utf-8")
                (paths.accounts_dir / "backup.json").write_text("{}", encoding="utf-8")
                atomic_write_json(status_path(paths, "main"), {"last_checked_at": "2026-05-28T10:00:00Z"})
                atomic_write_json(status_path(paths, "backup"), {"last_checked_at": "2026-05-28T12:30:00Z"})

                self.assertEqual("2026-05-28T12:30:00+00:00", latest_account_refresh(paths))


class CheckCommandTests(unittest.TestCase):
    def test_run_check_command_executes_cli_with_manager_environment(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with mock.patch.dict(
                "os.environ",
                {
                    "CODEX_HOME": f"{tmpdir}/codex",
                    "CODEX_MANAGER_HOME": f"{tmpdir}/manager",
                },
                clear=False,
            ):
                paths = Paths()

                with mock.patch("codex_manager.textual_ui.subprocess.run") as run:
                    run.return_value = subprocess.CompletedProcess(["python"], 0, "", "")

                    result = run_check_command(paths)

                self.assertEqual({"returncode": 0}, result)
                command = run.call_args.args[0]
                env = run.call_args.kwargs["env"]
                self.assertIn("main(['check', '--quiet'])", command[-1])
                self.assertEqual(str(paths.codex_home), env["CODEX_HOME"])
                self.assertEqual(str(paths.manager_home), env["CODEX_MANAGER_HOME"])
                self.assertEqual(str(paths.codex_auth), env["CODEX_AUTH_PATH"])

    def test_run_check_command_raises_on_cli_error_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with mock.patch.dict(
                "os.environ",
                {
                    "CODEX_HOME": f"{tmpdir}/codex",
                    "CODEX_MANAGER_HOME": f"{tmpdir}/manager",
                },
                clear=False,
            ):
                paths = Paths()

                with mock.patch("codex_manager.textual_ui.subprocess.run") as run:
                    run.return_value = subprocess.CompletedProcess(["python"], 1, "", "codex-manager: failed")

                    with self.assertRaisesRegex(ManagerError, "failed"):
                        run_check_command(paths)
