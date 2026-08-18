from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

from codex_manager.config import DEFAULT_CONFIG, reset_config
from codex_manager.paths import Paths
from codex_manager.storage import atomic_write_json, read_json


class ConfigResetTests(unittest.TestCase):
    def test_reset_config_replaces_existing_values_with_current_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with mock.patch.dict(os.environ, {"CODEX_MANAGER_HOME": f"{tmpdir}/manager"}, clear=False):
                paths = Paths()
                atomic_write_json(
                    paths.config_file,
                    {"proxy": "https://old.example", "maintain_interval": "1d", "unknown": "old"},
                )

                config = reset_config(paths)

                self.assertEqual(DEFAULT_CONFIG, config)
                self.assertEqual(DEFAULT_CONFIG, read_json(paths.config_file))
                self.assertTrue(paths.config_file.with_name("config.json.BAK1").exists())


if __name__ == "__main__":
    unittest.main()
