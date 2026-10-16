"""Single-instance lock: a second Jarvis must not start; a dead one must not block."""

from __future__ import annotations

import subprocess
import sys
import textwrap
import time
from pathlib import Path

from core.single_instance import SingleInstance


def test_second_lock_is_refused_until_released(tmp_path: Path) -> None:
    first, second = SingleInstance(tmp_path), SingleInstance(tmp_path)
    assert first.acquire()
    assert not second.acquire()
    assert second.other_pid() is not None
    first.release()
    assert second.acquire()
    second.release()


def test_wait_for_release(tmp_path: Path) -> None:
    first, second = SingleInstance(tmp_path), SingleInstance(tmp_path)
    assert first.acquire()
    start = time.monotonic()
    assert not second.acquire(wait_s=0.5)
    assert time.monotonic() - start >= 0.45


def _hold_lock_in_child(tmp_path: Path) -> subprocess.Popen[str]:
    code = textwrap.dedent(
        f"""
        import sys, time
        sys.path.insert(0, {str(Path(__file__).resolve().parent.parent)!r})
        from core.single_instance import SingleInstance
        lock = SingleInstance(__import__("pathlib").Path({str(tmp_path)!r}))
        print("locked" if lock.acquire() else "busy", flush=True)
        time.sleep(30)
        """
    )
    child = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
    assert child.stdout is not None and child.stdout.readline().strip() == "locked"
    return child


def test_lock_held_by_another_process(tmp_path: Path) -> None:
    child = _hold_lock_in_child(tmp_path)
    try:
        lock = SingleInstance(tmp_path)
        assert not lock.acquire()
        assert lock.other_pid() == child.pid
    finally:
        child.kill()
        child.wait()
    # The OS dropped the dead process's lock: the stale file doesn't block us.
    assert SingleInstance(tmp_path).acquire(wait_s=2)
