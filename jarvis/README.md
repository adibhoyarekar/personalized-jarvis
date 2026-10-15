# Jarvis - personal voice assistant for Windows

Say "Jarvis", ask for something, and it uses tools to control your PC and
answers out loud. Built with Claude (Anthropic API) and local speech models.

> **Status: Phase 3 of 5 - hands-free.** Say "Hey Jarvis", ask, and keep
> talking; F9 push-to-talk still works alongside. More tools, memory and the
> full safety gate arrive in Phase 4.

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
`small.en` model (about 500 MB) to the Hugging Face cache, and the
`hey_jarvis` wake word model (a few MB) into the openwakeword package folder.
Both happen once. The voice-activity model (Silero) ships with faster-whisper,
so there's no torch download.

## Run

```powershell
python main.py --list-devices   # find your mic/speaker indexes
python main.py                  # hands-free: "Hey Jarvis" (same as --wake); F9 also works
python main.py --debug          # same, printing state changes and live wake word scores
python main.py --push-to-talk   # F9 only, no wake word
python main.py --text           # type to Jarvis (debug mode)
python main.py --text --speak   # type, and hear the replies
python -m pytest                # run the tests
```

In text mode: `/tools` lists tools, `/reset` clears the conversation, `/quit` exits.

### Hands-free (wake word)

1. Say **"Hey Jarvis"**. A two-note chime means it heard you.
2. Say your request. It stops recording after about a second of silence
   (`vad.silence_timeout`). If you say nothing for 5 s it quietly goes back
   to sleep.
3. Jarvis answers. For the next **6 seconds** you can just talk, no wake word
   needed ("and what about tomorrow?"). Say "thanks", "that's all" or
   "stop listening" to end early; silence ends it too.

You can also say it in one breath ("Hey Jarvis, open Spotify"). If the wake
word model can't load (no internet on first run, say), Jarvis prints a
warning and falls back to push-to-talk.

The flow is a small state machine, `IDLE -> WAKE_DETECTED -> LISTENING ->
THINKING -> SPEAKING -> FOLLOW_UP -> IDLE`, in `core/state.py`. `--debug`
prints every transition, and the tray icon (Phase 5) subscribes to it.

If a tool is slow (2.5 s with no reply yet), Jarvis says a short filler such
as "One moment." Turn this off with `tts.filler: false`.

### Speakers or headphones? (`audio.headphones`)

The microphone can't tell your voice from Jarvis's voice coming out of your
speakers, so interrupting Jarvis by voice ("barge-in") only works reliably
with headphones.

| | `headphones: false` (default, speakers) | `headphones: true` |
|---|---|---|
| Interrupt by saying "Hey Jarvis" while it talks | No, detection pauses while Jarvis speaks | Yes |
| Interrupt by just talking over it (0.4 s+) | No | Yes |
| Interrupt with F9 | Yes | Yes |
| Risk of Jarvis hearing itself | None (and the first 0.3 s after it stops is ignored) | High if you're actually on speakers |

On speakers, follow-up listening starts right after Jarvis stops talking. On
headphones you can cut in at any point.

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
| Wake word fires by itself | Raise `wake.threshold` to 0.6-0.7 (watch scores with `--debug`) |
| Wake word misses you | Lower `wake.threshold` to 0.35-0.4; raise mic level in Windows Sound settings |
| Cuts you off mid-sentence | Raise `vad.silence_timeout` to 1.3-1.5 |
| Waits too long after you stop | Lower `vad.silence_timeout` to 0.7-0.8 |
| Jarvis hears itself | Keep `audio.headphones: false` on speakers; raise `audio.echo_tail` to 0.5 |
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
- Wake word `hey_jarvis` at threshold 0.5 with a 1.5 s cooldown. Plain
  "Jarvis" would need a custom-trained model (`wake.model` accepts a path to
  one).
- Voice detection: Silero VAD (bundled with faster-whisper) with an energy
  fallback; 1.0 s silence ends a request, 15 s maximum, 0.5 s pre-roll.
- Follow-up window 6 s. Exit phrases ("thanks", "that's all", "stop
  listening", "no", ...) are answered locally, with no API call.
- Speakers assumed (`audio.headphones: false`), so voice barge-in is off.
