# Jarvis - personal voice assistant for Windows

Say "Jarvis", ask for something, and it uses tools to control your PC and
answers out loud. Built with Claude (Anthropic API) and local speech models.

> **Status: Phase 2 of 5 - push-to-talk voice.** Hold F9, speak, release;
> Jarvis answers out loud. The wake word ("Jarvis") arrives in Phase 3.

## Setup (Windows, Python 3.11+)

```powershell
cd jarvis
py -3.11 -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env        # then paste your key into .env
```

Get an API key at https://platform.claude.com/settings/keys and put it in
`jarvis\.env` as `ANTHROPIC_API_KEY=...`.

Voice needs nothing beyond `pip install -r requirements.txt`: the Windows
wheels of `sounddevice` and `soundfile` include PortAudio and an MP3 decoder,
so **ffmpeg is not required**. The first voice run downloads the Whisper
`small.en` model (about 500 MB) to the Hugging Face cache, once.

## Run

```powershell
python main.py --list-devices   # find your mic/speaker indexes
python main.py                  # push-to-talk voice mode (same as --push-to-talk)
python main.py --text           # type to Jarvis (debug mode)
python main.py --text --speak   # type, and hear the replies
python main.py --debug          # any mode, with verbose logs in logs\jarvis.log
python -m pytest                # run the tests
```

In text mode: `/tools` lists tools, `/reset` clears the conversation, `/quit` exits.

### Push-to-talk

Hold **F9**, speak, release. You'll hear a high beep when recording starts and
a lower one when it stops. Taps shorter than 0.3 s are ignored. Press F9 while
Jarvis is talking to cut it off and ask something else (barge-in). For risky
actions Jarvis asks a yes/no question; hold F9 and answer. Ctrl+C quits and
releases the mic and hotkey.

Replies start playing after the first sentence arrives: the streamed reply
is split into sentences and each one goes to text-to-speech straight away,
while the next is synthesised in the background.

**Windows note on the hotkey:** the `keyboard` library works without admin
rights for normal windows. If F9 does nothing while an *elevated* window
(e.g. an admin terminal or Task Manager) is focused, either run Jarvis as
administrator too, or set `hotkey_backend: pynput` in `config.yaml`.

### Voice troubleshooting

| Problem | Fix |
|---|---|
| "No microphone found" | Plug in a mic; check Settings > Privacy & security > Microphone > "Let desktop apps access your microphone" |
| Wrong mic or speaker | `python main.py --list-devices`, then set `audio.input_device` / `audio.output_device` (index or part of the name) |
| F9 does nothing | See the Windows note above, or change `hotkey` (e.g. `"right ctrl"`) |
| Transcription slow | `stt.model: base.en` (or `tiny.en`) |
| "didn't catch that" a lot | Speak closer to the mic, hold the key a moment longer, or lower `stt.min_rms` |
| GPU errors on startup | Jarvis falls back to CPU automatically; force it with `stt.device: cpu` |
| No speech, text only | edge-tts needs internet. Jarvis keeps printing replies while it's offline |
| Different voice | `edge-tts --list-voices`, then set `tts.voice` (e.g. `en-US-GuyNeural`) |

## Tools so far

| Tool | What it does |
|---|---|
| `open_app` | Launches an app by name (aliases in `config.yaml` -> `tools.apps`) |
| `system_stats` | CPU, RAM, disk and battery |
| `open_url` | Opens a website in your default browser |
| `web_search` | DuckDuckGo search, top results |
| `list_folder` | Lists a folder (desktop, downloads, ... or any path) |

## Adding a tool

Write one function anywhere in `tools/` with type hints and a docstring. That's it:

```python
from tools import tool

@tool
def flip_coin(times: int = 1) -> str:
    """Flip a coin and report the results.

    Args:
        times: How many flips.
    """
    import random
    return ", ".join(random.choice(["heads", "tails"]) for _ in range(times))
```

The decorator builds the JSON schema from the type hints (`str`, `int`,
`float`, `bool`, `list[str]`, `Optional[...]`, `Literal[...]`) and the
description from the docstring (the `Args:` section becomes parameter
descriptions). Use `@tool(dangerous=True)` for anything destructive, so Jarvis
asks "yes or no?" first. Disable tools without code changes via
`tools.disabled` / `tools.disabled_modules` in `config.yaml`.

## Files and logs

- `config.yaml` - all settings; `.env` - your API key only.
- `logs/jarvis.log` - main log (rotating).
- `logs/tool_calls.jsonl` - one line per tool call: arguments, approval, result.
- Transcription times are logged ("Transcribed 2.1s of audio in 0.45s").

## Defaults chosen (change in `config.yaml`)

- Model `claude-sonnet-5-5` at effort `low` for snappy spoken replies; raise
  `llm.effort` to `medium` if answers feel shallow.
- Last 10 exchanges kept as context (`llm.max_history_turns`).
- `llm.refusal_fallback: true` - if the model declines a request, the API
  retries it on a fallback model inside the same call.
- Jarvis calls you "sir" until you set `assistant.user_name`.
- Tool calls time out after 30 s (`tools.timeout_s`).
- Voice: Whisper `small.en` with greedy decoding (`stt.beam_size: 1`) for
  speed, on your GPU if CUDA works, otherwise CPU int8. Voice
  `en-GB-RyanNeural` via edge-tts. F9 push-to-talk.
- Spoken confirmations: anything other than a clear "yes" (including
  silence for 20 seconds) counts as "no".
