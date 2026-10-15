"""The Claude tool-use loop: streaming replies, tool execution, conversation history."""

from __future__ import annotations

import datetime as dt
import logging
import os
import platform
import threading
from dataclasses import dataclass
from typing import Any, Callable

import anthropic

from core.safety import SafetyGate
from tools import ToolRegistry, ToolResult, validate_args

log = logging.getLogger(__name__)

TextCallback = Callable[[str], None]
StatusCallback = Callable[[str, str], None]
FactsProvider = Callable[[str], list[str]]

FALLBACK_BETA = "server-side-fallback-2026-07-01"

SYSTEM_TEMPLATE = """\
You are {name}, a personal voice assistant running on {user}'s {os_name} PC.

Personality: calm, competent and quick, with a dry, understated wit, like a \
first-rate butler who happens to be an engineer. Helpful first, funny second. \
Never gushing or sycophantic. Address the user as "{user}" now and then, not \
in every reply.

Everything you write is read aloud by text-to-speech, so:
- Reply in one to three short sentences unless the user asks for more detail.
- Use plain spoken English only: no markdown, lists, bullet points, headings, \
code blocks, emoji or URLs (unless the user asks you to spell one out).
- Write numbers, units and times the way a person would say them.
- When you use a tool, say briefly what you did or what you found, e.g. \
"Chrome is open." or "CPU is at twelve percent; all quiet."
- If a tool fails, say so plainly and, if useful, suggest a fix in a few words.

Using tools:
- Use a tool whenever it helps answer or act; you can call several at once.
- Don't ask for permission first: the system asks the user to confirm risky \
actions itself. If the user declines, accept it gracefully.
- If a request is ambiguous, pick the most sensible reading, or ask one short \
question if guessing would be risky.
- Never invent tool results. If you don't know and no tool can tell you, say so.

Current local date and time: {now}.\
{facts}"""


class AgentError(Exception):
    """An error with a short message suitable for speaking to the user."""

    def __init__(self, spoken: str, detail: str = "") -> None:
        super().__init__(detail or spoken)
        self.spoken = spoken


@dataclass
class Reply:
    """Result of one :meth:`Agent.ask` call."""

    text: str
    error: bool = False
    interrupted: bool = False
    tool_calls: int = 0


def build_system_prompt(
    name: str, user: str, facts: list[str] | None = None, now: dt.datetime | None = None
) -> str:
    """Render the system prompt, optionally including remembered facts."""
    now = now or dt.datetime.now()
    os_name = "Windows" if platform.system() == "Windows" else platform.system() or "desktop"
    facts_block = ""
    if facts:
        lines = "\n".join(f"- {f}" for f in facts)
        facts_block = (
            "\n\nThings the user asked you to remember (use them when relevant, "
            f"don't recite them unprompted):\n{lines}"
        )
    return SYSTEM_TEMPLATE.format(
        name=name,
        user=user,
        os_name=os_name,
        now=now.strftime("%A %d %B %Y, %I:%M %p").replace(" 0", " "),
        facts=facts_block,
    )


def friendly_api_error(exc: Exception) -> AgentError:
    """Translate SDK exceptions into short spoken explanations (most specific first)."""
    if isinstance(exc, anthropic.AuthenticationError):
        return AgentError(
            "My API key was rejected. Please check ANTHROPIC_API_KEY in the .env file.", str(exc)
        )
    if isinstance(exc, anthropic.PermissionDeniedError):
        return AgentError("The API says this key isn't allowed to use that model.", str(exc))
    if isinstance(exc, anthropic.NotFoundError):
        return AgentError("The configured model wasn't found. Check llm.model in config.yaml.", str(exc))
    if isinstance(exc, anthropic.RateLimitError):
        return AgentError("I'm being rate limited. Give me a moment and try again.", str(exc))
    if isinstance(exc, anthropic.BadRequestError):
        return AgentError("Something about that request didn't work. The details are in the log.", str(exc))
    if isinstance(exc, (anthropic.InternalServerError, anthropic.APIStatusError)):
        return AgentError("Anthropic's servers are having a moment. Try again shortly.", str(exc))
    if isinstance(exc, anthropic.APITimeoutError):
        return AgentError("The request timed out. The connection may be slow.", str(exc))
    if isinstance(exc, anthropic.APIConnectionError):
        return AgentError("I can't reach the internet right now. Check your connection.", str(exc))
    return AgentError("Something went wrong on my end. The details are in the log.", repr(exc))


def _sanitize_content(content: Any) -> list[dict[str, Any]]:
    """Keep only text and tool_use blocks, as plain dicts, for long-term history.

    Thinking (and fallback) blocks are bound to the exact conversation that
    produced them. Because older turns get trimmed from the front, a stored
    thinking block would no longer match its original prefix, so finished
    turns are stored without them. Inside a running turn, blocks are passed
    back untouched.
    """
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    blocks: list[dict[str, Any]] = []
    for block in content:
        data = block if isinstance(block, dict) else block.model_dump()
        kind = data.get("type")
        if kind == "text" and data.get("text"):
            blocks.append({"type": "text", "text": data["text"]})
        elif kind == "tool_use":
            blocks.append(
                {"type": "tool_use", "id": data["id"], "name": data["name"], "input": data["input"]}
            )
        elif kind == "tool_result":
            result = {k: data[k] for k in ("type", "tool_use_id", "content", "is_error") if k in data}
            blocks.append(result)
    return blocks


class Agent:
    """Runs conversations with Claude, executing tools and keeping short-term history.

    Args:
        config: Loaded :class:`core.config.Config`.
        registry: Enabled tools.
        gate: Safety gate used for dangerous tools and the audit log.
        client: Optional pre-built Anthropic client (tests inject a fake).
        facts_provider: Returns remembered facts relevant to the user's text.
    """

    def __init__(
        self,
        config: Any,
        registry: ToolRegistry,
        gate: SafetyGate,
        client: Any | None = None,
        facts_provider: FactsProvider | None = None,
    ) -> None:
        self.config = config
        self.registry = registry
        self.gate = gate
        self.facts_provider = facts_provider
        self.model: str = config.get("llm.model", "claude-sonnet-5-5")
        self.max_tokens: int = int(config.get("llm.max_tokens", 16000))
        self.effort: str = str(config.get("llm.effort", "low"))
        self.max_turns: int = int(config.get("llm.max_history_turns", 10))
        self.max_rounds: int = int(config.get("llm.max_tool_rounds", 8))
        self.use_fallback: bool = bool(config.get("llm.refusal_fallback", True))
        self.name: str = config.get("assistant.name", "Jarvis")
        self.user: str = config.get("assistant.user_name", "") or "sir"
        self._history: list[list[dict[str, Any]]] = []
        self._cancel = threading.Event()
        self._lock = threading.Lock()
        self.client = client or self._make_client()

    # -- setup -------------------------------------------------------------

    def _make_client(self) -> anthropic.Anthropic:
        if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
            raise AgentError(
                "No API key found.",
                "ANTHROPIC_API_KEY is not set. Copy .env.example to .env in the jarvis "
                "folder and paste your key from https://platform.claude.com/settings/keys",
            )
        return anthropic.Anthropic(
            timeout=float(self.config.get("llm.request_timeout_s", 60)),
            max_retries=int(self.config.get("llm.max_retries", 2)),
        )

    # -- public API --------------------------------------------------------

    def reset(self) -> None:
        """Forget the conversation so far."""
        self._history.clear()

    def cancel(self) -> None:
        """Abort the reply in progress (used for barge-in)."""
        self._cancel.set()

    @property
    def history_turns(self) -> int:
        return len(self._history)

    def ask(
        self,
        user_text: str,
        on_text: TextCallback | None = None,
        on_status: StatusCallback | None = None,
    ) -> Reply:
        """Send one user utterance and run the tool loop until Claude is done.

        Args:
            user_text: What the user said or typed.
            on_text: Receives reply text as it streams (also error messages).
            on_status: Receives ``(state, detail)`` updates: ``("thinking", "")``,
                ``("tool", name)``, ``("idle", "")``.

        Returns:
            The full reply. Errors are reported in the reply, never raised.
        """
        emit = on_text or (lambda _t: None)
        status = on_status or (lambda _s, _d: None)
        with self._lock:
            self._cancel.clear()
            try:
                return self._ask_locked(user_text.strip(), emit, status)
            finally:
                status("idle", "")

    # -- internals ---------------------------------------------------------

    def _system_prompt(self, user_text: str) -> str:
        facts: list[str] = []
        if self.facts_provider is not None:
            try:
                facts = self.facts_provider(user_text)
            except Exception:  # noqa: BLE001 - memory problems must not block replies
                log.exception("Facts provider failed")
        return build_system_prompt(self.name, self.user, facts)

    def _request_kwargs(self, system: str, messages: list[dict[str, Any]]) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": system,
            "messages": messages,
            "thinking": {"type": "adaptive"},
            "output_config": {"effort": self.effort},
        }
        tools = self.registry.api_definitions()
        if tools:
            kwargs["tools"] = tools
        if self.use_fallback:
            kwargs["betas"] = [FALLBACK_BETA]
            kwargs["fallbacks"] = "default"
        return kwargs

    def _ask_locked(self, user_text: str, emit: TextCallback, status: StatusCallback) -> Reply:
        if not user_text:
            return Reply("")
        system = self._system_prompt(user_text)
        past = [m for turn in self._history[-self.max_turns :] for m in turn]
        turn: list[dict[str, Any]] = [{"role": "user", "content": user_text}]
        spoken: list[str] = []
        tool_calls = 0

        def on_chunk(chunk: str) -> None:
            spoken.append(chunk)
            emit(chunk)

        try:
            for round_no in range(self.max_rounds + 1):
                status("thinking", "")
                message = self._stream_once(system, past + turn, on_chunk)
                if message is None:  # cancelled mid-stream
                    self._commit(turn, "".join(spoken), interrupted=True)
                    return Reply("".join(spoken), interrupted=True, tool_calls=tool_calls)

                stop = message.stop_reason
                tool_uses = [b for b in message.content if b.type == "tool_use"]
                log.info("Round %d: stop_reason=%s tools=%s", round_no, stop, [b.name for b in tool_uses])

                if stop == "refusal":
                    text = "I'm afraid I can't help with that one."
                    on_chunk(("" if not spoken else " ") + text)
                    self._commit(turn, "".join(spoken))
                    return Reply("".join(spoken), tool_calls=tool_calls)

                if stop in ("max_tokens", "model_context_window_exceeded") and tool_uses:
                    # A truncated tool input must never run.
                    raise AgentError("That got a bit long for me. Could you break it into smaller steps?")

                turn.append({"role": "assistant", "content": message.content})

                if stop == "pause_turn":
                    continue
                if stop != "tool_use" or not tool_uses:
                    self._commit(turn, "")
                    return Reply("".join(spoken), tool_calls=tool_calls)

                if round_no == self.max_rounds:
                    raise AgentError("I've gone around in circles on that one. Let's try a simpler request.")

                if spoken and not "".join(spoken).endswith((" ", "\n")):
                    on_chunk(" ")
                results = self._run_tools(tool_uses, status)
                tool_calls += len(results)
                turn.append({"role": "user", "content": [r.to_api() for r in results]})
                if self._cancel.is_set():
                    self._commit(turn, "".join(spoken), interrupted=True)
                    return Reply("".join(spoken), interrupted=True, tool_calls=tool_calls)

            raise AgentError("I've gone around in circles on that one.")  # pragma: no cover

        except AgentError as err:
            log.error("Agent error: %s", err)
            return self._fail(turn, spoken, err, emit, tool_calls)
        except anthropic.AnthropicError as exc:
            log.error("Anthropic API error: %r", exc)
            return self._fail(turn, spoken, friendly_api_error(exc), emit, tool_calls)
        except Exception as exc:  # noqa: BLE001 - never crash the main loop
            log.exception("Unexpected error while answering")
            return self._fail(turn, spoken, friendly_api_error(exc), emit, tool_calls)

    def _fail(
        self,
        turn: list[dict[str, Any]],
        spoken: list[str],
        err: AgentError,
        emit: TextCallback,
        tool_calls: int,
    ) -> Reply:
        prefix = " " if spoken else ""
        emit(prefix + err.spoken)
        # Keep the user's request and what was actually said, so a follow-up
        # like "try again" still has context.
        self._commit(turn, "".join(spoken) + prefix + err.spoken)
        return Reply("".join(spoken) + prefix + err.spoken, error=True, tool_calls=tool_calls)

    def _stream_once(
        self, system: str, messages: list[dict[str, Any]], on_chunk: TextCallback
    ) -> Any | None:
        """Stream one model response. Returns the final message, or None if cancelled."""
        kwargs = self._request_kwargs(system, messages)
        with self.client.beta.messages.stream(**kwargs) as stream:
            for event in stream:
                if self._cancel.is_set():
                    log.info("Reply cancelled mid-stream")
                    return None
                if event.type == "text":
                    on_chunk(event.text)
            return stream.get_final_message()

    def _run_tools(self, tool_uses: list[Any], status: StatusCallback) -> list[ToolResult]:
        """Confirm (if dangerous) and execute tool calls; parallel where possible."""
        # (block, future-or-result, needs_audit) - declined calls are audited by the gate.
        pending: list[tuple[Any, Any, bool]] = []
        for block in tool_uses:
            name, args = block.name, block.input
            spec = self.registry.get(name)
            if spec is None:
                pending.append((block, ToolResult(block.id, name, f"Unknown tool '{name}'", True), True))
                continue
            clean, error = validate_args(spec, args)
            if error:
                pending.append((block, ToolResult(block.id, name, f"Invalid arguments: {error}", True), True))
                continue
            if not self.gate.approve(name, clean or {}, spec.dangerous):
                declined = ToolResult(block.id, name, "The user declined this action.", True)
                pending.append((block, declined, False))
                continue
            status("tool", name)
            pending.append((block, self.registry.submit(block.id, name, clean), True))

        results: list[ToolResult] = []
        for block, item, needs_audit in pending:
            result = item if isinstance(item, ToolResult) else self.registry.wait(item, block.id, block.name)
            if needs_audit:
                args = block.input if isinstance(block.input, dict) else {"raw": block.input}
                self.gate.record(
                    block.name,
                    args,
                    approved=True,
                    result=result.content,
                    is_error=result.is_error,
                    duration_s=result.duration_s,
                )
            log.info("Tool %s -> %s%s", block.name, "ERROR " if result.is_error else "", result.content[:200])
            results.append(result)
        return results

    def _commit(self, turn: list[dict[str, Any]], final_text: str, interrupted: bool = False) -> None:
        """Store a finished turn in history in a valid, append-only-friendly shape."""
        clean: list[dict[str, Any]] = []
        for msg in turn:
            blocks = _sanitize_content(msg["content"])
            if blocks:
                clean.append({"role": msg["role"], "content": blocks})
            elif msg["role"] == "assistant":
                clean.append({"role": "assistant", "content": [{"type": "text", "text": "(no reply)"}]})

        # Drop a trailing assistant tool_use that never got its results.
        if clean and clean[-1]["role"] == "assistant" and any(
            b["type"] == "tool_use" for b in clean[-1]["content"]
        ):
            clean.pop()
        if clean and clean[-1]["role"] == "user":
            text = final_text.strip() or "(no reply)"
            if interrupted:
                text += " [interrupted by the user]"
            clean.append({"role": "assistant", "content": [{"type": "text", "text": text}]})
        if clean and clean[0]["role"] == "user":
            self._history.append(clean)
            del self._history[: -self.max_turns]
