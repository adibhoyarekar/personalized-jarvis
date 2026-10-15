"""Shared test fixtures."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from core.config import Config


@pytest.fixture
def config(tmp_path: Path) -> Config:
    data = {
        "assistant": {"name": "Jarvis", "user_name": "Tester"},
        "llm": {"model": "claude-sonnet-5-5", "max_history_turns": 3, "max_tool_rounds": 3,
                "refusal_fallback": True},
        "tools": {"timeout_s": 2, "apps": {"notepad": "notepad.exe"},
                  "folders": {"downloads": str(tmp_path)}},
        "paths": {"logs_dir": str(tmp_path / "logs"), "data_dir": str(tmp_path / "data")},
    }
    return Config(data, tmp_path)


# --- a scripted stand-in for anthropic.Anthropic -----------------------------

class Block(SimpleNamespace):
    def model_dump(self) -> dict[str, Any]:
        return dict(vars(self))


def text_block(text: str) -> Block:
    return Block(type="text", text=text)


def tool_block(id_: str, name: str, input_: Any) -> Block:
    return Block(type="tool_use", id=id_, name=name, input=input_)


def thinking_block() -> Block:
    return Block(type="thinking", thinking="", signature="sig")


class FakeStream:
    def __init__(self, message: SimpleNamespace) -> None:
        self.message = message

    def __enter__(self) -> "FakeStream":
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def __iter__(self):
        for block in self.message.content:
            if block.type == "text":
                yield SimpleNamespace(type="text", text=block.text)

    def get_final_message(self) -> SimpleNamespace:
        return self.message


class FakeClient:
    """Returns scripted responses in order and records every request."""

    def __init__(self, responses: list[Any]) -> None:
        self.responses = list(responses)
        self.requests: list[dict[str, Any]] = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(stream=self._stream))

    def _stream(self, **kwargs: Any) -> FakeStream:
        # Snapshot messages: the agent mutates its turn list after the call.
        self.requests.append({**kwargs, "messages": list(kwargs["messages"])})
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return FakeStream(item)


def message(stop_reason: str, *blocks: Block) -> SimpleNamespace:
    return SimpleNamespace(stop_reason=stop_reason, content=list(blocks))
