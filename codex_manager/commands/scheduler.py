from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from ..config import cron_expression, ensure_config
from ..errors import ManagerError
from ..paths import Paths
from ..system import run_command

CRON_MARKER = "# codex-manager-maintain"
MONITOR_CRON_MARKER = "# codex-manager-check"


def resolve_manager_bin() -> str:
    found = shutil.which("codex-manager")
    if found:
        return found
    candidate = Path(sys.argv[0]).expanduser()
    if candidate.exists():
        return str(candidate.resolve())
    return sys.argv[0]


def scheduler_paths(paths: Paths) -> tuple[Path, Path]:
    user_dir = paths.home / ".config/systemd/user"
    return user_dir / "codex-manager-maintain.service", user_dir / "codex-manager-maintain.timer"


def monitor_scheduler_paths(paths: Paths) -> tuple[Path, Path]:
    user_dir = paths.home / ".config/systemd/user"
    return user_dir / "codex-manager-check.service", user_dir / "codex-manager-check.timer"


def write_text_file(path: Path, content: str, mode: int = 0o644) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    os.chmod(path, mode)


def install_crontab(bin_path: str, interval: str, monitor_interval: str) -> str:
    schedule = cron_expression(interval)
    line = f"{schedule} {shlex.quote(bin_path)} maintain --quiet >/dev/null 2>&1 {CRON_MARKER}"
    monitor_schedule = cron_expression(monitor_interval)
    monitor_line = f"{monitor_schedule} {shlex.quote(bin_path)} check --quiet >/dev/null 2>&1 {MONITOR_CRON_MARKER}"
    current = subprocess.run(
        ["crontab", "-l"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        check=False,
    )
    existing = current.stdout if current.returncode == 0 else ""
    lines = [
        line
        for line in existing.splitlines()
        if CRON_MARKER not in line
        and MONITOR_CRON_MARKER not in line
        and "codex-manager maintain --quiet" not in line
        and "codex-manager check --quiet" not in line
    ]
    lines.append(line)
    lines.append(monitor_line)
    proc = subprocess.run(
        ["crontab", "-"],
        input="\n".join(lines) + "\n",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise ManagerError(f"failed to install crontab: {proc.stderr.strip()}")
    return f"crontab: {line}; {monitor_line}"


def apply_scheduler(paths: Paths, bin_path: str | None = None) -> str:
    config = ensure_config(paths)
    bin_path = bin_path or resolve_manager_bin()
    service_path, timer_path = scheduler_paths(paths)
    monitor_service_path, monitor_timer_path = monitor_scheduler_paths(paths)
    write_text_file(service_path, f"""[Unit]
Description=Codex Manager maintenance

[Service]
Type=oneshot
ExecStart={bin_path} maintain --quiet
""")
    write_text_file(timer_path, f"""[Unit]
Description=Run Codex Manager maintenance

[Timer]
OnBootSec=5min
OnUnitActiveSec={config["maintain_interval"]}
RandomizedDelaySec={config["randomized_delay"]}
Persistent=true
Unit=codex-manager-maintain.service

[Install]
WantedBy=timers.target
""")
    write_text_file(monitor_service_path, f"""[Unit]
Description=Codex Manager limit monitor

[Service]
Type=oneshot
ExecStart={bin_path} check --quiet
""")
    write_text_file(monitor_timer_path, f"""[Unit]
Description=Run Codex Manager usage check

[Timer]
OnBootSec=2min
OnUnitActiveSec={config["monitor_interval"]}
RandomizedDelaySec=0s
Persistent=true
Unit=codex-manager-check.service

[Install]
WantedBy=timers.target
""")

    if shutil.which("systemctl"):
        code, _ = run_command(["systemctl", "--user", "show-environment"], timeout=5)
        if code == 0:
            for command in (
                ["systemctl", "--user", "daemon-reload"],
                ["systemctl", "--user", "enable", "--now", "codex-manager-maintain.timer"],
                ["systemctl", "--user", "enable", "--now", "codex-manager-check.timer"],
            ):
                command_code, output = run_command(command, timeout=10)
                if command_code != 0:
                    raise ManagerError(f"{' '.join(command)} failed: {output}")
            return "systemd user timers: codex-manager-maintain.timer, codex-manager-check.timer"

    if shutil.which("crontab"):
        return install_crontab(bin_path, config["maintain_interval"], config["monitor_interval"])

    return "not installed; neither systemd user timers nor crontab are available"


def cmd_scheduler_apply(args) -> int:
    status = apply_scheduler(Paths(), args.bin)
    if args.quiet:
        print(status)
    else:
        print(f"scheduler applied: {status}")
    return 0
