from __future__ import annotations

import subprocess


def run_command(command: list[str], timeout: int = 5) -> tuple[int | None, str]:
    try:
        result = subprocess.run(
            command,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout,
        )
        return result.returncode, result.stdout.rstrip()
    except FileNotFoundError:
        return None, f"{command[0]} not found"
    except subprocess.TimeoutExpired:
        return None, f"{' '.join(command)} timed out after {timeout}s"
