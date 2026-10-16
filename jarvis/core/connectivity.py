"""Watches whether the Claude API is reachable and retries in the background when it isn't."""

from __future__ import annotations

import logging
import threading
from typing import Callable

log = logging.getLogger(__name__)

PingFn = Callable[[], str]  # returns "ok", "offline", "auth" or "error"


class ConnectivityMonitor:
    """Tracks online/offline; while offline, pings every ``interval`` seconds.

    Args:
        ping: Checks the API; "offline" means unreachable, anything else counts as reachable
            (a bad key is a config problem, not a connection problem).
        on_offline: Called once when the connection is lost.
        on_online: Called once when it comes back.
        interval: Seconds between retries while offline.
    """

    def __init__(
        self,
        ping: PingFn,
        on_offline: Callable[[], None] | None = None,
        on_online: Callable[[], None] | None = None,
        interval: float = 30.0,
    ) -> None:
        self.ping = ping
        self.on_offline = on_offline
        self.on_online = on_online
        self.interval = interval
        self.online = True
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def check_now(self) -> bool:
        """Ping synchronously and update the state. Returns True if reachable."""
        try:
            status = self.ping()
        except Exception:  # noqa: BLE001
            log.exception("Connectivity ping failed")
            status = "offline"
        if status == "offline":
            self.mark_offline()
        else:
            self.mark_online()
        return self.online

    def mark_offline(self) -> None:
        with self._lock:
            if not self.online:
                return
            self.online = False
        log.warning("Claude API unreachable; running in degraded mode")
        self._fire(self.on_offline)
        self._wake.set()

    def mark_online(self) -> None:
        with self._lock:
            if self.online:
                return
            self.online = True
        log.info("Claude API reachable again")
        self._fire(self.on_online)

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="connectivity", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=2)

    def _run(self) -> None:
        while not self._stop.is_set():
            if self.online:
                self._wake.wait()  # sleep until something reports a lost connection
                self._wake.clear()
                continue
            if self._stop.wait(self.interval):
                return
            self.check_now()

    @staticmethod
    def _fire(callback: Callable[[], None] | None) -> None:
        if callback is None:
            return
        try:
            callback()
        except Exception:  # noqa: BLE001
            log.exception("Connectivity callback failed")
