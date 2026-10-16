"""Google Gemini backend, via Gemini's OpenAI-compatible endpoint (free tier available).

The shared tool loop in :class:`core.agent.Agent` works on Anthropic-shaped
messages. This subclass converts them to OpenAI chat format on the way out and
turns each streamed response back into Anthropic-like blocks on the way in, so
tools, the safety gate, history and offline mode behave the same for both providers.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

import openai

from core.agent import (
    DEFAULT_GEMINI_MODEL,
    GEMINI_BASE_URL,
    Agent,
    AgentError,
    TextCallback,
    gemini_api_key,
)

log = logging.getLogger(__name__)

# OpenAI finish_reason -> the Anthropic stop_reason the shared loop understands.
_STOP_REASONS = {
    "stop": "end_turn",
    "length": "max_tokens",
    "content_filter": "refusal",
    "tool_calls": "tool_use",
}
_EFFORT = {"low": "low", "medium": "medium", "high": "high", "xhigh": "high", "max": "high"}
# Keywords Gemini's function-declaration schema rejects or ignores.
_UNSUPPORTED_SCHEMA_KEYS = {"additionalProperties", "default", "cache_control", "eager_input_streaming"}


@dataclass
class Block:
    """A content block shaped like the Anthropic SDK's (attribute access + model_dump)."""

    type: str
    text: str = ""
    id: str = ""
    name: str = ""
    input: Any = field(default_factory=dict)
    #: Provider data that must be echoed back with a tool call (Gemini thought signatures).
    extra: dict[str, Any] | None = None

    def model_dump(self) -> dict[str, Any]:
        if self.type == "text":
            return {"type": "text", "text": self.text}
        return {"type": "tool_use", "id": self.id, "name": self.name, "input": self.input}


@dataclass
class Message:
    content: list[Block]
    stop_reason: str


def clean_schema(schema: Any) -> Any:
    """Drop JSON-schema keywords Gemini rejects; a dropped default moves into the description."""
    if isinstance(schema, list):
        return [clean_schema(item) for item in schema]
    if not isinstance(schema, dict):
        return schema
    out: dict[str, Any] = {}
    for key, value in schema.items():
        if key in _UNSUPPORTED_SCHEMA_KEYS:
            continue
        if key == "properties" and isinstance(value, dict):
            out[key] = {name: clean_schema(sub) for name, sub in value.items()}
        else:
            out[key] = clean_schema(value)
    if "default" in schema and isinstance(out.get("description"), str):
        out["description"] = f"{out['description'].rstrip()} Default: {json.dumps(schema['default'])}."
    return out


def tool_definitions(api_defs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert Messages-API tool definitions to OpenAI function tools."""
    return [
        {
            "type": "function",
            "function": {
                "name": d["name"],
                "description": d.get("description", ""),
                "parameters": clean_schema(d["input_schema"]),
            },
        }
        for d in api_defs
    ]


def _get(block: Any, key: str, default: Any = None) -> Any:
    return block.get(key, default) if isinstance(block, dict) else getattr(block, key, default)


def to_openai_messages(system: str, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert Anthropic-shaped history (text/tool_use/tool_result blocks) to OpenAI chat messages."""
    out: list[dict[str, Any]] = [{"role": "system", "content": system}]
    for msg in messages:
        role, content = msg["role"], msg["content"]
        if isinstance(content, str):
            out.append({"role": role, "content": content})
            continue
        texts: list[str] = []
        calls: list[dict[str, Any]] = []
        for block in content:
            kind = _get(block, "type")
            if kind == "text" and _get(block, "text"):
                texts.append(_get(block, "text"))
            elif kind == "tool_use":
                call: dict[str, Any] = {
                    "id": _get(block, "id"),
                    "type": "function",
                    "function": {"name": _get(block, "name"), "arguments": json.dumps(_get(block, "input", {}))},
                }
                if _get(block, "extra"):
                    call["extra_content"] = _get(block, "extra")
                calls.append(call)
            elif kind == "tool_result":
                body = _get(block, "content", "")
                if not isinstance(body, str):
                    body = json.dumps(body)
                if _get(block, "is_error") and not body.startswith("Error"):
                    body = f"Error: {body}"
                out.append({"role": "tool", "tool_call_id": _get(block, "tool_use_id"), "content": body})
        if role == "assistant":
            reply: dict[str, Any] = {"role": "assistant", "content": "".join(texts) or None}
            if calls:
                reply["tool_calls"] = calls
            out.append(reply)
        elif texts:
            out.append({"role": "user", "content": "\n".join(texts)})
    return out


def _is_key_error(exc: Exception) -> bool:
    text = str(exc)
    return "API key" in text or "API_KEY" in text


def friendly_gemini_error(exc: Exception) -> AgentError:
    """Translate OpenAI-SDK exceptions from the Gemini endpoint into short spoken explanations."""
    if isinstance(exc, AgentError):
        return exc
    if isinstance(exc, openai.AuthenticationError) or (
        isinstance(exc, (openai.BadRequestError, openai.PermissionDeniedError)) and _is_key_error(exc)
    ):
        return AgentError("My Gemini API key was rejected. Please check GEMINI_API_KEY in the .env file.", str(exc))
    if isinstance(exc, openai.PermissionDeniedError):
        return AgentError("Google says this key isn't allowed to use that model.", str(exc))
    if isinstance(exc, openai.NotFoundError):
        return AgentError("The Gemini model wasn't found. Check llm.gemini_model in config.yaml.", str(exc))
    if isinstance(exc, openai.RateLimitError):
        return AgentError("I've hit Gemini's free-tier limit. Give me a minute and try again.", str(exc))
    if isinstance(exc, openai.BadRequestError):
        return AgentError("Something about that request didn't work. The details are in the log.", str(exc))
    if isinstance(exc, openai.APIStatusError):
        return AgentError("Google's servers are having a moment. Try again shortly.", str(exc))
    if isinstance(exc, openai.APITimeoutError):
        return AgentError("The request timed out. The connection may be slow.", str(exc))
    if isinstance(exc, openai.APIConnectionError):
        return AgentError("I can't reach the internet right now. Check your connection.", str(exc))
    return AgentError("Something went wrong on my end. The details are in the log.", repr(exc))


class GeminiAgent(Agent):
    """:class:`core.agent.Agent` backed by Google Gemini (``llm.provider: gemini``)."""

    CONNECTION_ERRORS = (openai.APIConnectionError,)  # includes APITimeoutError
    API_ERRORS = (openai.OpenAIError,)

    def __init__(self, config: Any, *args: Any, **kwargs: Any) -> None:
        super().__init__(config, *args, **kwargs)
        self.model = str(config.get("llm.gemini_model", DEFAULT_GEMINI_MODEL) or DEFAULT_GEMINI_MODEL)
        self._tools = tool_definitions(self.registry.api_definitions())

    def _make_client(self) -> openai.OpenAI:
        key = gemini_api_key()
        if not key:
            raise AgentError(
                "No Gemini API key found.",
                "GEMINI_API_KEY is not set. Add GEMINI_API_KEY=your-key to the .env file in the "
                "jarvis folder (free key: https://aistudio.google.com/apikey)",
            )
        return openai.OpenAI(
            api_key=key,
            base_url=GEMINI_BASE_URL,
            timeout=float(self.config.get("llm.request_timeout_s", 60)),
            max_retries=int(self.config.get("llm.max_retries", 2)),
        )

    def _friendly_error(self, exc: Exception) -> AgentError:
        return friendly_gemini_error(exc)

    def ping(self, timeout: float = 6.0) -> str:
        try:
            self.client.with_options(timeout=timeout, max_retries=0).models.retrieve(self.model)
            return "ok"
        except openai.APIConnectionError:
            return "offline"
        except (openai.AuthenticationError, openai.PermissionDeniedError):
            return "auth"
        except openai.NotFoundError:
            return "ok"  # reachable; the model name problem surfaces on the first request
        except openai.BadRequestError as exc:
            return "auth" if _is_key_error(exc) else "error"
        except Exception:  # noqa: BLE001
            log.exception("Gemini ping failed")
            return "error"

    def complete(self, prompt: str, max_tokens: int = 2000) -> str:
        try:
            response = self.client.chat.completions.create(
                model=self.model,
                max_tokens=max(max_tokens, 4000),  # thinking tokens count against this on Gemini
                messages=[{"role": "user", "content": prompt}],
                reasoning_effort="low",
            )
        except openai.OpenAIError as exc:
            raise friendly_gemini_error(exc) from exc
        choice = response.choices[0]
        if choice.finish_reason == "content_filter":
            raise AgentError("I can't summarise that one.")
        return (choice.message.content or "").strip()

    def _stream_once(self, system: str, messages: list[dict[str, Any]], on_chunk: TextCallback) -> Message | None:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "messages": to_openai_messages(system, messages),
            "stream": True,
            "reasoning_effort": _EFFORT.get(self.effort, "low"),
        }
        if self._tools:
            kwargs["tools"] = self._tools
        stream = self.client.chat.completions.create(**kwargs)
        text: list[str] = []
        calls: dict[int, dict[str, Any]] = {}
        finish: str | None = None
        try:
            for chunk in stream:
                if self._cancel.is_set():
                    log.info("Reply cancelled mid-stream")
                    return None
                if not chunk.choices:
                    continue
                choice = chunk.choices[0]
                delta = choice.delta
                if delta is not None and delta.content:
                    text.append(delta.content)
                    on_chunk(delta.content)
                for tc in (delta.tool_calls if delta is not None else None) or []:
                    # Gemini often sends each call whole without an index; treat that as a new call.
                    index = getattr(tc, "index", None)
                    slot = calls.setdefault(
                        index if index is not None else len(calls), {"id": "", "name": "", "args": "", "extra": None}
                    )
                    if tc.id:
                        slot["id"] = tc.id
                    if tc.function is not None:
                        if tc.function.name:
                            slot["name"] = tc.function.name
                        if tc.function.arguments:
                            slot["args"] += tc.function.arguments
                    extra = (tc.model_extra or {}).get("extra_content")
                    if extra:
                        slot["extra"] = extra
                if choice.finish_reason:
                    finish = choice.finish_reason
        finally:
            stream.close()

        blocks: list[Block] = []
        if text:
            blocks.append(Block("text", text="".join(text)))
        for key in sorted(calls):
            slot = calls[key]
            raw = slot["args"].strip()
            try:
                args: Any = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                args = raw  # the shared loop reports this back to the model as invalid arguments
            blocks.append(
                Block(
                    "tool_use",
                    id=slot["id"] or f"call_{uuid.uuid4().hex[:12]}",
                    name=slot["name"],
                    input=args,
                    extra=slot["extra"],
                )
            )
        if finish == "length":
            stop = "max_tokens"  # never run a possibly truncated tool call
        elif any(b.type == "tool_use" for b in blocks):
            stop = "tool_use"  # Gemini sometimes reports "stop" alongside tool calls
        else:
            stop = _STOP_REASONS.get(finish or "stop", "end_turn")
        return Message(blocks, stop)
