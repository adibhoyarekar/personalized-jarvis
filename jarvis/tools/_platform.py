"""Small OS helpers shared by tool modules (not itself a tool module)."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

IS_WINDOWS = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"

# Characters cmd.exe would interpret; never let them reach `cmd /c start`.
_CMD_META = set('&|<>^"%\n\r')


def require_windows(feature: str) -> None:
    """Raise a clear error when a Windows-only feature is used elsewhere."""
    if not IS_WINDOWS:
        raise RuntimeError(f"{feature} is only supported on Windows")


def is_safe_shell_token(text: str) -> bool:
    """True if ``text`` has no cmd.exe metacharacters."""
    return not any(ch in _CMD_META for ch in text)


def open_with_default_handler(target: str) -> None:
    """Open a file, folder or URL with the OS default handler."""
    if IS_WINDOWS:
        os.startfile(target)  # type: ignore[attr-defined]  # noqa: S606
    elif IS_MAC:
        subprocess.Popen(["open", target])  # noqa: S603, S607
    else:
        subprocess.Popen(["xdg-open", target], start_new_session=True)  # noqa: S603, S607


def launch(command: str) -> str:
    """Start a program by executable name or path without waiting for it.

    On Windows this goes through ``start``, which also finds apps registered
    under "App Paths" (chrome, msedge, spotify ...), like the Run dialog does.

    Returns:
        The command that was launched.

    Raises:
        FileNotFoundError: If the program cannot be found.
        ValueError: If the command contains shell metacharacters.
    """
    if not is_safe_shell_token(command):
        raise ValueError(f"Refusing to launch suspicious command: {command!r}")
    path = Path(os.path.expandvars(os.path.expanduser(command)))
    if path.exists():
        open_with_default_handler(str(path))
        return str(path)
    resolved = shutil.which(command)
    if IS_WINDOWS:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        if resolved:
            subprocess.Popen([resolved], creationflags=subprocess.DETACHED_PROCESS)  # noqa: S603
            return resolved
        # `start "" name` resolves App Paths registrations; it returns 0 even
        # when Windows then shows a "cannot find" dialog, so check what we can.
        proc = subprocess.run(  # noqa: S603
            ["cmd", "/c", "start", "", command],
            capture_output=True,
            text=True,
            timeout=10,
            creationflags=flags,
        )
        if proc.returncode != 0:
            raise FileNotFoundError(f"Windows could not start '{command}'")
        return command
    if not resolved:
        raise FileNotFoundError(f"No program named '{command}' found on PATH")
    subprocess.Popen([resolved], start_new_session=True)  # noqa: S603
    return resolved
