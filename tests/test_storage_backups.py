from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from codex_manager.paths import Paths, account_path, ensure_dirs
from codex_manager.storage import atomic_write_json, read_json


class ManagerBackupTests(unittest.TestCase):
    def test_manager_account_writes_keep_last_five_backups(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with mock.patch.dict(
                os.environ,
                {
                    "CODEX_HOME": f"{tmpdir}/codex",
                    "CODEX_MANAGER_HOME": f"{tmpdir}/manager",
                },
                clear=False,
            ):
                paths = Paths()
                ensure_dirs(paths)
                target = account_path(paths, "main")

                for version in range(7):
                    atomic_write_json(target, {"version": version, "tokens": {"refresh_token": f"r{version}"}})

                self.assertEqual(6, read_json(target)["version"])
                self.assertEqual(5, read_json(Path(f"{target}.BAK1"))["version"])
                self.assertEqual(4, read_json(Path(f"{target}.BAK2"))["version"])
                self.assertEqual(3, read_json(Path(f"{target}.BAK3"))["version"])
                self.assertEqual(2, read_json(Path(f"{target}.BAK4"))["version"])
                self.assertEqual(1, read_json(Path(f"{target}.BAK5"))["version"])
                self.assertFalse(Path(f"{target}.BAK6").exists())

    def test_live_codex_auth_write_does_not_create_manager_backup(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with mock.patch.dict(
                os.environ,
                {
                    "CODEX_HOME": f"{tmpdir}/codex",
                    "CODEX_MANAGER_HOME": f"{tmpdir}/manager",
                },
                clear=False,
            ):
                paths = Paths()
                ensure_dirs(paths)

                atomic_write_json(paths.codex_auth, {"tokens": {"refresh_token": "r1"}})
                atomic_write_json(paths.codex_auth, {"tokens": {"refresh_token": "r2"}})

                self.assertFalse(Path(f"{paths.codex_auth}.BAK1").exists())


if __name__ == "__main__":
    unittest.main()
