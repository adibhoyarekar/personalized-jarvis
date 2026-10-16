"""Supervisor: restart after crashes, at most 5 per 10 minutes."""

from __future__ import annotations

import pytest

from core.supervisor import Supervisor


class Clock:
    def __init__(self) -> None:
        self.t = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.sleeps.append(s)
        self.t += s


def crashing(times: int, then_ok: bool = True):  # type: ignore[no-untyped-def]
    calls = {"n": 0}

    def target() -> None:
        calls["n"] += 1
        if calls["n"] <= times:
            raise RuntimeError(f"boom {calls['n']}")
        if not then_ok:
            raise RuntimeError("still broken")

    return target, calls


def test_recovers_after_a_few_crashes() -> None:
    clock = Clock()
    crashes: list[str] = []
    target, calls = crashing(3)
    sup = Supervisor(target, on_crash=lambda e: crashes.append(str(e)), clock=clock, sleep=clock.sleep)
    assert sup.run() is True
    assert calls["n"] == 4 and sup.restarts == 3
    assert crashes == ["boom 1", "boom 2", "boom 3"]
    assert clock.sleeps == [3.0, 3.0, 3.0]  # waits 3 s before each restart


def test_gives_up_after_5_restarts_in_10_minutes() -> None:
    clock = Clock()
    gave_up: list[str] = []
    target, calls = crashing(100)
    sup = Supervisor(target, on_give_up=lambda e: gave_up.append(str(e)), clock=clock, sleep=clock.sleep)
    assert sup.run() is False
    assert sup.restarts == 5 and calls["n"] == 6
    assert gave_up == ["boom 6"]


def test_old_crashes_age_out_of_the_window() -> None:
    clock = Clock()
    target, calls = crashing(12)

    def slow_sleep(s: float) -> None:  # 3 minutes between crashes: never 6 within 10 minutes
        clock.t += 180

    sup = Supervisor(target, clock=clock, sleep=slow_sleep)
    assert sup.run() is True
    assert sup.restarts == 12


def test_quit_during_the_restart_delay_stops_cleanly() -> None:
    clock = Clock()
    target, calls = crashing(10)
    sup = Supervisor(target, clock=clock, sleep=clock.sleep, should_stop=lambda: True)
    assert sup.run() is True and calls["n"] == 1


def test_ctrl_c_is_not_swallowed() -> None:
    def target() -> None:
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        Supervisor(target).run()
