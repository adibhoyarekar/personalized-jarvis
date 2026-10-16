# CLAUDE.md

Guidance for Claude Code sessions working on this repository.

## What this is

**Jarvis**, a hands-free voice assistant for Windows (Python 3.11+). All code
lives in `jarvis/`; run every command from there.

Pipeline: wake word (openWakeWord) → VAD endpointing (Silero ONNX bundled
with faster-whisper) → speech-to-text (faster-whisper, local) → Claude with
tool use (Anthropic SDK, streaming) → text-to-speech (edge-tts), sentence by
sentence. It can also run in text mode.

## Commands

```bash
cd jarvis
python -m pytest                 # full suite; needs no mic, API key or network
python -m pytest tests/test_safety.py -q
python main.py --text            # type to the agent (needs ANTHROPIC_API_KEY in jarvis/.env)
python main.py --check           # PASS/FAIL environment check
python main.py --debug           # voice mode with state + wake-score printing
```

Tests must pass on Linux too (CI-style sandboxes have no audio devices, no
display and often no internet). Windows-only code imports its libraries
lazily inside functions.

## Layout

```
jarvis/
  main.py               CLI: flags, single-instance lock, text mode, dispatch to VoiceApp
  config.yaml           all settings (validated by core/config_schema.py); secrets only in .env
  core/
    app.py              Runtime (agent+tools+memory+scheduler) and VoiceApp (tray, overlay,
                        supervisor, connectivity/offline mode, greeting, restart)
    agent.py            Claude loop: streaming, tool rounds, history, offline fallback, ping()
    gemini_agent.py     Gemini backend (llm.provider: gemini), same loop via the openai SDK
    voice.py            VoiceAssistant event loop (wake/PTT/follow-up/barge-in/confirm/mute)
    audio_in.py         mic, devices, push-to-talk, wake word, VAD, Endpointer, AudioPipeline
    stt.py / tts.py     faster-whisper wrapper / pluggable TTS engines + SpeechStream
    state.py            AssistantState enum + StateManager (subscribe for UI)
    safety.py           ShellPolicy (denylist/allowlist), SafetyGate.review, audit log
    memory.py           SQLite: facts, preferences, command_history, todos, reminders, app_state
    scheduler.py        fires reminders/timers; connectivity.py, offline.py (degraded mode)
    supervisor.py       crash restart limits; single_instance.py lock file
    checks.py           --check; setup_wizard.py + config_editor.py for --setup
    startup.py          Task Scheduler XML / Run key (scripts/ call this)
  tools/                every module is auto-discovered; _-prefixed modules are helpers
  ui/                   window.py (main window, tkinter thread), tray.py (pystray), overlay.py
  scripts/              install_startup.py, uninstall_startup.py, run_jarvis.bat
  tests/                pytest; fakes for the API client, audio and TTS live in conftest/test_voice
```

## Conventions

- **Model:** `llm.model` defaults to `claude-sonnet-5-5` (the user's choice),
  with adaptive thinking, `output_config.effort` (default `low` for voice
  latency), streaming via `client.beta.messages.stream` and
  `fallbacks="default"` with the `server-side-fallback-2026-07-01` beta.
  Don't use `budget_tokens`, forced `tool_choice`, or assistant prefill (all rejected by this model).
- **Providers:** `llm.provider: gemini` swaps in `core/gemini_agent.py` (a subclass of
  `Agent` using Gemini's OpenAI-compatible endpoint via the `openai` package). History and
  the tool loop stay Anthropic-shaped; the subclass converts messages at the edge and
  overrides `_stream_once`, `ping`, `complete` and the error hooks. Build agents with
  `core.agent.create_agent`.
- **History is append-only within a turn.** Finished turns are stored without
  thinking blocks (`_sanitize_content`) so trimming old turns never invalidates
  a replayed thinking block. Keep that invariant if you touch `Agent._commit`.
- **Tools:** one function + `@tool` in `tools/`. Type hints and a Google-style
  docstring are required (the schema is built from them). Return a short
  string or a small dict; raise with a human-readable message on failure.
  Use `dangerous=True, summary="..."` for destructive actions and
  `shell_arg=` for shell commands. File paths must go through
  `tools._paths.resolve_safe_path`. See `jarvis/docs/ADDING_TOOLS.md`.
- **Safety never fails open:** confirmation errors and timeouts mean "no";
  the denylist is checked before the allowlist and before asking.
- **Threads:** the voice loop owns the main thread. The mic callback, pipeline,
  TTS, tray, window, overlay, scheduler and connectivity monitor are background
  threads that talk to it through queues, events, or thread-safe methods
  (`VoiceAssistant.stop/set_muted/set_wake_active/submit_text`, `StateManager.set`).
  Never block the PortAudio callback. tkinter objects must be created and freed
  on their own thread (`ui/window.py` collects garbage there before exiting).
- **Config:** read with `config.get("section.key", default)`. A new setting
  needs a default in code, an entry in `config.yaml` with a comment, a field
  in `core/config_schema.py` (sections forbid unknown keys), and a README row.
- **Errors reach the user as short spoken sentences** (`AgentError.spoken`,
  `friendly_api_error`); details go to `logs/jarvis.log`.
- Style: type hints, docstrings, `logging.getLogger(__name__)`, no hard-coded
  user paths or keys, comments only where the reason isn't obvious.

## Testing tips

- `tests/conftest.py` has `FakeClient` (scripted streaming responses) and
  message/block builders for agent tests.
- `tests/test_voice.py` has fake recorder/STT/TTS/agent/pipeline objects for
  the voice loop. `core.audio_in.EnergyVAD` lets endpointing be tested on
  synthetic numpy audio.
- Network tools are tested by monkeypatching `tools.web_tools._get`.
- Add a test with every new tool. `test_all_real_tools_have_valid_schemas`
  checks every tool's schema automatically.
