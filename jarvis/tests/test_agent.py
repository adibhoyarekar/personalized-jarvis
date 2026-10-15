"""Tests for the agent loop using a scripted fake Anthropic client."""

from __future__ import annotations

from pathlib import Path

import anthropic
import httpx2 as httpx

import tools
from core.agent import Agent, build_system_prompt
from core.safety import SafetyGate
from tests.conftest import FakeClient, message, text_block, thinking_block, tool_block
from tools import ToolRegistry


@tools.tool(name="zz_agent_echo")
def _echo(text: str) -> str:
    """Echo text back.

    Args:
        text: Text to echo.
    """
    return f"echo:{text}"


@tools.tool(name="zz_agent_danger", dangerous=True)
def _danger() -> str:
    """Do something risky."""
    return "did it"


def make_agent(config, client, tmp_path: Path, confirm=lambda q: True) -> Agent:  # type: ignore[no-untyped-def]
    registry = ToolRegistry(tools.all_tools(), timeout_s=2)
    gate = SafetyGate(confirm, tmp_path / "audit.jsonl")
    return Agent(config, registry, gate, client=client)


def test_plain_reply_streams_text(config, tmp_path: Path) -> None:
    client = FakeClient([message("end_turn", thinking_block(), text_block("Hello, Tester."))])
    agent = make_agent(config, client, tmp_path)
    chunks: list[str] = []
    reply = agent.ask("hi", on_text=chunks.append)
    assert reply.text == "Hello, Tester." and not reply.error
    assert "".join(chunks) == "Hello, Tester."

    req = client.requests[0]
    assert req["model"] == "claude-sonnet-5-5"
    assert req["fallbacks"] == "default" and req["betas"] == ["server-side-fallback-2026-07-01"]
    assert req["thinking"] == {"type": "adaptive"}
    assert "Tester" in req["system"]


def test_tool_round_trip_and_parallel_calls(config, tmp_path: Path) -> None:
    client = FakeClient(
        [
            message(
                "tool_use",
                thinking_block(),
                tool_block("t1", "zz_agent_echo", {"text": "a"}),
                tool_block("t2", "zz_agent_echo", {"text": "b"}),
            ),
            message("end_turn", text_block("Echoed both.")),
        ]
    )
    agent = make_agent(config, client, tmp_path)
    reply = agent.ask("echo a and b")
    assert reply.text == "Echoed both." and reply.tool_calls == 2

    second = client.requests[1]["messages"]
    results = second[-1]["content"]
    assert [r["tool_use_id"] for r in results] == ["t1", "t2"]  # one user message, all results
    assert [r["content"] for r in results] == ["echo:a", "echo:b"]
    # Within a turn the assistant content (incl. thinking) is passed back untouched.
    assert second[-2]["content"][0].type == "thinking"
    assert (tmp_path / "audit.jsonl").read_text().count("zz_agent_echo") == 2


def test_history_is_trimmed_and_thinking_stripped(config, tmp_path: Path) -> None:
    replies = [message("end_turn", thinking_block(), text_block(f"r{i}")) for i in range(5)]
    client = FakeClient(replies)
    agent = make_agent(config, client, tmp_path)
    for i in range(5):
        agent.ask(f"q{i}")
    assert agent.history_turns == 3  # max_history_turns in the fixture
    last_request = client.requests[-1]["messages"]
    assert last_request[0] == {"role": "user", "content": [{"type": "text", "text": "q1"}]}
    for msg in last_request[:-1]:
        for block in msg["content"]:
            assert block["type"] != "thinking"


def test_dangerous_tool_declined(config, tmp_path: Path) -> None:
    client = FakeClient(
        [
            message("tool_use", tool_block("t1", "zz_agent_danger", {})),
            message("end_turn", text_block("Understood, I left it alone.")),
        ]
    )
    asked: list[str] = []
    agent = make_agent(config, client, tmp_path, confirm=lambda q: asked.append(q) or False)
    reply = agent.ask("do the risky thing")
    assert asked and reply.text.startswith("Understood")
    result = client.requests[1]["messages"][-1]["content"][0]
    assert result["is_error"] is True and "declined" in result["content"]


def test_invalid_tool_args_return_error_to_model(config, tmp_path: Path) -> None:
    client = FakeClient(
        [
            message("tool_use", tool_block("t1", "zz_agent_echo", {"txt": "oops"})),
            message("end_turn", text_block("Sorry about that.")),
        ]
    )
    agent = make_agent(config, client, tmp_path)
    agent.ask("echo")
    result = client.requests[1]["messages"][-1]["content"][0]
    assert result["is_error"] and "Invalid arguments" in result["content"]


def test_truncated_tool_input_is_not_run(config, tmp_path: Path) -> None:
    client = FakeClient([message("max_tokens", tool_block("t1", "zz_agent_danger", {}))])
    asked: list[str] = []
    agent = make_agent(config, client, tmp_path, confirm=lambda q: asked.append(q) or True)
    reply = agent.ask("go")
    assert reply.error and not asked


def test_api_errors_become_spoken_messages(config, tmp_path: Path) -> None:
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    client = FakeClient(
        [
            anthropic.APIConnectionError(request=request),
            message("end_turn", text_block("Back online.")),
        ]
    )
    agent = make_agent(config, client, tmp_path)
    spoken: list[str] = []
    reply = agent.ask("hello?", on_text=spoken.append)
    assert reply.error and "internet" in reply.text
    assert spoken == [reply.text]
    # History stays valid: the next request starts with a user message and alternates.
    agent.ask("again")
    roles = [m["role"] for m in client.requests[-1]["messages"]]
    assert roles == ["user", "assistant", "user"]


def test_refusal_is_handled(config, tmp_path: Path) -> None:
    client = FakeClient([message("refusal")])
    agent = make_agent(config, client, tmp_path)
    reply = agent.ask("something it won't do")
    assert "can't help" in reply.text


def test_tool_round_limit(config, tmp_path: Path) -> None:
    loop = [message("tool_use", tool_block(f"t{i}", "zz_agent_echo", {"text": "x"})) for i in range(4)]
    client = FakeClient(loop)
    agent = make_agent(config, client, tmp_path)
    reply = agent.ask("loop forever")
    assert reply.error and len(client.requests) == 4  # max_tool_rounds=3 plus the first call
    assert agent.history_turns == 1


def test_system_prompt_includes_facts() -> None:
    prompt = build_system_prompt("Jarvis", "Tony", ["Favourite colour is red"])
    assert "Favourite colour is red" in prompt and "Tony" in prompt
    assert "markdown" in prompt
