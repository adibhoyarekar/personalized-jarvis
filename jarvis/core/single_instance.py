"""Only one Jarvis at a time: an OS-level lock on a file in the data folder.

The lock is released by the OS when the process exits, even after a crash,
so a stale lock file never blocks the next start. On Windows a second launch
can also ask the running Jarvis to show its window (:func:`request_show`).
"""

from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path
from typing import IO, Any, Callable

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


SHOW_EVENT = "Local\\JarvisShowWindow"
_EVENT_MODIFY_STATE = 0x0002
_WAIT_OBJECT_0 = 0


def _kernel32() -> Any:
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    k32.CreateEventW.restype = wintypes.HANDLE
    k32.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
    k32.OpenEventW.restype = wintypes.HANDLE
    k32.OpenEventW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    k32.SetEvent.argtypes = [wintypes.HANDLE]
    k32.WaitForSingleObject.restype = wintypes.DWORD
    k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    return k32


def request_show(name: str = SHOW_EVENT) -> bool:
    """Ask the Jarvis that is already running to show its window. False if none is listening."""
    if os.name != "nt":
        return False
    try:
        k32 = _kernel32()
        handle = k32.OpenEventW(_EVENT_MODIFY_STATE, False, name)
        if not handle:
            return False
        try:
            return bool(k32.SetEvent(handle))
        finally:
            k32.CloseHandle(handle)
    except Exception:  # noqa: BLE001
        log.debug("Could not signal the running instance", exc_info=True)
        return False


class ShowRequests:
    """Windows: calls ``on_show`` whenever a second launch calls :func:`request_show`."""

    def __init__(self, on_show: Callable[[], None], name: str = SHOW_EVENT) -> None:
        self.on_show = on_show
        self.name = name
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if os.name != "nt":
            return
        try:
            k32 = _kernel32()
            handle = k32.CreateEventW(None, False, False, self.name)  # auto-reset
        except Exception:  # noqa: BLE001
            log.debug("Show-window event unavailable", exc_info=True)
            return
        if not handle:
            return
        self._thread = threading.Thread(target=self._wait, args=(k32, handle), name="show-requests", daemon=True)
        self._thread.start()

    def _wait(self, k32: Any, handle: Any) -> None:
        try:
            while not self._stop.is_set():
                if k32.WaitForSingleObject(handle, 500) == _WAIT_OBJECT_0:
                    try:
                        self.on_show()
                    except Exception:  # noqa: BLE001
                        log.exception("Show-window request failed")
        finally:
            k32.CloseHandle(handle)

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
