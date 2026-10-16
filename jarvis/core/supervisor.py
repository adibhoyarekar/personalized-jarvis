"""Crash recovery: restart the voice loop after an unexpected error, within limits."""

from __future__ import annotations

import logging
import time
from collections import deque
from typing import Callable

log = logging.getLogger(__name__)


class Supervisor:
    """Runs ``target`` and restarts it when it raises.

    A normal return or KeyboardInterrupt/SystemExit ends supervision. After
    ``max_restarts`` crashes within ``window_s`` seconds it gives up, since a
    tight crash loop helps nobody.

    Args:
        target: The session to run (blocks until it ends).
        on_crash: Called with the exception before each restart (e.g. show the ERROR icon).
        on_give_up: Called once when the restart budget is exhausted.
        on_restart: Called just before a restart.
        max_restarts: Restarts allowed per window.
        window_s: Length of the window in seconds.
        delay_s: Pause before restarting.
        clock / sleep: Injectable for tests.
        should_stop: Return True to skip a pending restart (e.g. the user quit meanwhile).
    """

    def __init__(
        self,
        target: Callable[[], None],
        on_crash: Callable[[BaseException], None] | None = None,
        on_give_up: Callable[[BaseException], None] | None = None,
        on_restart: Callable[[int], None] | None = None,
        max_restarts: int = 5,
        window_s: float = 600.0,
        delay_s: float = 3.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        should_stop: Callable[[], bool] = lambda: False,
    ) -> None:
        self.target = target
        self.on_crash = on_crash
        self.on_give_up = on_give_up
        self.on_restart = on_restart
        self.max_restarts = max_restarts
        self.window_s = window_s
        self.delay_s = delay_s
        self.clock = clock
        self.sleep = sleep
        self.should_stop = should_stop
        self.crashes: deque[float] = deque()
        self.restarts = 0

    def run(self) -> bool:
        """Supervise until the target ends normally (True) or we give up (False)."""
        while True:
            try:
                self.target()
                return True
            except (KeyboardInterrupt, SystemExit):
                raise
            except Exception as exc:  # noqa: BLE001 - that's the point of a supervisor
                log.exception("Main loop crashed")
                now = self.clock()
                self.crashes.append(now)
                while self.crashes and now - self.crashes[0] > self.window_s:
                    self.crashes.popleft()
                self._call(self.on_crash, exc)
                if len(self.crashes) > self.max_restarts:
                    log.error("Crashed %d times in %.0f s; giving up", len(self.crashes), self.window_s)
                    self._call(self.on_give_up, exc)
                    return False
                self.sleep(self.delay_s)
                if self.should_stop():
                    return True
                self.restarts += 1
                log.warning("Restarting the main loop (restart %d)", self.restarts)
                self._call(self.on_restart, self.restarts)

    @staticmethod
    def _call(fn: Callable | None, arg: object) -> None:  # type: ignore[type-arg]
        if fn is None:
            return
        try:
            fn(arg)
        except Exception:  # noqa: BLE001
            log.exception("Supervisor callback failed")
