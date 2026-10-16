"""Tests for the SQLite memory: facts, recall, forget, prompt injection, history, todos."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.agent import build_system_prompt
from core.memory import HISTORY_LIMIT, Memory
from tools import ToolContext, set_context


@pytest.fixture
def memory() -> Memory:
    m = Memory(":memory:")
    set_context(ToolContext(memory=m))
    return m


def test_remember_and_update(memory: Memory) -> None:
    assert memory.remember("Wifi password", "hunter2") is False
    assert memory.remember("wifi password", "correct horse") is True
    facts = memory.all_facts()
    assert len(facts) == 1 and facts[0].key == "wifi password" and facts[0].value == "correct horse"
    with pytest.raises(ValueError):
        memory.remember("", "x")


def test_recall_keyword_and_fuzzy(memory: Memory) -> None:
    memory.remember("wifi password", "hunter2")
    memory.remember("mom's birthday", "14 March")
    memory.remember("favourite pizza", "margherita")
    assert memory.recall("what's my wifi password?")[0].value == "hunter2"
    assert memory.recall("when is mom's birthday")[0].value == "14 March"
    assert memory.recall("wi fi pasword")[0].key == "wifi password"  # typo -> fuzzy match
    assert memory.recall("pizzas")[0].key == "favourite pizza"  # plural
    assert memory.recall("quantum chromodynamics") == []


def test_forget_exact_and_fuzzy(memory: Memory) -> None:
    memory.remember("wifi password", "hunter2")
    memory.remember("car parking spot", "B2 level 3")
    assert memory.forget("WiFi Password") == "wifi password"
    assert memory.forget("parking spot") == "car parking spot"
    assert memory.forget("something never saved") is None
    assert memory.all_facts() == []


def test_relevant_facts_ranking_top5(memory: Memory) -> None:
    memory.remember("dentist appointment", "Friday 4pm with Dr Rao")
    memory.remember("dentist phone", "98765 43210")
    memory.remember("gym schedule", "Mon Wed Fri mornings")
    memory.remember("favourite colour", "blue")
    for i in range(8):
        memory.remember(f"dentist note {i}", f"misc {i}")
    ranked = memory.relevant_facts("when is my dentist appointment?")
    assert len(ranked) == 5
    assert ranked[0].key == "dentist appointment"  # two keyword hits beat one
    assert all("dentist" in f.key for f in ranked)
    assert memory.relevant_facts("tell me a joke") == []


def test_prompt_context_injects_preferences_and_facts(memory: Memory) -> None:
    memory.set_preference("temperature units", "celsius")
    memory.remember("dentist appointment", "Friday 4pm")
    lines = memory.prompt_context("dentist?")
    assert lines == ["preference - temperature units: celsius", "dentist appointment: Friday 4pm"]
    prompt = build_system_prompt("Jarvis", "Tony", lines)
    assert "dentist appointment: Friday 4pm" in prompt and "celsius" in prompt


def test_memory_tools(memory: Memory) -> None:
    from tools import memory_tools

    assert "Remembered" in memory_tools.remember("locker code", "4512")
    assert memory_tools.recall("locker code")["matches"][0]["value"] == "4512"
    assert memory_tools.list_memories()["facts"] == [{"key": "locker code", "value": "4512"}]
    assert "Forgotten" in memory_tools.forget("locker code")
    assert memory_tools.list_memories() == {"memories": "Nothing saved yet."}


def test_command_history_keeps_last_200(memory: Memory) -> None:
    for i in range(HISTORY_LIMIT + 15):
        memory.add_command(f"command {i}", "ok", [{"tool": "open_app", "args": {"name": "x"}}])
    recent = memory.recent_commands(3)
    assert [r["text"] for r in recent] == [f"command {HISTORY_LIMIT + 14 - i}" for i in range(3)]
    assert recent[0]["tools"][0]["tool"] == "open_app"
    total = memory._query("SELECT COUNT(*) AS n FROM command_history")[0]["n"]
    assert total == HISTORY_LIMIT


def test_todos(memory: Memory) -> None:
    from tools import productivity_tools as p

    p.add_todo("renew passport")
    p.add_todo("buy groceries")
    assert [t["text"] for t in p.read_todos()["todos"]] == ["renew passport", "buy groceries"]
    assert "passport" in p.complete_todo("pasport")  # fuzzy
    assert "buy groceries" in p.complete_todo("1")  # number in the open list
    assert p.read_todos() == {"todos": "The to-do list is empty."}


def test_persists_on_disk(tmp_path: Path) -> None:
    db = tmp_path / "data" / "jarvis.db"
    first = Memory(db)
    first.remember("name", "Aditya")
    first.close()
    assert Memory(db).recall("name")[0].value == "Aditya"
