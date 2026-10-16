"""Run-at-login registration for Windows: a Task Scheduler task, with a registry Run-key fallback.

The task starts ``pythonw.exe main.py --wake --tray`` (no console window),
30 seconds after you log in so audio devices are ready, from the project folder.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from xml.sax.saxutils import escape

from core.config import PROJECT_DIR

log = logging.getLogger(__name__)

TASK_NAME = "Jarvis Assistant"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "JarvisAssistant"
DEFAULT_ARGS = ["--wake", "--tray"]


def find_pythonw(project_dir: Path = PROJECT_DIR) -> Path:
    """The virtual environment's pythonw.exe (falls back to the current interpreter's)."""
    candidates = []
    if sys.prefix != sys.base_prefix:  # running inside a venv: use it
        candidates.append(Path(sys.executable).with_name("pythonw.exe"))
    for venv in (project_dir / ".venv", project_dir.parent / ".venv", project_dir / "venv"):
        candidates.append(venv / "Scripts" / "pythonw.exe")
    candidates.append(Path(sys.executable).with_name("pythonw.exe"))
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(
        "Couldn't find pythonw.exe next to this Python. Run this script with the Python "
        "you installed Jarvis's requirements into (see README)."
    )


def command_line(pythonw: Path, main_py: Path, args: list[str] | None = None) -> str:
    """Quoted command line for the Run key."""
    return " ".join([f'"{pythonw}"', f'"{main_py}"', *(args or DEFAULT_ARGS)])


def task_xml(pythonw: Path, main_py: Path, workdir: Path, user: str, args: list[str] | None = None,
             delay_s: int = 30) -> str:
    """Task Scheduler definition: at logon, hidden, no time limit, works on battery."""
    arguments = " ".join([f'"{main_py}"', *(args or DEFAULT_ARGS)])
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>Starts the Jarvis voice assistant when you log in.</Description>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>{escape(user)}</UserId>
      <Delay>PT{int(delay_s)}S</Delay>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{escape(user)}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>true</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{escape(str(pythonw))}</Command>
      <Arguments>{escape(arguments)}</Arguments>
      <WorkingDirectory>{escape(str(workdir))}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def current_user() -> str:
    domain = os.environ.get("USERDOMAIN", "")
    user = os.environ.get("USERNAME") or os.environ.get("USER") or "user"
    return f"{domain}\\{user}" if domain else user


def _schtasks(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["schtasks", *args], capture_output=True, text=True, timeout=30)  # noqa: S603, S607


def install(args: list[str] | None = None, use_task: bool = True) -> str:
    """Register Jarvis to start at login. Returns a description of what was done."""
    if sys.platform != "win32":
        raise OSError("Run-at-login registration is for Windows only")
    pythonw = find_pythonw()
    main_py = PROJECT_DIR / "main.py"
    if use_task:
        xml = task_xml(pythonw, main_py, PROJECT_DIR, current_user(), args)
        with tempfile.NamedTemporaryFile("w", suffix=".xml", delete=False, encoding="utf-16") as fh:
            fh.write(xml)
            xml_path = fh.name
        try:
            result = _schtasks("/Create", "/TN", TASK_NAME, "/XML", xml_path, "/F")
        finally:
            os.unlink(xml_path)
        if result.returncode == 0:
            _remove_run_key()  # don't start twice
            return f"Task Scheduler task '{TASK_NAME}' created: {pythonw} {main_py} {' '.join(args or DEFAULT_ARGS)}"
        log.warning("schtasks failed: %s", (result.stderr or result.stdout).strip())
    import winreg

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, RUN_VALUE, 0, winreg.REG_SZ, command_line(pythonw, main_py, args))
    return f"Registry Run key '{RUN_VALUE}' set (Task Scheduler wasn't available): {command_line(pythonw, main_py, args)}"


SHORTCUT_NAME = "Jarvis.lnk"


def _ps_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def shortcut_script(pythonw: Path, main_py: Path, workdir: Path, args: list[str] | None = None) -> str:
    """PowerShell that writes a desktop .lnk launching Jarvis hidden.

    The Desktop folder comes from Windows itself so OneDrive-redirected desktops work.
    """
    arguments = " ".join([f'"{main_py}"', *(args or DEFAULT_ARGS)])
    return "; ".join([
        "$desktop = [Environment]::GetFolderPath('Desktop')",
        f"$link = Join-Path $desktop {_ps_quote(SHORTCUT_NAME)}",
        "$s = (New-Object -ComObject WScript.Shell).CreateShortcut($link)",
        f"$s.TargetPath = {_ps_quote(str(pythonw))}",
        f"$s.Arguments = {_ps_quote(arguments)}",
        f"$s.WorkingDirectory = {_ps_quote(str(workdir))}",
        "$s.Description = 'Start the Jarvis voice assistant'",
        "$s.Save()",
        "Write-Output $link",
    ])


def create_desktop_shortcut(args: list[str] | None = None) -> str:
    """Put a Jarvis shortcut on the desktop. Returns the shortcut's path."""
    if sys.platform != "win32":
        raise OSError("Desktop shortcuts are for Windows only")
    script = shortcut_script(find_pythonw(), PROJECT_DIR / "main.py", PROJECT_DIR, args)
    result = subprocess.run(  # noqa: S603, S607
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        raise OSError(f"Couldn't create the shortcut: {(result.stderr or result.stdout).strip()}")
    return result.stdout.strip()


def _remove_run_key() -> bool:
    if sys.platform != "win32":
        return False
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, RUN_VALUE)
        return True
    except FileNotFoundError:
        return False


def uninstall() -> list[str]:
    """Remove both registrations. Returns what was removed."""
    if sys.platform != "win32":
        raise OSError("Run-at-login registration is for Windows only")
    removed = []
    if _schtasks("/Delete", "/TN", TASK_NAME, "/F").returncode == 0:
        removed.append(f"Task Scheduler task '{TASK_NAME}'")
    if _remove_run_key():
        removed.append(f"registry Run key '{RUN_VALUE}'")
    return removed


def status() -> str:
    if sys.platform != "win32":
        return "Not on Windows."
    lines = []
    query = _schtasks("/Query", "/TN", TASK_NAME, "/FO", "LIST")
    lines.append(f"Task '{TASK_NAME}': " + ("installed" if query.returncode == 0 else "not installed"))
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            value, _ = winreg.QueryValueEx(key, RUN_VALUE)
            lines.append(f"Run key: {value}")
    except FileNotFoundError:
        lines.append("Run key: not set")
    return "\n".join(lines)
