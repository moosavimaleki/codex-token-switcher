from __future__ import annotations

import shutil
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


def copy_text_to_clipboard(text: str) -> bool:
    commands: list[list[str]] = []

    if shutil.which("wl-copy"):
        commands.append(["wl-copy"])
    if shutil.which("xclip"):
        commands.append(["xclip", "-selection", "clipboard"])
    if shutil.which("xsel"):
        commands.append(["xsel", "--clipboard", "--input"])
    if shutil.which("pbcopy"):
        commands.append(["pbcopy"])
    if shutil.which("clip.exe"):
        commands.append(["clip.exe"])

    for command in commands:
        try:
            subprocess.run(
                command,
                input=text,
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=5,
            )
            return True
        except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
            continue
    return False
