"""Tests for the confirmation gate and audit log."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.safety import SafetyGate, parse_yes_no


@pytest.mark.parametrize("answer", ["yes", "Yes.", "yeah", "go ahead", "Sure thing", "OK"])
def test_yes_answers(answer: str) -> None:
    assert parse_yes_no(answer)


@pytest.mark.parametrize("answer", ["no", "nope", "", None, "maybe", "yes-ish no", "cancel"])
def test_non_yes_answers_deny(answer: str | None) -> None:
    assert not parse_yes_no(answer)


def test_safe_tools_skip_confirmation(tmp_path: Path) -> None:
    asked: list[str] = []
    gate = SafetyGate(lambda q: asked.append(q) or False, tmp_path / "audit.jsonl")
    assert gate.approve("system_stats", {}, dangerous=False)
    assert asked == []


def test_dangerous_tool_requires_yes(tmp_path: Path) -> None:
    questions: list[str] = []
    answers = iter([True, False])
    gate = SafetyGate(lambda q: questions.append(q) or next(answers), tmp_path / "audit.jsonl")
    assert gate.approve("close_app", {"name": "chrome"}, dangerous=True)
    assert not gate.approve("close_app", {"name": "chrome"}, dangerous=True)
    assert "close app" in questions[0] and "chrome" in questions[0]

    lines = (tmp_path / "audit.jsonl").read_text().splitlines()
    assert len(lines) == 1  # only the denial is logged by approve()
    assert json.loads(lines[0])["approved"] is False


def test_confirm_failure_fails_closed(tmp_path: Path) -> None:
    def broken(_q: str) -> bool:
        raise RuntimeError("mic unplugged")

    gate = SafetyGate(broken, tmp_path / "audit.jsonl")
    assert not gate.approve("shutdown_pc", {}, dangerous=True)


def test_confirmation_can_be_disabled(tmp_path: Path) -> None:
    gate = SafetyGate(lambda q: False, tmp_path / "audit.jsonl", confirm_dangerous=False)
    assert gate.approve("close_app", {"name": "x"}, dangerous=True)


def test_record_writes_jsonl(tmp_path: Path) -> None:
    gate = SafetyGate(lambda q: True, tmp_path / "logs" / "audit.jsonl")
    gate.record("open_app", {"name": "notepad"}, approved=True, result="Opened", is_error=False)
    entry = json.loads((tmp_path / "logs" / "audit.jsonl").read_text())
    assert entry["tool"] == "open_app" and entry["args"] == {"name": "notepad"}
