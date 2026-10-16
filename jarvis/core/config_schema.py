"""Validation of config.yaml with pydantic: clear messages for bad or missing values.

Every setting is optional (the code has defaults), but a value that *is*
present must have the right type and range. Unknown keys inside a known
section are rejected, because they are almost always typos
("silense_timeout"). Unknown top-level sections are allowed, so your own
tools can read their own settings.
"""

from __future__ import annotations

from typing import Any, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

Prob = float  # documented as 0-1 via Field constraints below


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Assistant(_Section):
    name: Optional[str] = None
    user_name: Optional[str] = None


class LLM(_Section):
    model: Optional[str] = None
    max_tokens: Optional[int] = Field(None, ge=256, le=128_000)
    effort: Optional[Literal["low", "medium", "high", "xhigh", "max"]] = None
    max_history_turns: Optional[int] = Field(None, ge=0, le=100)
    max_tool_rounds: Optional[int] = Field(None, ge=1, le=50)
    max_tool_calls: Optional[int] = Field(None, ge=1, le=50)
    request_timeout_s: Optional[float] = Field(None, gt=0)
    max_retries: Optional[int] = Field(None, ge=0, le=10)
    refusal_fallback: Optional[bool] = None


class Tools(_Section):
    timeout_s: Optional[float] = Field(None, gt=0)
    disabled: Optional[list[str]] = None
    disabled_modules: Optional[list[str]] = None
    apps: Optional[dict[str, str]] = None  # Phase 1 location, still accepted
    folders: Optional[dict[str, str]] = None


DeviceSpec = Union[int, str, None]


class Audio(_Section):
    input_device: DeviceSpec = None
    output_device: DeviceSpec = None
    sample_rate: Optional[int] = Field(None, ge=8000, le=48000)
    min_press_s: Optional[float] = Field(None, ge=0, le=5)
    beeps: Optional[bool] = None
    headphones: Optional[bool] = None
    echo_tail: Optional[float] = Field(None, ge=0, le=3)


class Wake(_Section):
    model: Optional[str] = None
    threshold: Optional[float] = Field(None, gt=0, lt=1)
    cooldown: Optional[float] = Field(None, ge=0, le=30)
    listen_timeout: Optional[float] = Field(None, gt=0, le=60)


class VAD(_Section):
    engine: Optional[Literal["silero", "energy"]] = None
    threshold: Optional[float] = Field(None, gt=0, lt=1)
    silence_timeout: Optional[float] = Field(None, ge=0.2, le=10)
    max_utterance: Optional[float] = Field(None, ge=1, le=120)
    min_speech: Optional[float] = Field(None, ge=0, le=5)
    preroll: Optional[float] = Field(None, ge=0, le=3)


class FollowUp(_Section):
    enabled: Optional[bool] = None
    window: Optional[float] = Field(None, ge=1, le=60)


class BargeIn(_Section):
    min_speech: Optional[float] = Field(None, ge=0.1, le=5)


class STT(_Section):
    model: Optional[str] = None
    device: Optional[Literal["auto", "cuda", "cpu"]] = None
    compute_type: Optional[str] = None
    beam_size: Optional[int] = Field(None, ge=1, le=10)
    download_root: Optional[str] = None
    min_rms: Optional[float] = Field(None, ge=0, le=1)


class TTS(_Section):
    engine: Optional[Literal["edge", "piper", "elevenlabs"]] = None
    voice: Optional[str] = None
    rate: Optional[str] = Field(None, pattern=r"^[+-]\d{1,3}%$")
    volume: Optional[str] = Field(None, pattern=r"^[+-]\d{1,3}%$")
    filler: Optional[bool] = None
    filler_delay: Optional[float] = Field(None, ge=0, le=30)
    fillers: Optional[list[str]] = None


class Project(_Section):
    path: Optional[str] = None
    dev_command: Optional[str] = None


class Weather(_Section):
    city: Optional[str] = None
    lat: Optional[float] = Field(None, ge=-90, le=90)
    lon: Optional[float] = Field(None, ge=-180, le=180)
    units: Optional[Literal["celsius", "fahrenheit"]] = None


class Briefing(_Section):
    include: Optional[list[Literal["weather", "news", "todos", "reminders"]]] = None


class Safety(_Section):
    confirm_dangerous: Optional[bool] = None
    confirm_timeout: Optional[float] = Field(None, ge=2, le=60)
    shell_allowlist: Optional[list[str]] = None
    auto_approve_allowlist: Optional[bool] = None


class Paths(_Section):
    logs_dir: Optional[str] = None
    data_dir: Optional[str] = None


class Logging(_Section):
    level: Optional[Literal["DEBUG", "INFO", "WARNING", "ERROR"]] = None

    @field_validator("level", mode="before")
    @classmethod
    def _upper(cls, v: Any) -> Any:
        return v.upper() if isinstance(v, str) else v


Corner = Literal["top-left", "top-right", "bottom-left", "bottom-right"]


class UI(_Section):
    tray: Optional[bool] = None
    overlay: Optional[bool] = None
    overlay_position: Optional[Corner] = None
    overlay_fade_s: Optional[float] = Field(None, ge=1, le=60)


class Startup(_Section):
    greeting: Optional[str] = None
    time_greeting: Optional[bool] = None
    chime: Optional[bool] = None
    auto_briefing: Optional[bool] = None


class JarvisConfig(BaseModel):
    """The whole file. Top-level extras are allowed (custom tool settings)."""

    model_config = ConfigDict(extra="allow")

    assistant: Optional[Assistant] = None
    llm: Optional[LLM] = None
    tools: Optional[Tools] = None
    audio: Optional[Audio] = None
    hotkey: Optional[str] = Field(None, min_length=1)
    hotkey_backend: Optional[Literal["auto", "keyboard", "pynput"]] = None
    wake: Optional[Wake] = None
    vad: Optional[VAD] = None
    follow_up: Optional[FollowUp] = None
    barge_in: Optional[BargeIn] = None
    stt: Optional[STT] = None
    tts: Optional[TTS] = None
    apps: Optional[dict[str, str]] = None
    safe_roots: Optional[list[str]] = None
    notes_dir: Optional[str] = None
    screenshots_dir: Optional[str] = None
    projects: Optional[dict[str, Optional[Project]]] = None
    weather: Optional[Weather] = None
    news_feeds: Optional[list[str]] = None
    briefing: Optional[Briefing] = None
    safety: Optional[Safety] = None
    paths: Optional[Paths] = None
    logging: Optional[Logging] = None
    ui: Optional[UI] = None
    startup: Optional[Startup] = None

    @field_validator("news_feeds")
    @classmethod
    def _urls(cls, v: Optional[list[str]]) -> Optional[list[str]]:
        for url in v or []:
            if not url.startswith(("http://", "https://")):
                raise ValueError(f"'{url}' is not an http(s) URL")
        return v


def _loc(loc: tuple[Any, ...]) -> str:
    return ".".join(str(p) for p in loc if not str(p).startswith(("function-", "literal[", "int", "str", "none")))


def _message(err: dict[str, Any]) -> str:
    kind = err.get("type", "")
    loc = _loc(err["loc"]) or "(top level)"
    got = err.get("input")
    if kind == "extra_forbidden":
        return f"{loc}: unknown setting (typo?)"
    if kind == "string_pattern_mismatch":
        return f"{loc}: must look like '+10%' or '-5%' (got {got!r})"
    if kind == "literal_error":
        expected = err.get("ctx", {}).get("expected", "")
        return f"{loc}: must be one of {expected} (got {got!r})"
    return f"{loc}: {err['msg'].removeprefix('Value error, ')} (got {got!r})"


def validate_config(data: dict[str, Any]) -> list[str]:
    """Return a list of human-readable problems (empty if the config is valid)."""
    try:
        JarvisConfig.model_validate(data)
    except ValidationError as exc:
        seen, problems = set(), []
        for err in exc.errors():
            msg = _message(err)
            # Union types report one error per branch; keep the first per location.
            key = _loc(err["loc"])
            if key in seen and err.get("type") != "extra_forbidden":
                continue
            seen.add(key)
            problems.append(msg)
        return problems
    return []
