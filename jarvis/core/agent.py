"""The Claude tool-use loop: streaming replies, tool execution, conversation history."""

from __future__ import annotations

import datetime as dt
import logging
import os
import platform
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

import anthropic

from core.offline import OFFLINE_HELP, OfflineRouter
from core.safety import SafetyGate
from tools import ToolRegistry, ToolResult, validate_args

log = logging.getLogger(__name__)

TextCallback = Callable[[str], None]
StatusCallback = Callable[[str, str], None]
FactsProvider = Callable[[str], list[str]]
TurnCallback = Callable[[str, str, list[dict[str, Any]]], None]

FALLBACK_BETA = "server-side-fallback-2026-07-01"

GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"
DEFAULT_GEMINI_MODEL = "gemini-flash-latest"


def llm_provider(config: Any) -> str:
    """The configured model provider: "anthropic" (default) or "gemini"."""
    return str(config.get("llm.provider", "anthropic") or "anthropic").strip().lower()


def gemini_api_key() -> str | None:
    return os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")

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

Your tools let you control this PC: open and close apps, volume, brightness, \
media keys, screenshots, locking, shutdown; search, open, read and create \
files in the user's allowed folders; web search, weather and news; the user's \
coding projects (open in VS Code, git, run allowlisted commands, dev servers); \
timers, reminders, to-dos and a daily briefing; the clipboard; and long-term \
memory (remember, recall, forget).

Using tools:
- Use a tool whenever it helps answer or act; you can call several at once, \
up to {max_calls} per request.
- Act on sensible defaults. Ask for a missing detail only when you truly can't \
proceed without it (e.g. which file to delete), in one short question.
- Don't ask for permission first: the system itself asks the user to confirm \
risky actions. If the user declines or a policy blocks a call, say so briefly \
and don't retry it.
- Only say you did something if the tool result shows it succeeded. If a tool \
returns an error, explain the problem in plain words instead.
- Never invent tool results. If you don't know and no tool can tell you, say so.
- When the user says "remember that...", save it with remember. Things you \
remember are listed below when relevant; use them naturally.
- For reminders, convert the user's time into an exact local "YYYY-MM-DD HH:MM" \
using the current date and time below.
- For "do that again", check recent_commands if the conversation doesn't show it.

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
    name: str,
    user: str,
    facts: list[str] | None = None,
    now: dt.datetime | None = None,
    max_calls: int = 6,
) -> str:
    """Render the system prompt, optionally including remembered facts."""
    now = now or dt.datetime.now()
    os_name = "Windows" if platform.system() == "Windows" else platform.system() or "desktop"
    facts_block = ""
    if facts:
        lines = "\n".join(f"- {f}" for f in facts)
        facts_block = (
            "\n\nSaved memories that may be relevant (use them when they help, "
            f"don't recite them unprompted):\n{lines}"
        )
    return SYSTEM_TEMPLATE.format(
        name=name,
        user=user,
        os_name=os_name,
        now=now.strftime("%A %d %B %Y, %I:%M %p (%Y-%m-%d %H:%M)").replace(" 0", " ", 1),
        facts=facts_block,
        max_calls=max_calls,
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

    Other providers subclass this and override the client, streaming, ``ping``,
    ``complete`` and the error hooks; the tool loop and history stay shared.

    Args:
        config: Loaded :class:`core.config.Config`.
        registry: Enabled tools.
        gate: Safety gate used for dangerous tools and the audit log.
        client: Optional pre-built Anthropic client (tests inject a fake).
        facts_provider: Returns remembered facts relevant to the user's text.
        on_turn: Called after each answered request with (text, reply, tool calls),
            e.g. to store command history.
    """

    #: Exceptions meaning the API couldn't be reached (switches to offline mode).
    CONNECTION_ERRORS: tuple[type[BaseException], ...] = (anthropic.APIConnectionError,)
    #: Any other error raised by the provider's SDK.
    API_ERRORS: tuple[type[BaseException], ...] = (anthropic.AnthropicError,)

    def __init__(
        self,
        config: Any,
        registry: ToolRegistry,
        gate: SafetyGate,
        client: Any | None = None,
        facts_provider: FactsProvider | None = None,
        on_turn: TurnCallback | None = None,
    ) -> None:
        self.config = config
        self.registry = registry
        self.gate = gate
        self.facts_provider = facts_provider
        self.on_turn = on_turn
        #: False while the API is unreachable: simple commands are then handled locally.
        self.online = True
        #: Called when a request fails because the API can't be reached / works again.
        self.on_connection_lost: Callable[[], None] | None = None
        self.on_connection_restored: Callable[[], None] | None = None
        #: While offline, requests the local router can't handle retry the API after this long.
        self.offline_retry_s = 20.0
        self._last_connection_error = 0.0
        self.offline_router = OfflineRouter()
        self.max_tool_calls: int = int(config.get("llm.max_tool_calls", 6))
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

    def ping(self, timeout: float = 6.0) -> str:
        """Cheap reachability check: "ok", "offline", "auth" (bad key) or "error"."""
        try:
            self.client.with_options(timeout=timeout, max_retries=0).models.retrieve(self.model)
            return "ok"
        except (anthropic.APIConnectionError, anthropic.APITimeoutError):
            return "offline"
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError):
            return "auth"
        except anthropic.NotFoundError:
            return "ok"  # reachable; the model name problem surfaces on the first request
        except Exception:  # noqa: BLE001
            log.exception("API ping failed")
            return "error"

    def complete(self, prompt: str, max_tokens: int = 2000) -> str:
        """One-off text completion without tools or history (used by tools like summarize_clipboard).

        Raises:
            AgentError: With a speakable message if the API call fails.
        """
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
            "thinking": {"type": "adaptive"},
            "output_config": {"effort": "low"},
        }
        if self.use_fallback:
            kwargs["betas"] = [FALLBACK_BETA]
            kwargs["fallbacks"] = "default"
        try:
            message = self.client.beta.messages.create(**kwargs)
        except anthropic.AnthropicError as exc:
            raise friendly_api_error(exc) from exc
        if message.stop_reason == "refusal":
            raise AgentError("I can't summarise that one.")
        return "".join(b.text for b in message.content if b.type == "text").strip()

    # -- internals ---------------------------------------------------------

    def _friendly_error(self, exc: Exception) -> AgentError:
        return friendly_api_error(exc)

    def _system_prompt(self, user_text: str) -> str:
        facts: list[str] = []
        if self.facts_provider is not None:
            try:
                facts = self.facts_provider(user_text)
            except Exception:  # noqa: BLE001 - memory problems must not block replies
                log.exception("Facts provider failed")
        return build_system_prompt(self.name, self.user, facts, max_calls=self.max_tool_calls)

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
        if not self.online:
            local = self._try_local(user_text, emit)
            if local is not None:
                return local
            if time.monotonic() - self._last_connection_error < self.offline_retry_s:
                emit(OFFLINE_HELP)
                return Reply(OFFLINE_HELP, error=True)
            # Long enough since the last failure: try the API again.
        system = self._system_prompt(user_text)
        past = [m for turn in self._history[-self.max_turns :] for m in turn]
        turn: list[dict[str, Any]] = [{"role": "user", "content": user_text}]
        spoken: list[str] = []
        tool_calls = 0
        call_log: list[dict[str, Any]] = []

        def on_chunk(chunk: str) -> None:
            spoken.append(chunk)
            emit(chunk)

        try:
            for round_no in range(self.max_rounds + 1):
                status("thinking", "")
                message = self._stream_once(system, past + turn, on_chunk)
                if not self.online:
                    self._set_online()
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
                    self._notify_turn(user_text, "".join(spoken), call_log)
                    return Reply("".join(spoken), tool_calls=tool_calls)

                if round_no == self.max_rounds:
                    raise AgentError("I've gone around in circles on that one. Let's try a simpler request.")

                if spoken and not "".join(spoken).endswith((" ", "\n")):
                    on_chunk(" ")
                budget = max(0, self.max_tool_calls - tool_calls)
                results = self._run_tools(tool_uses[:budget], status)
                results += [
                    ToolResult(
                        b.id,
                        b.name,
                        f"Not run: the limit of {self.max_tool_calls} tool calls per request was reached. "
                        "Answer with what you have.",
                        True,
                    )
                    for b in tool_uses[budget:]
                ]
                tool_calls += min(len(tool_uses), budget)
                call_log += [{"tool": b.name, "args": b.input} for b in tool_uses[:budget]]
                turn.append({"role": "user", "content": [r.to_api() for r in results]})
                if self._cancel.is_set():
                    self._commit(turn, "".join(spoken), interrupted=True)
                    return Reply("".join(spoken), interrupted=True, tool_calls=tool_calls)

            raise AgentError("I've gone around in circles on that one.")  # pragma: no cover

        except AgentError as err:
            log.error("Agent error: %s", err)
            return self._fail(turn, spoken, err, emit, tool_calls)
        except self.CONNECTION_ERRORS as exc:  # includes timeouts
            log.error("API unreachable: %r", exc)
            self.online = False
            self._last_connection_error = time.monotonic()
            if self.on_connection_lost is not None:
                try:
                    self.on_connection_lost()
                except Exception:  # noqa: BLE001
                    log.exception("on_connection_lost failed")
            if tool_calls == 0 and not spoken:
                local = self._try_local(user_text, emit)
                if local is not None:
                    return local
            return self._fail(turn, spoken, self._friendly_error(exc), emit, tool_calls)
        except self.API_ERRORS as exc:
            log.error("%s API error: %r", llm_provider(self.config).capitalize(), exc)
            return self._fail(turn, spoken, self._friendly_error(exc), emit, tool_calls)
        except Exception as exc:  # noqa: BLE001 - never crash the main loop
            log.exception("Unexpected error while answering")
            return self._fail(turn, spoken, self._friendly_error(exc), emit, tool_calls)

    def _set_online(self) -> None:
        self.online = True
        log.info("API reachable again")
        if self.on_connection_restored is not None:
            try:
                self.on_connection_restored()
            except Exception:  # noqa: BLE001
                log.exception("on_connection_restored failed")

    def _try_local(self, text: str, emit: TextCallback) -> Reply | None:
        """Handle a simple command without the API (degraded mode). None if not understood."""
        command = self.offline_router.match(text)
        if command is None:
            return None
        log.info("Handling locally (offline): %r -> %s", text, command.tool or "answer")
        calls: list[dict[str, Any]] = []
        if command.answer is not None:
            reply = command.answer
        else:
            spec = self.registry.get(command.tool or "")
            if spec is None:
                return None
            clean, error = validate_args(spec, command.args)
            if error:
                return None
            decision = self.gate.review(spec, clean or {})
            if not decision.allowed:
                reply = "Blocked by my safety rules." if decision.blocked else "Okay, I'll leave it."
            else:
                result = self.registry.run("local", spec.name, clean)
                self.gate.record(spec.name, clean or {}, result=result.content, is_error=result.is_error,
                                 confirmed=decision.confirmed, duration_s=result.duration_s)
                calls.append({"tool": spec.name, "args": clean, "offline": True})
                reply = (
                    f"That didn't work: {result.content.removeprefix('Error: ')}" if result.is_error
                    else result.content
                )
        emit(reply)
        self._commit([{"role": "user", "content": text}], reply)
        self._notify_turn(text, reply, calls)
        return Reply(reply, tool_calls=len(calls))

    def _notify_turn(self, text: str, reply: str, calls: list[dict[str, Any]]) -> None:
        if self.on_turn is None:
            return
        try:
            self.on_turn(text, reply, calls)
        except Exception:  # noqa: BLE001 - history problems must not break replies
            log.exception("on_turn callback failed")

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
        """Validate, pass through the safety gate, and execute tool calls (parallel where possible)."""
        # (block, future-or-result, args, confirmed) - blocked/declined calls are logged by the gate.
        pending: list[tuple[Any, Any, dict[str, Any] | None, bool | None]] = []
        for block in tool_uses:
            name, args = block.name, block.input
            spec = self.registry.get(name)
            if spec is None:
                pending.append((block, ToolResult(block.id, name, f"Unknown tool '{name}'", True), {}, None))
                continue
            clean, error = validate_args(spec, args)
            if error:
                result = ToolResult(block.id, name, f"Invalid arguments: {error}", True)
                pending.append((block, result, args if isinstance(args, dict) else {"raw": args}, None))
                continue
            decision = self.gate.review(spec, clean or {})
            if not decision.allowed:
                pending.append((block, ToolResult(block.id, name, decision.denial_message(), True), None, None))
                continue
            status("tool", name)
            pending.append((block, self.registry.submit(block.id, name, clean), clean or {}, decision.confirmed))

        results: list[ToolResult] = []
        for block, item, logged_args, confirmed in pending:
            result = item if isinstance(item, ToolResult) else self.registry.wait(item, block.id, block.name)
            if logged_args is not None:  # None = the gate already logged it
                self.gate.record(
                    block.name,
                    logged_args,
                    result=result.content,
                    is_error=result.is_error,
                    confirmed=confirmed,
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


def create_agent(config: Any, registry: ToolRegistry, gate: SafetyGate, **kwargs: Any) -> Agent:
    """Build the agent for ``llm.provider`` (anthropic or gemini)."""
    provider = llm_provider(config)
    if provider == "gemini":
        from core.gemini_agent import GeminiAgent

        return GeminiAgent(config, registry, gate, **kwargs)
    if provider != "anthropic":
        raise AgentError(
            "The AI provider in config.yaml isn't one I know.",
            f"Unknown llm.provider '{provider}'. Use anthropic or gemini.",
        )
    return Agent(config, registry, gate, **kwargs)
