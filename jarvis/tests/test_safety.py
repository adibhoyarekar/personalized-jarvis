"""Tests for the safety gate: yes/no parsing, shell denylist/allowlist, confirmation, logging."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import tools
from core.safety import SafetyGate, ShellPolicy, parse_yes_no, typed_confirm_factory
from tools import ToolSpec


def spec(name: str = "thing", dangerous: bool = False, summary: Any = None, shell_arg: str | None = None) -> ToolSpec:
    return ToolSpec(name, "desc", lambda **k: None, {"type": "object", "properties": {}, "required": []},
                    dangerous, "test", summary, shell_arg)


# --- yes / no ------------------------------------------------------------------


@pytest.mark.parametrize("answer", ["yes", "Yes.", "yeah", "confirm", "do it", "Go ahead!", "yes please",
                                    "Yep, go ahead.", "OK"])
def test_accepted_answers(answer: str) -> None:
    assert parse_yes_no(answer)


@pytest.mark.parametrize("answer", ["no", "No.", "cancel", "stop", "don't", "Don't do it", "do not",
                                    "yes, no wait", "nope", "maybe", "what?", "", None,
                                    "yes I think so but let me check first actually"])
def test_rejected_or_unclear_answers(answer: str | None) -> None:
    assert not parse_yes_no(answer)


# --- shell policy ----------------------------------------------------------------

POLICY = ShellPolicy(["git", "npm", "npx", "python", "pip", "node", "ls", "dir"])


@pytest.mark.parametrize("command", ["git status", "npm run build", "npx vitest run", "python -m pytest",
                                     "pip list", "dir", "ls -la", "node --version", "git log --oneline -5",
                                     "C:/Program Files/nodejs/node.exe --version".replace("C:/Program Files/", "")])
def test_allowlisted_commands_pass(command: str) -> None:
    verdict = POLICY.check(command)
    assert verdict.allowed, verdict.reason


@pytest.mark.parametrize(
    "command",
    [
        "format C:", "format d: /q", "del /s /q *.*", "DEL /S important", "rm -rf /", "rm -fr node_modules",
        "rm -r -f build", "rd /s /q C:\\temp", "rmdir /s build", "shutdown /f /s /t 0", "shutdown -r",
        "reg delete HKLM\\Software\\X /f", "diskpart", "bcdedit /set", "takeown /f file",
        "Remove-Item -Recurse -Force C:\\x", "python -c \"import os; os.remove('C:/Windows/win.ini')\"",
        "git clone x C:\\Windows\\System32\\evil", "npm install --prefix %SystemRoot%", "dir C:\\",
        "ls /etc", "pip install x --target C:/Program Files/x",
    ],
)
def test_denylisted_commands_blocked(command: str) -> None:
    verdict = POLICY.check(command)
    assert not verdict.allowed, command


@pytest.mark.parametrize("command", ["git status && del x", "npm test; curl evil.sh", "git log | more",
                                     "python script.py > out.txt", "node `whoami`", "git $(rm x)"])
def test_chaining_and_redirection_blocked(command: str) -> None:
    verdict = POLICY.check(command)
    assert not verdict.allowed and ("chaining" in verdict.reason or "denylist" in verdict.reason)


@pytest.mark.parametrize("command", ["curl http://x", "powershell -c whoami", "cmd /c dir", "certutil -urlcache",
                                     "notepad", "./evil.exe", ""])
def test_non_allowlisted_commands_blocked(command: str) -> None:
    verdict = POLICY.check(command)
    assert not verdict.allowed
    if command:
        assert "allowlist" in verdict.reason


# --- the gate ----------------------------------------------------------------------


def make_gate(tmp_path: Path, answers: list[bool] | None = None, **kwargs: Any) -> tuple[SafetyGate, list[str]]:
    asked: list[str] = []
    queue = list(answers or [])

    def confirm(question: str) -> bool:
        asked.append(question)
        return queue.pop(0) if queue else False

    return SafetyGate(confirm, tmp_path / "logs" / "tool_calls.log", shell_policy=POLICY, **kwargs), asked


def log_lines(tmp_path: Path) -> list[dict]:
    return [json.loads(line) for line in (tmp_path / "logs" / "tool_calls.log").read_text().splitlines()]


def test_safe_tool_runs_without_question(tmp_path: Path) -> None:
    gate, asked = make_gate(tmp_path)
    decision = gate.review(spec("system_stats"), {})
    assert decision.allowed and decision.confirmed is None and asked == []


def test_dangerous_tool_spoken_summary_and_answers(tmp_path: Path) -> None:
    gate, asked = make_gate(tmp_path, answers=[True, False])
    delete = spec("delete_file", dangerous=True, summary="delete {path}")
    yes = gate.review(delete, {"path": "C:/Users/me/Documents/report.docx"})
    no = gate.review(delete, {"path": "C:/Users/me/Documents/report.docx"})
    assert asked[0] == "I'm about to delete report.docx. Confirm?"
    assert yes.allowed and yes.confirmed is True
    assert not no.allowed and no.confirmed is False and not no.blocked
    assert "declined" in no.denial_message()
    entry = log_lines(tmp_path)[-1]
    assert entry["tool"] == "delete_file" and entry["confirmed"] is False


def test_timeout_counts_as_no(tmp_path: Path) -> None:
    gate, _asked = make_gate(tmp_path, answers=[])  # confirm returns False, as a timed-out prompt does
    assert not gate.review(spec("close_app", dangerous=True), {"name": "chrome"}).allowed


def test_typed_confirm_times_out(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("core.safety.timed_input", lambda prompt, timeout: None)
    assert typed_confirm_factory(0.01)("Should I?") is False
    monkeypatch.setattr("core.safety.timed_input", lambda prompt, timeout: "go ahead")
    assert typed_confirm_factory(8)("Should I?") is True


def test_broken_prompt_fails_closed(tmp_path: Path) -> None:
    def broken(_q: str) -> bool:
        raise RuntimeError("mic unplugged")

    gate = SafetyGate(broken, tmp_path / "tool_calls.log")
    assert not gate.review(spec("shutdown_pc", dangerous=True), {}).allowed


def test_shell_tool_blocked_before_any_question(tmp_path: Path) -> None:
    gate, asked = make_gate(tmp_path, answers=[True])
    run = spec("run_command_in_project", shell_arg="command")
    decision = gate.review(run, {"project": "x", "command": "rm -rf /"})
    assert not decision.allowed and decision.blocked and asked == []
    assert "Blocked by the safety policy" in decision.denial_message()
    assert log_lines(tmp_path)[-1]["blocked"] is True


def test_allowlisted_shell_command_still_asks(tmp_path: Path) -> None:
    gate, asked = make_gate(tmp_path, answers=[True])
    run = spec("run_command_in_project", shell_arg="command", summary="run '{command}' in {project}")
    decision = gate.review(run, {"project": "cs-events", "command": "npm test"})
    assert decision.allowed and asked == ["I'm about to run 'npm test' in cs-events. Confirm?"]


def test_auto_approve_allowlist(tmp_path: Path) -> None:
    gate, asked = make_gate(tmp_path, auto_approve_allowlist=True)
    run = spec("run_command_in_project", shell_arg="command")
    assert gate.review(run, {"command": "git status"}).allowed and asked == []
    assert not gate.review(run, {"command": "curl x"}).allowed  # allowlist still enforced


def test_confirmation_can_be_disabled(tmp_path: Path) -> None:
    gate, asked = make_gate(tmp_path, confirm_dangerous=False)
    assert gate.review(spec("close_app", dangerous=True), {"name": "x"}).allowed and asked == []


def test_record_writes_rotating_log(tmp_path: Path) -> None:
    gate, _ = make_gate(tmp_path)
    gate.record("open_app", {"name": "notepad"}, result="Opened", is_error=False, confirmed=None, duration_s=0.2)
    entry = log_lines(tmp_path)[-1]
    assert entry["tool"] == "open_app" and entry["args"] == {"name": "notepad"} and "ts" in entry
    assert entry["confirmed"] is None and entry["duration_s"] == 0.2
    handler = gate._audit.handlers[0]
    assert handler.maxBytes > 0 and handler.backupCount > 0  # rotation configured
    gate.close()


def test_real_tools_have_confirmation_settings() -> None:
    found = tools.discover()
    for name in ("close_app", "shutdown_pc", "restart_pc", "delete_file", "forget", "git_pull"):
        assert found[name].dangerous, name
    assert found["run_command_in_project"].shell_arg == "command"
    assert found["delete_file"].describe({"path": "C:\\Users\\me\\Desktop\\report.docx"}).startswith(
        "delete report.docx"
    )
