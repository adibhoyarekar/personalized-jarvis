"""Tool system: the ``@tool`` decorator, auto-discovery, schema building and execution.

Adding a tool takes one function in any module of this package::

    from tools import tool

    @tool
    def get_joke(topic: str = "anything") -> str:
        \"\"\"Tell a short joke.

        Args:
            topic: What the joke should be about.
        \"\"\"
        return f"..."

No other registration is needed: :func:`discover` imports every module in the
package, and the decorator records the function, its JSON schema (built from
the type hints) and its description (built from the docstring).

Mark anything with side effects you would not want by accident with
``@tool(dangerous=True)``; the safety gate then asks for confirmation first.
"""

from __future__ import annotations

import concurrent.futures
import importlib
import inspect
import json
import logging
import pkgutil
import re
import time
import types
import typing
from dataclasses import dataclass, field
from typing import Any, Callable, Literal, Union, get_args, get_origin

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Context shared with tools (config, memory, notifications ...)
# ---------------------------------------------------------------------------


@dataclass
class ToolContext:
    """Runtime services tools may need. Set once at startup via :func:`set_context`.

    Attributes:
        config: The loaded :class:`core.config.Config`.
        notify: Callback to tell the user something asynchronously (timers etc.).
        extras: Additional services (memory store, speech, ...) keyed by name.
    """

    config: Any = None
    notify: Callable[[str], None] = print
    extras: dict[str, Any] = field(default_factory=dict)


_context = ToolContext()


def set_context(context: ToolContext) -> None:
    """Install the runtime context that :func:`get_context` returns."""
    global _context
    _context = context


def get_context() -> ToolContext:
    """Return the runtime context (config, notify callback, extras)."""
    return _context


def setting(key: str, default: Any = None) -> Any:
    """Shortcut for ``get_context().config.get(key, default)``; safe without config."""
    cfg = _context.config
    return default if cfg is None else cfg.get(key, default)


# ---------------------------------------------------------------------------
# Decorator + schema building
# ---------------------------------------------------------------------------


class ToolDefinitionError(TypeError):
    """Raised at import time when a tool's signature cannot become a schema."""


@dataclass(frozen=True)
class ToolSpec:
    """Everything known about one registered tool."""

    name: str
    description: str
    func: Callable[..., Any]
    input_schema: dict[str, Any]
    dangerous: bool
    module: str

    def to_api(self) -> dict[str, Any]:
        """Tool definition in the shape the Messages API expects."""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
            # Stream tool inputs as they are generated; we validate them ourselves.
            "eager_input_streaming": True,
        }


_REGISTRY: dict[str, ToolSpec] = {}

_ARGS_HEADER = re.compile(r"^\s*(Args|Arguments|Parameters)\s*:\s*$", re.IGNORECASE)
_SECTION_HEADER = re.compile(
    r"^\s*(Returns|Return|Raises|Yields|Examples?|Notes?)\s*:\s*$", re.IGNORECASE
)
_ARG_LINE = re.compile(r"^\s*(\*{0,2}\w+)\s*(\([^)]*\))?\s*:\s*(.*)$")


def parse_docstring(doc: str | None) -> tuple[str, dict[str, str]]:
    """Split a Google-style docstring into (description, {param: description}).

    The description is everything except the ``Args:``/``Returns:``-style
    sections, so that guidance like "Use this when ..." reaches the model.
    """
    if not doc:
        return "", {}
    lines = inspect.cleandoc(doc).splitlines()
    desc_lines: list[str] = []
    params: dict[str, str] = {}
    section: str | None = None
    current: str | None = None
    arg_indent: int | None = None

    for line in lines:
        if _ARGS_HEADER.match(line):
            section, current, arg_indent = "args", None, None
            continue
        if _SECTION_HEADER.match(line):
            section, current = "skip", None
            continue
        if section == "args":
            if not line.strip():
                current = None
                continue
            indent = len(line) - len(line.lstrip())
            match = _ARG_LINE.match(line)
            if match and (arg_indent is None or indent <= arg_indent):
                arg_indent = indent
                current = match.group(1).lstrip("*")
                params[current] = match.group(3).strip()
            elif current is not None:
                params[current] = f"{params[current]} {line.strip()}".strip()
            continue
        if section == "skip":
            continue
        desc_lines.append(line)

    description = "\n".join(desc_lines).strip()
    return description, params


_PRIMITIVES: dict[type, str] = {str: "string", int: "integer", float: "number", bool: "boolean"}


def _type_to_schema(annotation: Any, where: str) -> tuple[dict[str, Any], bool]:
    """Map a Python annotation to JSON schema. Returns (schema, is_optional)."""
    origin = get_origin(annotation)

    if origin in (Union, types.UnionType):
        members = [a for a in get_args(annotation) if a is not type(None)]
        optional = len(members) < len(get_args(annotation))
        if len(members) != 1:
            raise ToolDefinitionError(f"{where}: unions other than Optional[X] are not supported")
        schema, _ = _type_to_schema(members[0], where)
        return schema, optional

    if origin is Literal:
        values = list(get_args(annotation))
        kinds = {type(v) for v in values}
        if len(kinds) != 1 or next(iter(kinds)) not in _PRIMITIVES:
            raise ToolDefinitionError(f"{where}: Literal values must share one primitive type")
        return {"type": _PRIMITIVES[next(iter(kinds))], "enum": values}, False

    if origin in (list, typing.List) or annotation is list:
        item_args = get_args(annotation)
        item_schema = _type_to_schema(item_args[0], where)[0] if item_args else {}
        return {"type": "array", "items": item_schema}, False

    if origin in (dict, typing.Dict) or annotation is dict:
        return {"type": "object"}, False

    if annotation in _PRIMITIVES:
        return {"type": _PRIMITIVES[annotation]}, False

    raise ToolDefinitionError(f"{where}: unsupported type annotation {annotation!r}")


def build_schema(func: Callable[..., Any]) -> tuple[str, dict[str, Any]]:
    """Build (description, input_schema) for ``func`` from its hints and docstring.

    Raises:
        ToolDefinitionError: On missing docstrings/type hints or unsupported types.
    """
    name = func.__name__
    description, param_docs = parse_docstring(func.__doc__)
    if not description:
        raise ToolDefinitionError(f"Tool '{name}' needs a docstring describing what it does")

    hints = typing.get_type_hints(func)
    properties: dict[str, Any] = {}
    required: list[str] = []
    for pname, param in inspect.signature(func).parameters.items():
        if param.kind in (param.VAR_POSITIONAL, param.VAR_KEYWORD):
            raise ToolDefinitionError(f"Tool '{name}': *args/**kwargs are not supported")
        if pname not in hints:
            raise ToolDefinitionError(f"Tool '{name}': parameter '{pname}' needs a type hint")
        schema, optional = _type_to_schema(hints[pname], f"Tool '{name}', parameter '{pname}'")
        if pname in param_docs:
            schema["description"] = param_docs[pname]
        if param.default is not inspect.Parameter.empty:
            if param.default is not None:
                schema["default"] = param.default
        elif not optional:
            required.append(pname)
        properties[pname] = schema

    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }
    return description, input_schema


def tool(
    func: Callable[..., Any] | None = None,
    *,
    name: str | None = None,
    dangerous: bool = False,
) -> Any:
    """Register a function as a tool the model can call.

    Usable bare (``@tool``) or with options (``@tool(dangerous=True)``).

    Args:
        func: The function being decorated.
        name: Tool name shown to the model (defaults to the function name).
        dangerous: Require a yes/no confirmation before every call.
    """

    def register(fn: Callable[..., Any]) -> Callable[..., Any]:
        description, schema = build_schema(fn)
        tool_name = name or fn.__name__
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", tool_name):
            raise ToolDefinitionError(f"Invalid tool name '{tool_name}'")
        existing = _REGISTRY.get(tool_name)
        if existing and existing.module != fn.__module__:
            raise ToolDefinitionError(
                f"Duplicate tool name '{tool_name}' in {fn.__module__} and {existing.module}"
            )
        _REGISTRY[tool_name] = ToolSpec(
            name=tool_name,
            description=description,
            func=fn,
            input_schema=schema,
            dangerous=dangerous,
            module=fn.__module__.rsplit(".", 1)[-1],
        )
        fn.__tool_spec__ = _REGISTRY[tool_name]  # type: ignore[attr-defined]
        return fn

    return register(func) if func is not None else register


def discover() -> dict[str, ToolSpec]:
    """Import every module in this package so their ``@tool`` functions register.

    A module that fails to import (e.g. a Windows-only dependency is missing)
    is logged and skipped, so one broken module never takes Jarvis down.
    """
    for module_info in pkgutil.iter_modules(__path__):
        if module_info.name.startswith("_"):
            continue
        full = f"{__name__}.{module_info.name}"
        try:
            importlib.import_module(full)
        except Exception:  # noqa: BLE001 - report and keep going
            log.exception("Could not load tool module %s; its tools are unavailable", full)
    return dict(_REGISTRY)


def all_tools() -> dict[str, ToolSpec]:
    """Every tool registered so far (without filtering by config)."""
    return dict(_REGISTRY)


# ---------------------------------------------------------------------------
# Argument validation + execution
# ---------------------------------------------------------------------------


def validate_args(spec: ToolSpec, args: Any) -> tuple[dict[str, Any] | None, str | None]:
    """Check model-supplied arguments against the tool schema.

    Returns:
        ``(clean_args, None)`` on success or ``(None, error_message)``.
    """
    if not isinstance(args, dict):
        return None, f"Arguments must be a JSON object, got {type(args).__name__}"
    props = spec.input_schema["properties"]
    unknown = sorted(set(args) - set(props))
    if unknown:
        return None, f"Unknown argument(s): {', '.join(unknown)}"
    missing = [p for p in spec.input_schema["required"] if p not in args]
    if missing:
        return None, f"Missing required argument(s): {', '.join(missing)}"

    clean: dict[str, Any] = {}
    for key, value in args.items():
        schema = props[key]
        if value is None:
            clean[key] = None
            continue
        ok, coerced = _check_type(value, schema)
        if not ok:
            return None, f"Argument '{key}' should be {schema.get('type')}, got {value!r}"
        if "enum" in schema and coerced not in schema["enum"]:
            return None, f"Argument '{key}' must be one of {schema['enum']}, got {coerced!r}"
        clean[key] = coerced
    return clean, None


def _check_type(value: Any, schema: dict[str, Any]) -> tuple[bool, Any]:
    kind = schema.get("type")
    if kind == "string":
        return isinstance(value, str), value
    if kind == "boolean":
        return isinstance(value, bool), value
    if kind == "integer":
        if isinstance(value, bool):
            return False, value
        if isinstance(value, int):
            return True, value
        if isinstance(value, float) and value.is_integer():
            return True, int(value)
        return False, value
    if kind == "number":
        return (isinstance(value, (int, float)) and not isinstance(value, bool)), value
    if kind == "array":
        if not isinstance(value, list):
            return False, value
        item_schema = schema.get("items") or {}
        out = []
        for item in value:
            ok, coerced = _check_type(item, item_schema) if item_schema else (True, item)
            if not ok:
                return False, value
            out.append(coerced)
        return True, out
    if kind == "object":
        return isinstance(value, dict), value
    return True, value


@dataclass
class ToolResult:
    """Outcome of one tool call, ready to send back as a ``tool_result`` block."""

    tool_use_id: str
    name: str
    content: str
    is_error: bool = False
    duration_s: float = 0.0

    def to_api(self) -> dict[str, Any]:
        block: dict[str, Any] = {
            "type": "tool_result",
            "tool_use_id": self.tool_use_id,
            "content": self.content,
        }
        if self.is_error:
            block["is_error"] = True
        return block


MAX_RESULT_CHARS = 12_000


def format_result(value: Any) -> str:
    """Turn a tool's return value into text for the model (JSON for structures)."""
    if value is None:
        text = "Done."
    elif isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            text = str(value)
    if len(text) > MAX_RESULT_CHARS:
        text = text[:MAX_RESULT_CHARS] + f"\n...[truncated {len(text) - MAX_RESULT_CHARS} chars]"
    return text


class ToolRegistry:
    """The set of tools enabled by config, plus execution with timeouts."""

    def __init__(
        self,
        specs: dict[str, ToolSpec],
        disabled: list[str] | None = None,
        disabled_modules: list[str] | None = None,
        timeout_s: float = 30.0,
    ) -> None:
        disabled_set = set(disabled or [])
        modules_off = set(disabled_modules or [])
        self.specs = {
            n: s for n, s in specs.items() if n not in disabled_set and s.module not in modules_off
        }
        self.timeout_s = timeout_s
        self._pool = concurrent.futures.ThreadPoolExecutor(max_workers=8, thread_name_prefix="tool")

    @classmethod
    def from_config(cls, config: Any) -> "ToolRegistry":
        """Discover tools and apply the ``tools.*`` toggles from config."""
        return cls(
            discover(),
            disabled=config.get("tools.disabled", []),
            disabled_modules=config.get("tools.disabled_modules", []),
            timeout_s=float(config.get("tools.timeout_s", 30)),
        )

    def api_definitions(self) -> list[dict[str, Any]]:
        """Tool definitions for the API, sorted by name for a stable (cacheable) prefix."""
        defs = [self.specs[name].to_api() for name in sorted(self.specs)]
        if defs:
            # Cache breakpoint on the last tool: the tool list is the stable part
            # of every request, so later requests reuse it instead of re-reading it.
            defs[-1] = {**defs[-1], "cache_control": {"type": "ephemeral"}}
        return defs

    def get(self, name: str) -> ToolSpec | None:
        return self.specs.get(name)

    def submit(self, tool_use_id: str, name: str, args: Any) -> "concurrent.futures.Future[ToolResult]":
        """Validate and start a tool call in the worker pool."""
        spec = self.specs.get(name)
        if spec is None:
            fut: concurrent.futures.Future[ToolResult] = concurrent.futures.Future()
            fut.set_result(ToolResult(tool_use_id, name, f"Unknown tool '{name}'", True))
            return fut
        clean, error = validate_args(spec, args)
        if error:
            fut = concurrent.futures.Future()
            fut.set_result(ToolResult(tool_use_id, name, f"Invalid arguments: {error}", True))
            return fut
        return self._pool.submit(self._run, spec, tool_use_id, clean or {})

    def run(self, tool_use_id: str, name: str, args: Any) -> ToolResult:
        """Run one tool call synchronously, honouring the timeout."""
        future = self.submit(tool_use_id, name, args)
        return self.wait(future, tool_use_id, name)

    def wait(
        self, future: "concurrent.futures.Future[ToolResult]", tool_use_id: str, name: str
    ) -> ToolResult:
        """Wait for a submitted call; a timeout becomes an error result."""
        try:
            return future.result(timeout=self.timeout_s)
        except concurrent.futures.TimeoutError:
            log.warning("Tool %s timed out after %ss", name, self.timeout_s)
            return ToolResult(
                tool_use_id, name, f"Tool timed out after {self.timeout_s:.0f} seconds", True
            )

    @staticmethod
    def _run(spec: ToolSpec, tool_use_id: str, args: dict[str, Any]) -> ToolResult:
        start = time.perf_counter()
        try:
            value = spec.func(**args)
            return ToolResult(
                tool_use_id, spec.name, format_result(value), False, time.perf_counter() - start
            )
        except Exception as exc:  # noqa: BLE001 - tool errors go back to the model
            log.exception("Tool %s failed", spec.name)
            return ToolResult(
                tool_use_id,
                spec.name,
                f"{type(exc).__name__}: {exc}",
                True,
                time.perf_counter() - start,
            )

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)
