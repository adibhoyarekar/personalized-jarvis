"""System control tools: launching apps and reporting machine health."""

from __future__ import annotations

import logging
from pathlib import Path

import psutil

from tools import setting, tool
from tools._platform import launch

log = logging.getLogger(__name__)


def _resolve_app(name: str) -> str:
    """Map a spoken app name to a command using the ``tools.apps`` config."""
    apps: dict[str, str] = {k.lower(): v for k, v in (setting("tools.apps", {}) or {}).items()}
    key = name.strip().lower()
    for candidate in (key, key.removesuffix(".exe"), key.replace(" ", "")):
        if candidate in apps:
            return apps[candidate]
    return name.strip()


@tool
def open_app(name: str) -> str:
    """Open (launch) an application on the PC.

    Use for requests like "open Chrome", "launch Notepad" or "start Spotify".
    For websites use open_url instead.

    Args:
        name: The app's common name, e.g. "chrome", "notepad", "vs code", "calculator".
    """
    command = _resolve_app(name)
    launched = launch(command)
    log.info("Launched %s via %s", name, launched)
    return f"Opened {name}."


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
    return stats
