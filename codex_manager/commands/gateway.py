from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from ..config import ensure_config
from ..errors import ManagerError
from ..paths import Paths


def _gateway_binary() -> Path:
    installed = Path(__file__).resolve().parents[2] / "codex-manager-gateway"
    if installed.exists():
        return installed
    local = Path(__file__).resolve().parents[2] / "rust-gateway" / "target" / "release" / "codex-manager-gateway"
    if local.exists():
        return local
    found = shutil.which("codex-manager-gateway")
    if found:
        return Path(found)
    raise ManagerError("codex-manager-gateway is not installed; run ./setup.sh")


def cmd_gateway(args) -> int:
    paths = Paths()
    config = ensure_config(paths)
    env = os.environ.copy()
    env["CODEX_MANAGER_HOME"] = str(paths.manager_home)
    env["CODEX_MANAGER_GATEWAY_LISTEN"] = args.listen or config["gateway_listen"]
    env["CODEX_MANAGER_GATEWAY_API_KEY"] = args.api_key or config["gateway_api_key"]
    if config.get("proxy"):
        env["CODEX_MANAGER_PROXY"] = config["proxy"]
    if config.get("gateway_upstream"):
        env["CODEX_MANAGER_GATEWAY_UPSTREAM"] = config["gateway_upstream"]
    return subprocess.run([str(_gateway_binary())], env=env, check=False).returncode
