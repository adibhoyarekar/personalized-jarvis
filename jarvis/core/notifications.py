"""Desktop notifications (Windows toasts) - best effort, never raises."""

from __future__ import annotations

import logging
import sys

log = logging.getLogger(__name__)


def show_toast(title: str, message: str, sound: bool = True) -> None:
    """Show a Windows toast notification. Does nothing on other systems or without winotify."""
    if sys.platform != "win32":
        log.info("Notification: %s - %s", title, message)
        return
    try:
        from winotify import Notification, audio

        toast = Notification(app_id="Jarvis", title=title, msg=message, duration="short")
        if sound:
            toast.set_audio(audio.Reminder, loop=False)
        toast.show()
    except Exception as exc:  # noqa: BLE001 - notifications are best effort
        log.warning("Toast notification failed: %s", exc)
