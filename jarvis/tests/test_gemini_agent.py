"""Tests for the Gemini backend using a scripted fake OpenAI-compatible client."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx2
import openai
import pytest
from openai.types.chat import ChatCompletionChunk

import tools
from core.agent import Agent, AgentError, create_agent
from core.config import Config
from core.gemini_agent import GeminiAgent, clean_schema, to_openai_messages, tool_definitions
from core.safety import SafetyGate
from tools import ToolRegistry


@tools.tool(name="zz_gemini_echo")
def _echo(text: str) -> str:
    """Echo text back.

    Args:
        text: Text to echo.
    """
    return f"echo:{text}"


def chunk(content: str | None = None, tool_calls: list[dict[str, Any]] | None = None,
          finish: str | None = None) -> ChatCompletionChunk:
    delta: dict[str, Any] = {}
    if content is not None:
        delta["content"] = content
    if tool_calls is not None:
        delta["tool_calls"] = tool_calls
    return ChatCompletionChunk.construct(
        id="c", object="chat.completion.chunk", created=0, model="gemini",
        choices=[{"index": 0, "delta": delta, "finish_reason": finish}],
    )


def call(name: str, args: dict[str, Any], call_id: str = "", **extra: Any) -> dict[str, Any]:
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}, **extra}


class FakeStream:
    def __init__(self, chunks: list[ChatCompletionChunk]) -> None:
        self.chunks, self.closed = chunks, False

    def __iter__(self):  # type: ignore[no-untyped-def]
        return iter(self.chunks)

    def close(self) -> None:
        self.closed = True


class FakeOpenAI:
    """Each scripted item is a chunk list (one streamed response) or an exception to raise."""

    def __init__(self, script: list[Any]) -> None:
        self.script = list(script)
        self.requests: list[dict[str, Any]] = []
        self.chat = self
        self.completions = self

    def create(self, **kwargs: Any) -> FakeStream:
        self.requests.append(kwargs)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return FakeStream(item)


def gemini_config(tmp_path: Path, **llm: Any) -> Config:
    return Config(
        {
            "assistant": {"name": "Jarvis", "user_name": "Tester"},
            "llm": {"provider": "gemini", "gemini_model": "gemini-test", "max_tool_rounds": 3, **llm},
            "tools": {"timeout_s": 2},
            "paths": {"logs_dir": str(tmp_path / "logs"), "data_dir": str(tmp_path / "data")},
        },
        tmp_path,
    )


def make_agent(tmp_path: Path, client: FakeOpenAI) -> GeminiAgent:
    registry = ToolRegistry(tools.all_tools(), timeout_s=2)
    return GeminiAgent(gemini_config(tmp_path), registry, SafetyGate(lambda q: True, tmp_path / "audit.jsonl"),
                       client=client)


def test_plain_reply_streams_text(tmp_path: Path) -> None:
    client = FakeOpenAI([[chunk("Hello, "), chunk("Tester."), chunk(finish="stop")]])
    agent = make_agent(tmp_path, client)
    pieces: list[str] = []
    reply = agent.ask("hi", on_text=pieces.append)
    assert reply.text == "Hello, Tester." and not reply.error and pieces == ["Hello, ", "Tester."]
    req = client.requests[0]
    assert req["model"] == "gemini-test" and req["stream"] is True and req["reasoning_effort"] == "low"
    assert req["messages"][0]["role"] == "system" and "Tester" in req["messages"][0]["content"]
    assert req["messages"][1] == {"role": "user", "content": "hi"}
    assert any(t["function"]["name"] == "zz_gemini_echo" for t in req["tools"])


def test_tool_round_trip_echoes_ids_and_thought_signatures(tmp_path: Path) -> None:
    signature = {"google": {"thought_signature": "sig"}}
    client = FakeOpenAI([
        # Gemini style: whole calls, no index, finish_reason "stop" even though tools were called.
        [chunk(tool_calls=[call("zz_gemini_echo", {"text": "a"}, "c1", extra_content=signature),
                           call("zz_gemini_echo", {"text": "b"})]),
         chunk(finish="stop")],
        [chunk("Echoed both."), chunk(finish="stop")],
    ])
    agent = make_agent(tmp_path, client)
    reply = agent.ask("echo a and b")
    assert reply.text == "Echoed both." and reply.tool_calls == 2

    messages = client.requests[1]["messages"]
    assistant, first, second = messages[-3:]
    ids = [c["id"] for c in assistant["tool_calls"]]
    assert ids[0] == "c1" and ids[1].startswith("call_")
    assert assistant["tool_calls"][0]["extra_content"] == signature
    assert first == {"role": "tool", "tool_call_id": "c1", "content": "echo:a"}
    assert second == {"role": "tool", "tool_call_id": ids[1], "content": "echo:b"}

    # The next request replays the finished turn as plain OpenAI messages.
    client.script.append([chunk("Sure."), chunk(finish="stop")])
    agent.ask("thanks")
    replay = client.requests[2]["messages"]
    assert [m["role"] for m in replay[1:]] == ["user", "assistant", "tool", "tool", "assistant", "user"]


def test_streamed_argument_fragments_are_joined(tmp_path: Path) -> None:
    client = FakeOpenAI([
        [chunk(tool_calls=[{"index": 0, "id": "c1", "type": "function",
                            "function": {"name": "zz_gemini_echo", "arguments": '{"te'}}]),
         chunk(tool_calls=[{"index": 0, "function": {"arguments": 'xt": "hi"}'}}]),
         chunk(finish="tool_calls")],
        [chunk("Done."), chunk(finish="stop")],
    ])
    agent = make_agent(tmp_path, client)
    assert agent.ask("echo hi").tool_calls == 1
    assert client.requests[1]["messages"][-1]["content"] == "echo:hi"


def test_truncated_tool_call_never_runs(tmp_path: Path) -> None:
    client = FakeOpenAI([[chunk(tool_calls=[call("zz_gemini_echo", {"text": "a"}, "c1")]), chunk(finish="length")]])
    reply = make_agent(tmp_path, client).ask("echo")
    assert reply.error and "smaller steps" in reply.text
    audit = tmp_path / "audit.jsonl"
    assert not audit.exists() or "zz_gemini_echo" not in audit.read_text()


def test_rate_limit_is_spoken_plainly(tmp_path: Path) -> None:
    request = httpx2.Request("POST", "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions")
    error = openai.RateLimitError("quota", response=httpx2.Response(429, request=request), body=None)
    reply = make_agent(tmp_path, FakeOpenAI([error])).ask("hi")
    assert reply.error and "free-tier limit" in reply.text


def test_connection_error_switches_to_offline_mode(tmp_path: Path) -> None:
    request = httpx2.Request("POST", "https://generativelanguage.googleapis.com")
    agent = make_agent(tmp_path, FakeOpenAI([openai.APIConnectionError(request=request)]))
    lost: list[bool] = []
    agent.on_connection_lost = lambda: lost.append(True)
    reply = agent.ask("what's the meaning of life")
    assert reply.error and not agent.online and lost == [True]


def test_schema_cleanup_moves_defaults_into_descriptions() -> None:
    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "default": {"type": "string", "description": "A property literally named default."},
            "limit": {"type": "integer", "description": "How many.", "default": 25},
        },
    }
    cleaned = clean_schema(schema)
    assert "additionalProperties" not in cleaned
    assert cleaned["properties"]["default"]["type"] == "string"
    assert cleaned["properties"]["limit"] == {"type": "integer", "description": "How many. Default: 25."}
    defs = tool_definitions([{"name": "x", "description": "d", "input_schema": schema, "cache_control": {}}])
    assert defs[0]["type"] == "function" and defs[0]["function"]["name"] == "x"


def test_all_real_tools_convert_without_unsupported_keywords() -> None:
    registry = ToolRegistry(tools.all_tools())
    text = json.dumps(tool_definitions(registry.api_definitions()))
    assert "additionalProperties" not in text and "cache_control" not in text


def test_message_conversion_marks_errors() -> None:
    out = to_openai_messages("sys", [
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t", "content": "boom", "is_error": True}]},
    ])
    assert out[1] == {"role": "tool", "tool_call_id": "t", "content": "Error: boom"}


def test_create_agent_picks_provider(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    registry = ToolRegistry(tools.all_tools())
    gate = SafetyGate(lambda q: True, tmp_path / "audit.jsonl")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    with pytest.raises(AgentError, match="GEMINI_API_KEY"):
        create_agent(gemini_config(tmp_path), registry, gate)
    monkeypatch.setenv("GEMINI_API_KEY", "AIza-test")
    agent = create_agent(gemini_config(tmp_path), registry, gate)
    assert isinstance(agent, GeminiAgent) and agent.model == "gemini-test"
    assert str(agent.client.base_url).startswith("https://generativelanguage.googleapis.com")

    anthropic_cfg = Config({"llm": {"provider": "anthropic"}}, tmp_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    assert type(create_agent(anthropic_cfg, registry, gate)) is Agent
    with pytest.raises(AgentError):
        create_agent(Config({"llm": {"provider": "nope"}}, tmp_path), registry, gate)
