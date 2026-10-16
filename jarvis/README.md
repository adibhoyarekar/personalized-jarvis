# Jarvis - personal voice assistant for Windows

Say "Jarvis", ask for something, and it uses tools to control your PC and
answers out loud. Built with Claude (Anthropic API) and local speech models.

> **Status: Phase 4 of 5 - full toolset.** Hands-free voice, 51 tools,
> long-term memory, reminders that survive restarts, and a safety gate for
> anything risky. The tray icon and run-at-startup arrive in Phase 5.

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

## Example voice commands

Say "Hey Jarvis" first (or hold F9). Inside the follow-up window you can skip it.

**PC control**
1. "Open Chrome." / "Open Excel." (Start Menu names work too)
2. "Close Notepad." *(asks you to confirm)*
3. "Set the volume to 30." / "Mute." / "Unmute."
4. "Turn the brightness down to 40 percent."
5. "How's my computer doing?" (CPU, RAM, disk, battery)
6. "Take a screenshot."
7. "Lock my PC."
8. "Shut down the computer." *(confirm; 30 s grace)* then "Cancel the shutdown."

**Files** (only inside `safe_roots`)
9. "Find the invoice from March in my downloads."
10. "What's on my desktop?"
11. "Read me notes.txt from Documents."
12. "Take a note: call the plumber on Monday."
13. "Delete old-report.docx." *(confirm; goes to the Recycle Bin)*

**Web**
14. "What's the weather like?" / "Will it rain in Pune tomorrow?"
15. "What's in the news?" / "Any cricket news?"
16. "Search the web for the latest Python release."

**Projects**
17. "Open cs-events." (VS Code)
18. "What's the git status of cs-events?"
19. "Run npm test in cs-events." *(confirm; allowlisted programs only)*
20. "Start the cs-events dev server." / "Stop the dev server."

**Productivity**
21. "Set a timer for 10 minutes for the pasta."
22. "Remind me to call mom at 6 pm." / "What reminders do I have?"
23. "Add renew passport to my to-do list." / "What's on my to-do list?" / "Mark passport as done."
24. "Give me my daily briefing."

**Media, clipboard, memory**
25. "Pause the music." / "Next track." / "Play lofi beats on YouTube." / "Open Spotify and search Arijit Singh."
26. "Summarize what's on my clipboard."
27. "Remember that my locker code is 4512." / "What's my locker code?" / "Forget my locker code." *(confirm)*
28. "Do that again." (Jarvis checks your recent commands)

## Tools

Each tool is one Python function with `@tool`. Run `python main.py --text`
then `/tools` to list what's loaded. **Bold** = asks for confirmation first.

| Module | Tools |
|---|---|
| `system_tools` | `open_app` (config aliases, then fuzzy Start Menu match, then PATH), **`close_app`**, `set_volume`, `get_volume`, `mute`, `unmute`, `set_brightness`, `lock_pc`, `system_stats`, `screenshot`, **`shutdown_pc`**, **`restart_pc`** (30 s delay), `cancel_shutdown` |
| `file_tools` | `search_files` (≤ 20 results), `open_file` (refuses programs/scripts), `read_text_file` (size-capped), `create_note`, `list_folder`, **`delete_file`** (Recycle Bin only) |
| `web_tools` | `web_search` (DuckDuckGo, top 5), `open_url`, `get_weather` (Open-Meteo, no key), `get_news` (your RSS feeds) |
| `dev_tools` | `open_project`, **`run_command_in_project`** (allowlist + confirm), `git_status`, **`git_pull`** (fast-forward only), `start_dev_server` (new terminal window), `stop_dev_server` |
| `productivity_tools` | `set_timer`, `set_reminder`, `list_reminders`, `cancel_reminder`, `add_todo`, `read_todos`, `complete_todo`, `daily_briefing` |
| `media_tools` | `play_pause`, `next_track`, `previous_track`, `media_volume`, `search_youtube`, `open_spotify` |
| `clipboard_tools` | `read_clipboard`, `write_clipboard`, `summarize_clipboard` (2-sentence summary by Claude) |
| `memory_tools` | `remember`, `recall`, **`forget`**, `list_memories`, `recent_commands` |

At most **6 tool calls per request** (`llm.max_tool_calls`) to stop loops. If
a tool fails, the model gets a short error message and explains it in plain
words; it's told never to claim success unless the tool reported it.

## Memory

Everything lives in one SQLite file, `data/jarvis.db`:

- **facts**: "remember that my wifi password is ...". Before every request,
  the 5 facts with the most keyword overlap with what you said are added to
  Claude's instructions. `recall` also matches typos ("wi fi pasword").
- **preferences**: "remember that I prefer Celsius" (saved as a preference)
  are always included.
- **command_history**: your last 200 requests and the tools they used, for
  "do that again".
- **todos** and **reminders**: reminders and timers fire with a spoken
  message plus a Windows notification. They're stored, so a reminder set
  before a restart still fires afterwards. Anything that came due while
  Jarvis was off is announced as "missed" at the next start.

## Safety gate

- **Confirmation:** tools marked `dangerous=True` make Jarvis say what it's about to do
  ("I'm about to delete report.docx. It goes to the Recycle Bin. Confirm?")
  and wait 8 s (`safety.confirm_timeout`) for an answer, spoken or typed in
  text mode. "Yes", "yeah", "confirm", "do it" and "go ahead" approve. "No",
  "cancel", "stop", "don't", anything unclear, or silence mean no.
- **Shell commands** (`run_command_in_project`):
  - **Denylist:** always blocked, whatever you answer. This covers
    `format`, `del /s`, `rd /s`, `rm -rf`, `shutdown`, `reg delete`,
    `diskpart`, `bcdedit` and similar, plus anything that mentions system
    folders (`C:\Windows`, `System32`, `Program Files`, `ProgramData`,
    a bare drive root).
  - **One command at a time:** chaining (`&&`, `;`, `|`), redirection
    (`>`) and substitution are blocked too.
  - **Allowlist:** the first word must be on `safety.shell_allowlist`
    (default `git, npm, npx, python, pip, node, ls, dir`). Anything else is
    blocked with a spoken explanation.
  - **Allowlisted commands still ask first**, unless you set
    `safety.auto_approve_allowlist: true`.
- **File tools** are confined to `safe_roots`. `..`, symlinks, drive paths
  and absolute paths elsewhere are rejected before anything is touched.
- **Audit log:** every tool call is written to `logs/tool_calls.log` as one
  JSON line: time, tool, arguments, whether you confirmed, whether it was
  blocked, and the result. The log rotates at 1 MB and keeps 5 old files.

## Config reference (`config.yaml`)

| Setting | What it does |
|---|---|
| `apps` | Spoken name -> program for `open_app` (`chrome: chrome.exe`) |
| `safe_roots` | The only folders file tools may touch |
| `notes_dir`, `screenshots_dir` | Where notes and screenshots go |
| `projects` | `name: {path, dev_command}` for the dev tools |
| `weather` | Default `city`, `lat`, `lon`, `units` |
| `news_feeds` | RSS feed URLs for `get_news` and the briefing |
| `briefing.include` | Any of `weather, news, todos, reminders` |
| `safety.*` | `confirm_dangerous`, `confirm_timeout`, `shell_allowlist`, `auto_approve_allowlist` |
| `llm.max_tool_calls` | Tool calls allowed per request (default 6) |
| `tools.disabled` / `tools.disabled_modules` | Hide tools from the model without code changes |
| `tools.folders` | Spoken folder names ("downloads") for file tools |
| `wake.*`, `vad.*`, `follow_up.*`, `audio.*`, `stt.*`, `tts.*` | Voice settings (see the sections above) |

## Adding a tool

Write one function anywhere in `tools/` (a new file works too) with type hints and a docstring. That's it:

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
descriptions). For anything destructive use
`@tool(dangerous=True, summary="delete {path}")`, so Jarvis says "I'm about to
delete report.docx. Confirm?" first. A tool that runs shell commands declares
`shell_arg="command"` so the denylist/allowlist applies. Tools can reach
memory with `get_memory()`, settings with `setting("key")`, and speak later
with `notify("...")`. Disable tools without code changes via
`tools.disabled` / `tools.disabled_modules` in `config.yaml`.

## Files and logs

- `config.yaml` - all settings; `.env` - your API key only.
- `logs/jarvis.log` - main log (rotating).
- `logs/tool_calls.log` - one JSON line per tool call (rotating).
- `data/jarvis.db` - memory, command history, todos and reminders.
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
- Confirmations: anything other than a clear "yes" (including 8 seconds of
  silence) counts as "no".
- Wake word `hey_jarvis` at threshold 0.5 with a 1.5 s cooldown. Plain
  "Jarvis" would need a custom-trained model (`wake.model` accepts a path to
  one).
- Voice detection: Silero VAD (bundled with faster-whisper) with an energy
  fallback; 1.0 s silence ends a request, 15 s maximum, 0.5 s pre-roll.
- Follow-up window 6 s. Exit phrases ("thanks", "that's all", "stop
  listening", "no", ...) are answered locally, with no API call.
- Speakers assumed (`audio.headphones: false`), so voice barge-in is off.
- File tools limited to Documents, Downloads and Desktop; notes go to
  `Documents\Jarvis Notes`, screenshots to `Pictures\Jarvis Screenshots`.
- Shell allowlist `git, npm, npx, python, pip, node, ls, dir`, always asking
  first. Note that `python` and `node` can run any script, so keep the
  confirmation on.
- Web search uses the `ddgs` package, the current name of `duckduckgo-search`.
- Weather defaults to Chhatrapati Sambhajinagar in Celsius.
