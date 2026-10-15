"""Tests for the assistant state machine."""

from __future__ import annotations

import pytest

from core.state import ALLOWED, AssistantState, InvalidTransition, StateManager

S = AssistantState


def test_full_conversation_cycle_notifies_subscribers() -> None:
    sm = StateManager(strict=True)
    seen: list[tuple[str, str, str]] = []
    sm.subscribe(lambda old, new, why: seen.append((old.name, new.name, why)))
    for state in (S.WAKE_DETECTED, S.LISTENING, S.THINKING, S.SPEAKING, S.FOLLOW_UP, S.IDLE):
        assert sm.set(state, f"to {state.name}")
    assert [n for _, n, _ in seen] == ["WAKE_DETECTED", "LISTENING", "THINKING", "SPEAKING", "FOLLOW_UP", "IDLE"]
    assert seen[0] == ("IDLE", "WAKE_DETECTED", "to WAKE_DETECTED")


def test_same_state_is_not_a_transition() -> None:
    sm = StateManager(strict=True)
    calls: list[object] = []
    sm.subscribe(lambda *a: calls.append(a))
    assert not sm.set(S.IDLE)
    assert calls == []


def test_invalid_transition_strict_raises_lenient_applies() -> None:
    with pytest.raises(InvalidTransition):
        StateManager(strict=True).set(S.SPEAKING)
    lenient = StateManager()
    assert lenient.set(S.SPEAKING)
    assert lenient.state is S.SPEAKING


def test_barge_in_and_push_to_talk_paths_are_allowed() -> None:
    for busy in (S.THINKING, S.SPEAKING, S.FOLLOW_UP):
        assert S.LISTENING in ALLOWED[busy]
    for state in S:
        assert state is S.IDLE or S.IDLE in ALLOWED[state]  # always recoverable


def test_transition_if_only_from_expected_states() -> None:
    sm = StateManager(strict=True)
    assert not sm.transition_if({S.THINKING}, S.SPEAKING)
    sm.set(S.LISTENING)
    sm.set(S.THINKING)
    assert sm.transition_if({S.THINKING}, S.SPEAKING)
    assert sm.state is S.SPEAKING


def test_unsubscribe_and_failing_listener() -> None:
    sm = StateManager()
    seen: list[str] = []

    def boom(*_a: object) -> None:
        raise RuntimeError("bad listener")

    sm.subscribe(boom)
    unsubscribe = sm.subscribe(lambda o, n, w: seen.append(n.name))
    sm.set(S.LISTENING)  # boom is logged, the next listener still runs
    unsubscribe()
    sm.set(S.THINKING)
    assert seen == ["LISTENING"]
