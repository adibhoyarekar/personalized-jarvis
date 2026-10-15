"""Assistant state machine: one enum, one manager, subscribers get every transition.

    IDLE -> WAKE_DETECTED -> LISTENING -> THINKING -> SPEAKING -> FOLLOW_UP -> IDLE

Other modules (the voice loop, the tray icon in Phase 5) call
:meth:`StateManager.subscribe` to react to changes.
"""

from __future__ import annotations

import enum
import logging
import threading
from typing import Callable

log = logging.getLogger(__name__)


class AssistantState(enum.Enum):
    IDLE = "idle"                    # waiting for the wake word / hotkey
    WAKE_DETECTED = "wake_detected"  # wake word heard, chime playing
    LISTENING = "listening"          # recording what the user says
    THINKING = "thinking"            # transcribing, waiting for Claude, running tools
    SPEAKING = "speaking"            # reading the reply aloud
    FOLLOW_UP = "follow_up"          # listening briefly for a follow-up, no wake word needed


S = AssistantState

#: Transitions the voice loop is expected to make. Anything can go to IDLE
#: (errors, cancellations); LISTENING is reachable from every busy state
#: because of barge-in and push-to-talk.
ALLOWED: dict[AssistantState, set[AssistantState]] = {
    S.IDLE: {S.WAKE_DETECTED, S.LISTENING, S.THINKING},
    S.WAKE_DETECTED: {S.LISTENING, S.IDLE},
    S.LISTENING: {S.THINKING, S.IDLE, S.WAKE_DETECTED},
    S.THINKING: {S.SPEAKING, S.FOLLOW_UP, S.LISTENING, S.WAKE_DETECTED, S.IDLE},
    S.SPEAKING: {S.THINKING, S.FOLLOW_UP, S.LISTENING, S.WAKE_DETECTED, S.IDLE},
    S.FOLLOW_UP: {S.LISTENING, S.WAKE_DETECTED, S.THINKING, S.IDLE},
}

Listener = Callable[[AssistantState, AssistantState, str], None]


class InvalidTransition(RuntimeError):
    """Raised in strict mode for a transition not listed in :data:`ALLOWED`."""


class StateManager:
    """Holds the current state and notifies subscribers of every change.

    Args:
        strict: Raise on unexpected transitions instead of logging a warning.
            The app runs non-strict so a timing quirk never wedges the loop;
            tests run strict.
    """

    def __init__(self, initial: AssistantState = AssistantState.IDLE, strict: bool = False) -> None:
        self._state = initial
        self._strict = strict
        self._lock = threading.RLock()
        self._listeners: list[Listener] = []

    @property
    def state(self) -> AssistantState:
        return self._state

    def is_(self, *states: AssistantState) -> bool:
        """True if the current state is one of ``states``."""
        return self._state in states

    def subscribe(self, listener: Listener) -> Callable[[], None]:
        """Call ``listener(old, new, reason)`` on every transition. Returns an unsubscribe function."""
        with self._lock:
            self._listeners.append(listener)

        def unsubscribe() -> None:
            with self._lock:
                if listener in self._listeners:
                    self._listeners.remove(listener)

        return unsubscribe

    def set(self, new: AssistantState, reason: str = "") -> bool:
        """Move to ``new``. Returns False if already there (no notification)."""
        with self._lock:
            old = self._state
            if new == old:
                return False
            if new not in ALLOWED[old]:
                message = f"Unexpected transition {old.name} -> {new.name} ({reason})"
                if self._strict:
                    raise InvalidTransition(message)
                log.warning(message)
            self._state = new
            listeners = list(self._listeners)
        log.debug("State %s -> %s%s", old.name, new.name, f" ({reason})" if reason else "")
        for listener in listeners:
            try:
                listener(old, new, reason)
            except Exception:  # noqa: BLE001 - one bad subscriber must not break the loop
                log.exception("State listener failed")
        return True

    def transition_if(self, expected: set[AssistantState], new: AssistantState, reason: str = "") -> bool:
        """Atomically move to ``new`` only if the current state is in ``expected``."""
        with self._lock:
            if self._state not in expected:
                return False
            return self.set(new, reason)
