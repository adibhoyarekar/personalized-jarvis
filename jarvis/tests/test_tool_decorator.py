"""Tests for the @tool decorator, schema building and argument validation."""

from __future__ import annotations

import re
from typing import Literal, Optional

import pytest

import tools
from tools import ToolDefinitionError, ToolRegistry, build_schema, parse_docstring, validate_args


def test_parse_docstring_splits_description_and_args() -> None:
    desc, params = parse_docstring(
        """Do a thing.

        Use this when testing.

        Args:
            name: The name.
                Continues here.
            count (int): How many.

        Returns:
            Something.
        """
    )
    assert desc == "Do a thing.\n\nUse this when testing."
    assert params == {"name": "The name. Continues here.", "count": "How many."}


def test_schema_from_type_hints() -> None:
    def sample(
        name: str,
        level: int = 3,
        ratio: float = 0.5,
        loud: bool = False,
        tags: list[str] | None = None,
        mode: Literal["a", "b"] = "a",
        note: Optional[str] = None,
    ) -> str:
        """Sample tool.

        Args:
            name: Who.
            mode: Which mode.
        """
        return name

    desc, schema = build_schema(sample)
    props = schema["properties"]
    assert desc == "Sample tool."
    assert schema["required"] == ["name"]
    assert schema["additionalProperties"] is False
    assert props["name"] == {"type": "string", "description": "Who."}
    assert props["level"] == {"type": "integer", "default": 3}
    assert props["ratio"]["type"] == "number"
    assert props["loud"]["type"] == "boolean"
    assert props["tags"] == {"type": "array", "items": {"type": "string"}}
    assert props["mode"]["enum"] == ["a", "b"]
    assert props["note"] == {"type": "string"}


def test_missing_docstring_or_hint_is_rejected() -> None:
    def no_doc(x: int) -> int:
        return x

    def no_hint(x) -> int:  # type: ignore[no-untyped-def]
        """Has a doc."""
        return x

    with pytest.raises(ToolDefinitionError):
        build_schema(no_doc)
    with pytest.raises(ToolDefinitionError):
        build_schema(no_hint)


def test_decorator_registers_with_options() -> None:
    @tools.tool(dangerous=True, name="zz_test_delete")
    def delete_everything(target: str) -> str:
        """Delete a target (test only).

        Args:
            target: What to delete.
        """
        return f"deleted {target}"

    spec = tools.all_tools()["zz_test_delete"]
    assert spec.dangerous is True
    assert spec.func is delete_everything
    api = spec.to_api()
    assert api["name"] == "zz_test_delete"
    assert api["input_schema"]["required"] == ["target"]
    assert api["eager_input_streaming"] is True


def test_discover_finds_builtin_tools() -> None:
    found = tools.discover()
    for name in ("open_app", "system_stats", "open_url", "web_search", "list_folder"):
        assert name in found, name


def test_validate_args() -> None:
    @tools.tool(name="zz_test_validate")
    def f(count: int, name: str = "x", mode: Literal["a", "b"] = "a") -> str:
        """Validate me.

        Args:
            count: Number.
        """
        return ""

    spec = tools.all_tools()["zz_test_validate"]
    assert validate_args(spec, {"count": 2})[0] == {"count": 2}
    assert validate_args(spec, {"count": 2.0})[0] == {"count": 2}
    assert validate_args(spec, {})[1].startswith("Missing")
    assert validate_args(spec, {"count": "2"})[1] is not None
    assert validate_args(spec, {"count": True})[1] is not None
    assert validate_args(spec, {"count": 1, "bogus": 1})[1].startswith("Unknown")
    assert validate_args(spec, {"count": 1, "mode": "c"})[1] is not None
    assert validate_args(spec, "not a dict")[1] is not None


def test_registry_filters_runs_and_reports_errors() -> None:
    @tools.tool(name="zz_test_boom")
    def boom() -> str:
        """Always fails."""
        raise ValueError("kaboom")

    @tools.tool(name="zz_test_add")
    def add(a: int, b: int) -> dict:
        """Add numbers.

        Args:
            a: First.
            b: Second.
        """
        return {"sum": a + b}

    registry = ToolRegistry(tools.all_tools(), disabled=["zz_test_validate"], timeout_s=2)
    assert "zz_test_validate" not in registry.specs
    assert registry.run("1", "zz_test_add", {"a": 2, "b": 3}).content == '{"sum": 5}'
    failed = registry.run("2", "zz_test_boom", {})
    assert failed.is_error and "kaboom" in failed.content
    assert registry.run("3", "nope", {}).is_error
    defs = registry.api_definitions()
    assert [d["name"] for d in defs] == sorted(d["name"] for d in defs)
    assert "cache_control" in defs[-1] and "cache_control" not in defs[0]

    no_modules = ToolRegistry(tools.all_tools(), disabled_modules=["system_tools"])
    assert "open_app" not in no_modules.specs
    registry.shutdown()
    no_modules.shutdown()


def test_registry_timeout() -> None:
    import time

    @tools.tool(name="zz_test_slow")
    def slow() -> str:
        """Sleeps."""
        time.sleep(1)
        return "late"

    registry = ToolRegistry(tools.all_tools(), timeout_s=0.1)
    result = registry.run("1", "zz_test_slow", {})
    assert result.is_error and "timed out" in result.content
    registry.shutdown()


def test_all_real_tools_have_valid_schemas() -> None:
    """Every discovered tool has a description and a schema the API will accept."""
    jsonschema = pytest.importorskip("jsonschema")
    found = tools.discover()
    expected = {
        "open_app", "close_app", "set_volume", "mute", "unmute", "set_brightness", "lock_pc", "system_stats",
        "screenshot", "shutdown_pc", "restart_pc", "cancel_shutdown",
        "search_files", "open_file", "read_text_file", "create_note", "list_folder", "delete_file",
        "web_search", "open_url", "get_weather", "get_news",
        "open_project", "run_command_in_project", "git_status", "git_pull", "start_dev_server", "stop_dev_server",
        "set_timer", "set_reminder", "add_todo", "read_todos", "complete_todo", "daily_briefing",
        "play_pause", "next_track", "previous_track", "media_volume", "search_youtube", "open_spotify",
        "read_clipboard", "write_clipboard", "summarize_clipboard",
        "remember", "recall", "forget", "list_memories",
    }
    assert expected <= set(found), expected - set(found)
    for name, spec in found.items():
        if name.startswith("zz_"):
            continue
        api = spec.to_api()
        assert re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", api["name"])
        assert len(api["description"]) >= 15, name
        schema = api["input_schema"]
        jsonschema.Draft202012Validator.check_schema(schema)
        assert schema["type"] == "object" and schema["additionalProperties"] is False
        assert set(schema["required"]) <= set(schema["properties"]), name
        for prop, sub in schema["properties"].items():
            assert "type" in sub, (name, prop)
            if "default" in sub:
                jsonschema.validate(sub["default"], {k: v for k, v in sub.items() if k != "default"})
