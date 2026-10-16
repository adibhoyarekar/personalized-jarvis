"""Fires timers and reminders stored in the memory database.

Because reminders live in SQLite, they survive restarts: anything that came
due while Jarvis was off is announced at the next start as a missed reminder.
"""

from __future__ import annotations

import datetime as dt
import logging
import sys
import threading
from typing import Callable

from core.memory import Memory, Reminder

log = logging.getLogger(__name__)


def show_toast(title: str, message: str) -> None:
    """Windows toast notification (no-op elsewhere or if winotify is missing)."""
    if sys.platform != "win32":
        return
    try:
        from winotify import Notification, audio

        toast = Notification(app_id="Jarvis", title=title, msg=message, duration="long")
        toast.set_audio(audio.Reminder, loop=False)
        toast.show()
    except Exception as exc:  # noqa: BLE001 - notifications are best effort
        log.warning("Toast notification failed: %s", exc)


class ReminderScheduler:
    """Background thread that announces due reminders.

    Args:
        memory: The store holding reminders.
        announce: Speaks/prints a message (looked up on each fire).
        toast: Shows a desktop notification.
        poll_s: How often to check the database.
    """

    def __init__(
        self,
        memory: Memory,
        announce: Callable[[str], None],
        toast: Callable[[str, str], None] = show_toast,
        poll_s: float = 1.0,
    ) -> None:
        self.memory = memory
        self.announce = announce
        self.toast = toast
        self.poll_s = poll_s
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="reminders", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)

    @staticmethod
    def message_for(reminder: Reminder, now: dt.datetime) -> str:
        late = (now - reminder.due_at).total_seconds()
        if reminder.kind == "timer":
            label = "" if reminder.text.lower() in ("", "timer") else f" for {reminder.text}"
            body = f"Your timer{label} is done."
        else:
            body = f"Reminder: {reminder.text}."
        if late > 120:  # came due while Jarvis was off or busy
            body = f"Missed while I was away, due at {reminder.due_at.strftime('%I:%M %p').lstrip('0')}: " + body
        return body

    def check_once(self, now: dt.datetime | None = None) -> list[Reminder]:
        """Fire everything due now. Returns what fired (used by tests)."""
        now = now or dt.datetime.now()
        fired = []
        for reminder in self.memory.due_reminders(now):
            self.memory.mark_fired(reminder.id)  # mark first: never announce twice
            message = self.message_for(reminder, now)
            log.info("Firing %s %d: %s", reminder.kind, reminder.id, reminder.text)
            try:
                self.toast("Jarvis " + reminder.kind, message)
            except Exception:  # noqa: BLE001
                log.exception("Toast failed")
            try:
                self.announce(message)
            except Exception:  # noqa: BLE001
                log.exception("Announcing a reminder failed")
            fired.append(reminder)
        return fired

    def _run(self) -> None:
        while not self._stop.wait(self.poll_s):
            try:
                self.check_once()
            except Exception:  # noqa: BLE001 - keep the scheduler alive
                log.exception("Reminder check failed")
