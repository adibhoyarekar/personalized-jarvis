"""Media control via the keyboard media keys, plus YouTube and Spotify shortcuts."""

from __future__ import annotations

import logging
import webbrowser
from typing import Literal
from urllib.parse import quote, quote_plus

from tools import tool
from tools._platform import IS_WINDOWS, open_with_default_handler

log = logging.getLogger(__name__)


def press(key: str, times: int = 1) -> None:
    """Press a media key (works for Spotify, YouTube in the browser, most players)."""
    try:
        import pyautogui
    except Exception as exc:  # noqa: BLE001 - ImportError, or no display
        raise RuntimeError(f"Media keys need pyautogui ({exc})") from exc
    pyautogui.press(key, presses=max(1, times), interval=0.02)


@tool
def play_pause() -> str:
    """Play or pause whatever media is playing (Spotify, YouTube, any player)."""
    press("playpause")
    return "Toggled play/pause."


@tool
def next_track() -> str:
    """Skip to the next track."""
    press("nexttrack")
    return "Skipped to the next track."


@tool
def previous_track() -> str:
    """Go back to the previous track."""
    press("prevtrack")
    return "Back to the previous track."


@tool
def media_volume(direction: Literal["up", "down", "mute"], steps: int = 5) -> str:
    """Nudge the volume with the media keys (each step is about 2 percent).

    For an exact level use set_volume instead.

    Args:
        direction: "up", "down" or "mute" (toggle).
        steps: How many key presses (1-25).
    """
    if direction == "mute":
        press("volumemute")
        return "Toggled mute."
    steps = max(1, min(int(steps), 25))
    press("volumeup" if direction == "up" else "volumedown", steps)
    return f"Volume {direction} {steps} steps."


@tool
def search_youtube(query: str) -> str:
    """Open YouTube search results for a query in the browser.

    Args:
        query: What to search for, e.g. "lofi beats" or "python tutorial".
    """
    webbrowser.open(f"https://www.youtube.com/results?search_query={quote_plus(query)}")
    return f"Opened YouTube results for {query}."


@tool
def open_spotify(search: str = "") -> str:
    """Open Spotify, optionally showing search results for a song, artist or playlist.

    Args:
        search: Optional search, e.g. "Arijit Singh" or "focus playlist".
    """
    uri = f"spotify:search:{quote(search)}" if search.strip() else "spotify:"
    try:
        open_with_default_handler(uri)
        return f"Opened Spotify{' and searched for ' + search if search.strip() else ''}."
    except OSError:
        log.info("Spotify app not registered; using the web player")
    web = f"https://open.spotify.com/search/{quote(search)}" if search.strip() else "https://open.spotify.com"
    webbrowser.open(web)
    note = "" if IS_WINDOWS else " (desktop app not found)"
    return f"Opened the Spotify web player{note}."
