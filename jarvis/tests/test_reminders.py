"""Reminders and timers persist in SQLite and fire after a restart."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from core.memory import Memory
from core.scheduler import ReminderScheduler
from tools import ToolContext, set_context


def scheduler(memory: Memory) -> tuple[ReminderScheduler, list[str], list[str]]:
    spoken: list[str] = []
    toasts: list[str] = []
    return ReminderScheduler(memory, spoken.append, toast=lambda t, m: toasts.append(m)), spoken, toasts


def test_reminder_survives_restart(tmp_path: Path) -> None:
    db = tmp_path / "jarvis.db"
    memory = Memory(db)
    set_context(ToolContext(memory=memory))
    from tools import productivity_tools as p

    result = p.set_reminder("call mom", in_minutes=30)
    assert "call mom" in result
    memory.close()  # Jarvis quits

    restarted = Memory(db)  # ...and starts again
    pending = restarted.pending_reminders()
    assert [r.text for r in pending] == ["call mom"]

    sched, spoken, toasts = scheduler(restarted)
    assert sched.check_once(dt.datetime.now()) == []  # not due yet
    fired = sched.check_once(dt.datetime.now() + dt.timedelta(minutes=31))
    assert [r.text for r in fired] == ["call mom"] and spoken == ["Reminder: call mom."] and toasts
    assert sched.check_once(dt.datetime.now() + dt.timedelta(minutes=40)) == []  # never twice
    assert Memory(db).pending_reminders() == []


def test_missed_reminder_is_flagged(tmp_path: Path) -> None:
    memory = Memory(tmp_path / "jarvis.db")
    memory.add_reminder("stand-up meeting", dt.datetime.now() - dt.timedelta(hours=2))
    sched, spoken, _ = scheduler(memory)
    sched.check_once()
    assert spoken and spoken[0].startswith("Missed while I was away")


def test_timer_and_cancel(tmp_path: Path) -> None:
    memory = Memory(tmp_path / "jarvis.db")
    set_context(ToolContext(memory=memory))
    from tools import productivity_tools as p

    assert "pasta" in p.set_timer(minutes=0, seconds=5, label="pasta")
    p.set_reminder("water plants", in_minutes=60)
    assert "water plants" in p.cancel_reminder("water plants")
    sched, spoken, _ = scheduler(memory)
    sched.check_once(dt.datetime.now() + dt.timedelta(seconds=6))
    assert spoken == ["Your timer for pasta is done."]
    assert memory.pending_reminders() == []


def test_set_reminder_time_parsing(tmp_path: Path) -> None:
    memory = Memory(tmp_path / "jarvis.db")
    set_context(ToolContext(memory=memory))
    from tools import productivity_tools as p

    tomorrow = (dt.datetime.now() + dt.timedelta(days=1)).replace(hour=9, minute=30, second=0, microsecond=0)
    assert "tomorrow" in p.set_reminder("gym", at=tomorrow.strftime("%Y-%m-%d %H:%M"))
    p.set_reminder("late check", at="23:59" if dt.datetime.now().strftime("%H:%M") < "23:59" else "00:01")
    with pytest.raises(ValueError):
        p.set_reminder("past", at="2001-01-01 10:00")
    with pytest.raises(ValueError):
        p.set_reminder("when?")
    assert len(memory.pending_reminders()) == 2
