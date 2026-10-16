"""System control: apps, volume, brightness, lock, screenshots, stats, shutdown."""

from __future__ import annotations

import datetime as dt
import difflib
import logging
import os
import subprocess
import time
from pathlib import Path

import psutil

from core.config import resolve_path
from tools import setting, tool
from tools._platform import IS_WINDOWS, launch, open_with_default_handler, require_windows

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Apps
# ---------------------------------------------------------------------------


def app_aliases() -> dict[str, str]:
    """Top-level ``apps`` from config (falls back to the Phase 1 ``tools.apps``)."""
    apps = setting("apps") or setting("tools.apps") or {}
    return {str(k).lower(): str(v) for k, v in apps.items()}


_SHORTCUTS: tuple[float, dict[str, Path]] = (0.0, {})


def start_menu_shortcuts() -> dict[str, Path]:
    """Start Menu .lnk shortcuts by lower-case name (cached for 10 minutes)."""
    global _SHORTCUTS
    stamp, cached = _SHORTCUTS
    if cached and time.monotonic() - stamp < 600:
        return cached
    found: dict[str, Path] = {}
    roots = [os.environ.get("PROGRAMDATA", ""), os.environ.get("APPDATA", "")]
    for base in filter(None, roots):
        folder = Path(base) / "Microsoft" / "Windows" / "Start Menu" / "Programs"
        if not folder.is_dir():
            continue
        for lnk in folder.rglob("*.lnk"):
            name = lnk.stem.lower()
            if "uninstall" in name or "readme" in name:
                continue
            found.setdefault(name, lnk)
    _SHORTCUTS = (time.monotonic(), found)
    return found


def match_shortcut(name: str, shortcuts: dict[str, Path]) -> Path | None:
    """Best Start Menu match for a spoken app name ("word" -> "Word", "vs code" -> "Visual Studio Code")."""
    key = name.strip().lower()
    if key in shortcuts:
        return shortcuts[key]
    starts = sorted(n for n in shortcuts if n.startswith(key))
    if starts:
        return shortcuts[min(starts, key=len)]
    words = sorted((n for n in shortcuts if key in n.split()), key=len)
    if words:
        return shortcuts[words[0]]
    close = difflib.get_close_matches(key, list(shortcuts), n=1, cutoff=0.6)
    return shortcuts[close[0]] if close else None


@tool
def open_app(name: str) -> str:
    """Open (launch) an application on the PC.

    Use for "open Chrome", "launch Notepad", "start Excel". For websites use
    open_url; for coding projects use open_project.

    Args:
        name: The app's common name, e.g. "chrome", "notepad", "vs code", "excel", "calculator".
    """
    key = name.strip().lower()
    aliases = app_aliases()
    for candidate in (key, key.removesuffix(".exe"), key.replace(" ", "")):
        if candidate in aliases:
            launched = launch(aliases[candidate])
            log.info("Launched %s via alias %s", name, launched)
            return f"Opened {name}."
    if IS_WINDOWS:
        shortcut = match_shortcut(key, start_menu_shortcuts())
        if shortcut is not None:
            open_with_default_handler(str(shortcut))
            return f"Opened {shortcut.stem}."
    launch(name.strip())
    return f"Opened {name}."


#: Never closed, whatever the user asks.
PROTECTED_PROCESSES = {
    "system", "system idle process", "csrss.exe", "wininit.exe", "winlogon.exe", "services.exe", "lsass.exe",
    "smss.exe", "svchost.exe", "dwm.exe", "explorer.exe", "fontdrvhost.exe", "registry", "memory compression",
}


def find_processes(name: str) -> list[psutil.Process]:
    """Processes whose executable matches the app name or its configured alias."""
    key = name.strip().lower()
    targets = {key, key + ".exe"}
    alias = app_aliases().get(key)
    if alias:
        exe = Path(alias).name.lower()
        targets |= {exe, exe if exe.endswith(".exe") else exe + ".exe"}
    me = os.getpid()
    found = []
    for proc in psutil.process_iter(["pid", "name"]):
        pname = (proc.info.get("name") or "").lower()
        if proc.info["pid"] == me or pname in PROTECTED_PROCESSES:
            continue
        if pname in targets or pname.removesuffix(".exe") == key.replace(" ", ""):
            found.append(proc)
    return found


@tool(dangerous=True, summary="close {name}. Unsaved work in it could be lost")
def close_app(name: str) -> str:
    """Close a running application (all of its windows).

    Args:
        name: The app's name, e.g. "chrome", "notepad", "spotify".
    """
    procs = find_processes(name)
    if not procs:
        return f"{name} doesn't seem to be running."
    for proc in procs:
        try:
            proc.terminate()
        except psutil.Error:
            pass
    _gone, alive = psutil.wait_procs(procs, timeout=3)
    for proc in alive:
        try:
            proc.kill()
        except psutil.Error:
            pass
    return f"Closed {name} ({len(procs)} process{'es' if len(procs) != 1 else ''})."


# ---------------------------------------------------------------------------
# Volume, brightness, lock
# ---------------------------------------------------------------------------


def _endpoint_volume():  # type: ignore[no-untyped-def]
    """pycaw IAudioEndpointVolume for the default speakers."""
    require_windows("Volume control")
    try:
        import comtypes
        from pycaw.pycaw import AudioUtilities
    except ImportError as exc:
        raise RuntimeError("Volume control needs pycaw: pip install pycaw") from exc
    comtypes.CoInitialize()  # tools run on worker threads
    speakers = AudioUtilities.GetSpeakers()
    volume = getattr(speakers, "EndpointVolume", None)
    if volume is not None:  # pycaw >= 20240210
        return volume
    from ctypes import POINTER, cast

    from comtypes import CLSCTX_ALL
    from pycaw.pycaw import IAudioEndpointVolume

    interface = speakers.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
    return cast(interface, POINTER(IAudioEndpointVolume))


@tool
def set_volume(level: int) -> str:
    """Set the system (master) volume.

    Args:
        level: Volume from 0 to 100.
    """
    level = max(0, min(int(level), 100))
    volume = _endpoint_volume()
    volume.SetMasterVolumeLevelScalar(level / 100.0, None)
    if level > 0:
        volume.SetMute(0, None)
    return f"Volume set to {level} percent."


@tool
def get_volume() -> str:
    """Report the current system volume and whether it is muted."""
    volume = _endpoint_volume()
    level = round(volume.GetMasterVolumeLevelScalar() * 100)
    return f"Volume is {level} percent{' (muted)' if volume.GetMute() else ''}."


@tool
def mute() -> str:
    """Mute all system sound."""
    _endpoint_volume().SetMute(1, None)
    return "Muted."


@tool
def unmute() -> str:
    """Unmute system sound."""
    _endpoint_volume().SetMute(0, None)
    return "Sound is back on."


@tool
def set_brightness(level: int) -> str:
    """Set the screen brightness (laptop screens and monitors that support it).

    Args:
        level: Brightness from 0 to 100.
    """
    level = max(0, min(int(level), 100))
    try:
        import screen_brightness_control as sbc
    except ImportError as exc:
        raise RuntimeError("Brightness control needs screen-brightness-control") from exc
    try:
        sbc.set_brightness(level)
    except Exception as exc:  # noqa: BLE001 - library raises its own error types
        raise RuntimeError(f"This display doesn't allow brightness changes ({exc})") from exc
    return f"Brightness set to {level} percent."


@tool
def lock_pc() -> str:
    """Lock the computer (shows the Windows lock screen)."""
    require_windows("Locking the PC")
    import ctypes

    if not ctypes.windll.user32.LockWorkStation():  # type: ignore[attr-defined]
        raise RuntimeError("Windows refused to lock the workstation")
    return "Locked."


# ---------------------------------------------------------------------------
# Stats and screenshots
# ---------------------------------------------------------------------------


@tool
def system_stats() -> dict:
    """Report current CPU usage, memory use, disk space and battery level.

    Use when asked how the computer is doing, about performance, battery, RAM or storage.
    """
    mem = psutil.virtual_memory()
    disk = psutil.disk_usage(Path.home().anchor or "/")  # the system drive
    stats: dict = {
        "cpu_percent": psutil.cpu_percent(interval=0.5),
        "cpu_cores": psutil.cpu_count(logical=True),
        "memory_percent": mem.percent,
        "memory_used_gb": round(mem.used / 1e9, 1),
        "memory_total_gb": round(mem.total / 1e9, 1),
        "disk_percent": disk.percent,
        "disk_free_gb": round(disk.free / 1e9, 1),
    }
    battery = psutil.sensors_battery() if hasattr(psutil, "sensors_battery") else None
    if battery is not None:
        stats["battery_percent"] = round(battery.percent)
        stats["plugged_in"] = bool(battery.power_plugged)
        if battery.secsleft not in (psutil.POWER_TIME_UNLIMITED, psutil.POWER_TIME_UNKNOWN):
            stats["battery_minutes_left"] = int(battery.secsleft // 60)
    else:
        stats["battery"] = "no battery detected"
    top = sorted(
        (p.info for p in psutil.process_iter(["name", "memory_percent"]) if p.info.get("memory_percent")),
        key=lambda i: i["memory_percent"],
        reverse=True,
    )[:3]
    stats["top_memory_apps"] = [f"{i['name']} ({i['memory_percent']:.0f}%)" for i in top]
    return stats


@tool
def screenshot() -> str:
    """Take a screenshot of all screens and save it as a PNG. Returns where it was saved."""
    try:
        from PIL import ImageGrab
    except ImportError as exc:
        raise RuntimeError("Screenshots need Pillow: pip install Pillow") from exc
    folder = resolve_path(str(setting("screenshots_dir", "~/Pictures/Jarvis Screenshots")), Path.home())
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"screenshot-{dt.datetime.now():%Y%m%d-%H%M%S}.png"
    image = ImageGrab.grab(all_screens=True) if IS_WINDOWS else ImageGrab.grab()
    image.save(path)
    return f"Saved screenshot to {path}"


# ---------------------------------------------------------------------------
# Shutdown / restart (30 s grace period, cancellable)
# ---------------------------------------------------------------------------

POWER_DELAY_S = 30


def _shutdown_cmd(*args: str) -> None:
    require_windows("Shutdown and restart")
    proc = subprocess.run(["shutdown", *args], capture_output=True, text=True, timeout=10)  # noqa: S603, S607
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "shutdown failed").strip())


@tool(dangerous=True, summary="shut down the computer in 30 seconds")
def shutdown_pc() -> str:
    """Shut down the computer after a 30-second warning. cancel_shutdown stops it."""
    _shutdown_cmd("/s", "/t", str(POWER_DELAY_S), "/c", "Jarvis: shutting down. Say 'cancel shutdown' to stop.")
    return f"Shutting down in {POWER_DELAY_S} seconds. Say 'cancel shutdown' to stop it."


@tool(dangerous=True, summary="restart the computer in 30 seconds")
def restart_pc() -> str:
    """Restart the computer after a 30-second warning. cancel_shutdown stops it."""
    _shutdown_cmd("/r", "/t", str(POWER_DELAY_S), "/c", "Jarvis: restarting. Say 'cancel shutdown' to stop.")
    return f"Restarting in {POWER_DELAY_S} seconds. Say 'cancel shutdown' to stop it."


@tool
def cancel_shutdown() -> str:
    """Cancel a pending shutdown or restart."""
    try:
        _shutdown_cmd("/a")
    except RuntimeError as exc:
        if "1116" in str(exc) or "not in progress" in str(exc).lower():
            return "There was no shutdown pending."
        raise
    return "Shutdown cancelled."
