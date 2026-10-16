"""Only one Jarvis at a time: an OS-level lock on a file in the data folder.

The lock is released by the OS when the process exits, even after a crash,
so a stale lock file never blocks the next start.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import IO

log = logging.getLogger(__name__)


class SingleInstance:
    """Exclusive lock on ``<lock_dir>/<name>.lock``.

    Example:
        lock = SingleInstance(data_dir)
        if not lock.acquire():
            sys.exit("Jarvis is already running")
    """

    def __init__(self, lock_dir: Path, name: str = "jarvis") -> None:
        self.path = Path(lock_dir) / f"{name}.lock"
        self._fh: IO[str] | None = None

    @property
    def held(self) -> bool:
        return self._fh is not None

    def acquire(self, wait_s: float = 0.0) -> bool:
        """Try to take the lock, retrying for up to ``wait_s`` seconds (used on restart)."""
        deadline = time.monotonic() + wait_s
        while True:
            if self._try_lock():
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.2)

    def _try_lock(self) -> bool:
        if self._fh is not None:
            return True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+", encoding="utf-8")  # noqa: SIM115 - held for the process lifetime
        try:
            if os.name == "nt":
                import msvcrt

                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            return False
        fh.seek(0)
        fh.truncate()
        fh.write(str(os.getpid()))
        fh.flush()
        self._fh = fh
        log.debug("Acquired instance lock %s", self.path)
        return True

    def other_pid(self) -> int | None:
        """PID written by the instance holding the lock (diagnostics only)."""
        try:
            text = self.path.read_text(encoding="utf-8").strip()
            return int(text) if text.isdigit() else None
        except OSError:
            return None

    def release(self) -> None:
        fh, self._fh = self._fh, None
        if fh is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        fh.close()

    def __enter__(self) -> "SingleInstance":
        if not self.acquire():
            raise RuntimeError("Another instance is already running")
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()
