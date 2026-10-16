"""Timers, reminders, todos and the daily briefing.

Timers and reminders are stored in SQLite and fired by
:class:`core.scheduler.ReminderScheduler`, so they survive restarts.
"""

from __future__ import annotations

import datetime as dt
import logging

from tools import get_memory, setting, tool

log = logging.getLogger(__name__)


def _spoken_time(when: dt.datetime, now: dt.datetime | None = None) -> str:
    now = now or dt.datetime.now()
    clock = when.strftime("%I:%M %p").lstrip("0")
    if when.date() == now.date():
        return clock
    if when.date() == (now + dt.timedelta(days=1)).date():
        return f"tomorrow at {clock}"
    return f"{when.strftime('%A %d %B')} at {clock}"


def _duration(seconds: float) -> str:
    seconds = int(round(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    parts = [f"{h} hour{'s' * (h != 1)}" if h else "", f"{m} minute{'s' * (m != 1)}" if m else "",
             f"{s} second{'s' * (s != 1)}" if s and not h else ""]
    return " ".join(p for p in parts if p) or "0 seconds"


@tool
def set_timer(minutes: float = 0, seconds: int = 0, label: str = "") -> str:
    """Start a countdown timer. Jarvis announces it out loud (and with a notification) when it ends.

    Args:
        minutes: Minutes on the timer (may be fractional, e.g. 1.5).
        seconds: Extra seconds.
        label: Optional name, e.g. "pasta".
    """
    total = float(minutes) * 60 + int(seconds)
    if total < 1:
        raise ValueError("The timer needs a duration")
    if total > 24 * 3600:
        raise ValueError("Timers can be at most 24 hours; use a reminder instead")
    due = dt.datetime.now() + dt.timedelta(seconds=total)
    rid = get_memory().add_reminder(label.strip() or "timer", due, kind="timer")
    name = f" for {label.strip()}" if label.strip() else ""
    return f"Timer {rid}{name} set for {_duration(total)}, ending at {_spoken_time(due)}."


@tool
def set_reminder(text: str, at: str = "", in_minutes: float = 0) -> str:
    """Remind the user about something at a time. Persists across restarts.

    Give either `at` (a local date/time you computed from the current time in
    your instructions) or `in_minutes`.

    Args:
        text: What to remind about, e.g. "call mom".
        at: Local time as "YYYY-MM-DD HH:MM" (or "HH:MM" for today), e.g. "2026-10-16 17:30".
        in_minutes: Alternatively, minutes from now.
    """
    now = dt.datetime.now()
    if in_minutes:
        due = now + dt.timedelta(minutes=float(in_minutes))
    elif at.strip():
        raw = at.strip().replace("T", " ")
        try:
            due = dt.datetime.fromisoformat(raw)
        except ValueError:
            try:
                clock = dt.time.fromisoformat(raw)
            except ValueError as exc:
                raise ValueError("Give the time as 'YYYY-MM-DD HH:MM' or 'HH:MM'") from exc
            due = dt.datetime.combine(now.date(), clock)
            if due <= now:
                due += dt.timedelta(days=1)
        if due.tzinfo is not None:
            due = due.astimezone().replace(tzinfo=None)
    else:
        raise ValueError("When should I remind you? Give a time or a number of minutes")
    if due <= now:
        raise ValueError(f"{_spoken_time(due)} is already in the past")
    rid = get_memory().add_reminder(text, due, kind="reminder")
    return f"Reminder {rid} set: {text}, {_spoken_time(due)}."


@tool
def list_reminders() -> dict:
    """List pending reminders and running timers."""
    now = dt.datetime.now()
    items = [
        {"id": r.id, "kind": r.kind, "text": r.text, "when": _spoken_time(r.due_at, now)}
        for r in get_memory().pending_reminders()
    ]
    return {"pending": items} if items else {"pending": "No reminders or timers."}


@tool
def cancel_reminder(reminder: str) -> str:
    """Cancel a pending reminder or timer by its number or by words from its text.

    Args:
        reminder: The reminder/timer number, or words from it like "call mom".
    """
    memory = get_memory()
    pending = memory.pending_reminders()
    target = None
    if reminder.strip().isdigit():
        target = next((r for r in pending if r.id == int(reminder)), None)
    else:
        words = reminder.lower().split()
        target = next((r for r in pending if all(w in r.text.lower() for w in words)), None)
    if target is None or not memory.cancel_reminder(target.id):
        return f"I couldn't find a pending reminder matching '{reminder}'."
    return f"Cancelled the {target.kind} '{target.text}'."


# ---------------------------------------------------------------------------
# Todos
# ---------------------------------------------------------------------------


@tool
def add_todo(text: str) -> str:
    """Add an item to the to-do list.

    Args:
        text: The task, e.g. "renew passport".
    """
    if not text.strip():
        raise ValueError("The to-do is empty")
    get_memory().add_todo(text)
    count = len(get_memory().todos())
    return f"Added. You have {count} open to-do{'s' * (count != 1)}."


@tool
def read_todos(include_done: bool = False) -> dict:
    """Read the to-do list (open items, numbered).

    Args:
        include_done: Also include completed items.
    """
    items = get_memory().todos(include_done=include_done)
    if not items:
        return {"todos": "The to-do list is empty."}
    return {
        "todos": [
            {"number": i, "text": t["text"], "done": bool(t["done_at"])} for i, t in enumerate(items, start=1)
        ]
    }


@tool
def complete_todo(item: str) -> str:
    """Mark a to-do as done, by its number from read_todos or by words from it.

    Args:
        item: The number ("2") or part of the text ("passport").
    """
    done = get_memory().complete_todo(item)
    if done is None:
        return f"I couldn't find an open to-do matching '{item}'."
    return f"Marked '{done['text']}' as done."


# ---------------------------------------------------------------------------
# Daily briefing
# ---------------------------------------------------------------------------


@tool
def daily_briefing() -> dict:
    """Morning briefing: date and time, weather, top headlines, open to-dos and today's reminders.

    Speak it as a flowing summary in under 30 seconds (about 75 words): no lists,
    round the numbers, mention only the 3 headlines' gist.
    """
    include = set(setting("briefing.include", ["weather", "news", "todos", "reminders"]) or [])
    now = dt.datetime.now()
    brief: dict = {"now": now.strftime("%A %d %B, %I:%M %p")}
    if "weather" in include:
        try:
            from tools.web_tools import weather_report

            w = weather_report()
            brief["weather"] = {"place": w["place"], "now": w["now"], "today": w["today"]}
        except Exception as exc:  # noqa: BLE001 - one failing section must not sink the briefing
            brief["weather"] = f"unavailable ({exc})"
    if "news" in include:
        try:
            from tools.web_tools import headlines

            brief["headlines"] = [h["title"] for h in headlines(3)]
        except Exception as exc:  # noqa: BLE001
            brief["headlines"] = f"unavailable ({exc})"
    memory = get_memory()
    if "todos" in include:
        todos = memory.todos()
        brief["todos"] = {"open": len(todos), "first": [t["text"] for t in todos[:3]]}
    if "reminders" in include:
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        today = memory.reminders_between(now, start + dt.timedelta(days=1))
        brief["reminders_today"] = [r.describe() for r in today if r.kind == "reminder"] or "none"
    brief["instructions"] = "Speak this in under 30 seconds as natural sentences."
    return brief
