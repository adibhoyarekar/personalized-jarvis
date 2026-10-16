# Jarvis: a hands-free voice assistant for Windows

Say **"Hey Jarvis"**, ask for something, and Jarvis controls your PC, looks
things up and answers out loud. It runs in your system tray, starts with
Windows, and recovers from crashes on its own.

```
"Hey Jarvis" ──► wake word ──► listen until you stop ──► speech-to-text
   (local)       openWakeWord       Silero VAD            faster-whisper (local)
                                                                │
   speak the reply ◄── text-to-speech ◄── Claude + 51 tools ◄───┘
   sentence by sentence   edge-tts         (safety gate for anything risky)
```

## Features

- **Hands-free:** a wake word, voice activity detection, a 6-second follow-up
  window with no wake word needed, and barge-in (interrupt Jarvis mid-sentence).
  F9 push-to-talk works alongside.
- **Fast:** replies stream, and speech starts after the first sentence. Whisper
  runs locally (on your GPU if it can).
- **51 tools:** apps, volume, brightness, screenshots, files, web search,
  weather, news, coding projects, git, timers, reminders, to-dos, a daily
  briefing, media keys, the clipboard and long-term memory.
- **Safe by default:** risky actions ask "Confirm?" first. Shell commands go
  through an allowlist and a hard denylist. File tools are confined to the
  folders you choose. Every tool call is logged.
- **Memory:** it remembers facts and preferences, keeps your last 200 commands,
  and stores reminders that survive restarts.
- **Desktop integration:** a dark desktop window with an animated orb and the
  live conversation (closing it hides Jarvis to the tray), a tray icon that
  changes colour with the state, an optional on-screen overlay, run-at-login,
  a single instance, automatic crash recovery, and an offline mode for simple commands.
- **Easy to extend:** a new tool is one function plus `@tool`.

## Requirements

- Windows 10 or 11, Python **3.11 or newer** (python.org installer; tick "Add to PATH").
- A microphone and speakers or headphones.
- An API key: Anthropic (Claude, paid: https://platform.claude.com/settings/keys) or
  Google Gemini (free tier: https://aistudio.google.com/apikey). See
  [Using Gemini for free](#using-gemini-for-free).
- About 1 GB of disk for the speech model. A GPU is optional.
- Internet for Claude, edge-tts voices, weather and news. Wake word and
  speech recognition run offline.

## Install

```powershell
git clone https://github.com/adibhoyarekar/personalized-jarvis
cd personalized-jarvis\jarvis
py -3.11 -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt        # or: pip install -e .
python main.py --setup                 # API key, mic, wake word, city, projects
python main.py --check                 # everything should be PASS
python main.py                         # say "Hey Jarvis"
```

No ffmpeg is needed. The first voice run downloads the Whisper `small.en`
model (~500 MB) and the `hey_jarvis` wake word model (a few MB); both
happen once.

## The setup wizard (`--setup`)

`python main.py --setup` asks, one question at a time (Enter keeps the
current value):

1. Your API key (Anthropic `sk-ant-...` or Gemini `AIza...`). It's tested, then saved to
   `jarvis\.env`, and `llm.provider` is set to match.
2. What Jarvis should call you.
3. Which microphone and which speakers, picked from a numbered list.
4. Wake word sensitivity (0.2-0.9).
5. Headphones or speakers (this decides whether you can interrupt by voice).
6. Your city for weather. It's looked up to get the coordinates.
7. Your coding projects (folder and dev-server command).
8. Extra folders the file tools may use.

The wizard edits only the lines it needs, so your comments in
`config.yaml` stay. The previous file is kept as `config.yaml.bak`, and the
result is validated before it's kept.

## Command line

| Flag | What it does |
|---|---|
| *(none)* / `--wake` | Hands-free: "Hey Jarvis" (F9 also works) |
| `--push-to-talk` | Hold F9 to talk; no wake word |
| `--text` | Type instead of talk (great for debugging); `--speak` also reads replies aloud |
| `--tray` / `--no-tray` | Show/hide the tray icon (on by default under `pythonw`) |
| `--window` / `--no-window` | Show/hide the desktop window (on by default; or set `ui.window`) |
| `--minimized` | Start with the window hidden in the tray (used at login) |
| `--overlay` | Show the on-screen overlay (or set `ui.overlay: true`) |
| `--setup` | First-run wizard |
| `--check` | PASS/FAIL table: API key, Claude reachable, mic, speakers, models, TTS, hotkey, folders, projects |
| `--list-devices` | Audio devices with their indexes |
| `--debug` | Print state changes and live wake word scores; verbose `logs\jarvis.log` |
| `--config PATH` | Use another config file |

Text mode commands: `/tools`, `/reset`, `/quit`.

## Using it

1. Say **"Hey Jarvis"**. You'll hear a two-note chime and the tray icon turns green.
2. Say your request. Recording stops after about a second of silence.
3. Jarvis answers (the icon goes amber while thinking, blue while speaking).
   For 6 seconds afterwards you can just keep talking. "Thanks", "that's all"
   or "stop listening" ends it early.

You can say it in one breath ("Hey Jarvis, open Spotify"). Press **F9** at any
time to interrupt and talk.

### Tray icon

| Colour | Meaning |
|---|---|
| Gray | Idle, waiting for "Hey Jarvis" |
| Green | Listening (wake word heard, follow-up window) |
| Amber | Thinking / running tools |
| Blue | Speaking |
| Red, crossed out | Microphone muted |
| Dark red | Error; Jarvis restarts automatically (up to 5 times in 10 minutes) |

Hover over the icon for the state and the last command. Right-click for the
menu: **Mute/Unmute microphone** (a left-click on the icon also toggles it), **Mode** (wake
word or push-to-talk only), **Open logs folder**, **Open config.yaml**,
**Open README**, **Restart Jarvis** and **Quit**.

### Speakers or headphones? (`audio.headphones`)

The microphone can't tell your voice from Jarvis's voice coming out of your
speakers, so interrupting by voice needs headphones.

| | `headphones: false` (default, speakers) | `headphones: true` |
|---|---|---|
| Interrupt by saying "Hey Jarvis" while it talks | No, listening pauses while Jarvis speaks | Yes |
| Interrupt by just talking over it (0.4 s+) | No | Yes |
| Interrupt with F9 | Yes | Yes |
| Risk of Jarvis hearing itself | None (the first 0.3 s after it stops is ignored too) | High if you're really on speakers |

### The HUD (desktop window)

Voice mode opens the J.A.R.V.I.S. HUD: a full-screen-style heads-up display with
an animated arc-reactor core, live system diagnostics (CPU, memory, network,
storage, power, uptime), the conversation as it streams, an event log of what
Jarvis is doing, and a command line. The whole display changes colour with the
state: cyan on standby, aqua while listening (the spectrum ring follows your
voice), amber while processing, blue while responding, red when the mic is off
or something failed.

It is drawn by Microsoft Edge (built into Windows) in app mode, so it is a
plain window with no browser bars, and the animation runs on the graphics card.
If the PC can't keep up, the HUD lowers its own detail automatically. Chrome or
Brave are used if Edge is missing. The page is served only to this PC
(127.0.0.1) and every request needs a random per-run key.

- Type a command and press Enter (it interrupts a reply like F9 does), or use
  the quick buttons (status report, weather, briefing, news, tasks).
- The mic button or **Ctrl+M** turns the microphone off and on.

- **X (close)** hides the window; Jarvis keeps running in the tray. Click the
  tray icon (or "Open Jarvis" in its menu) to bring the window back.
- **Quit** in the tray menu exits Jarvis completely.
- Launching Jarvis again (e.g. the desktop shortcut) brings the running
  window forward instead of starting a second copy.
- Without a tray icon (`--no-tray`), closing the window quits.

Turn it off with `ui.window: false` or `--no-window`. The HUD's browser cache
lives in `%LOCALAPPDATA%\Jarvis\hud-browser`.

### Overlay

With `ui.overlay: true`, a small dark box in a corner of the screen shows the
state, what you said, and the reply as it streams. It fades out 4 seconds
after Jarvis goes idle. On Windows, clicks pass straight through it.

## Start with Windows

```powershell
python scripts\install_startup.py                 # start at login (Task Scheduler)
python scripts\install_startup.py --desktop       # ...and add a Jarvis shortcut to the desktop
python scripts\install_startup.py --desktop-only  # just the desktop shortcut
python scripts\install_startup.py --status
python scripts\uninstall_startup.py               # stop starting at login
```

Use the same Python you installed the requirements with (`.venv\Scripts\python`
if you made a virtual environment). This creates a hidden Task Scheduler task named
**"Jarvis Assistant"**. It runs `pythonw.exe main.py --wake --tray --minimized` 30 seconds
after you log in (so audio devices are ready): no console window, and the window
starts hidden in the tray. The desktop shortcut opens the window. If Task
Scheduler isn't available, a registry Run key (`HKCU\...\Run\JarvisAssistant`)
is used instead. `scripts\run_jarvis.bat` starts the same thing by hand.

Only one Jarvis runs at a time. Starting a second one brings the running
window forward (or, with no window, says "Jarvis is already running") and exits. On launch you hear a chime and "Good morning,
<name>. Jarvis online." (`startup.*`). Set `startup.auto_briefing: true` to
also get the daily briefing on the first launch of each day.

## Offline mode

If Claude can't be reached at startup or mid-conversation, Jarvis says so
once and keeps working for simple commands: the time and date, timers,
volume, mute, play/pause/next, opening apps, screenshots and locking the PC.
It retries every 30 seconds and tells you when it's back online.

## 40 example voice commands

**PC control**
1. "Open Chrome."
2. "Open Excel." (Start Menu names work)
3. "Close Notepad." *(asks to confirm)*
4. "Set the volume to 30."
5. "Mute." / "Unmute."
6. "Turn the brightness down to 40 percent."
7. "How's my computer doing?"
8. "Which apps are using the most memory?"
9. "Take a screenshot."
10. "Lock my PC."
11. "Restart the computer." *(confirm; 30 s grace)*
12. "Cancel the shutdown."

**Files** (inside your safe folders)
13. "Find the invoice from March."
14. "What's on my desktop?"
15. "Show the newest files in Downloads."
16. "Read me notes.txt from Documents."
17. "Open the latest PDF in my downloads."
18. "Take a note: call the plumber on Monday."
19. "Delete old-report.docx." *(confirm; Recycle Bin)*

**Web**
20. "What's the weather like?"
21. "Will it rain in Pune tomorrow?"
22. "What's in the news?"
23. "Any cricket news today?"
24. "Search the web for the latest Python release."
25. "Go to github.com."

**Projects**
26. "Open cs-events."
27. "What's the git status of cs-events?"
28. "Run npm test in cs-events." *(confirm)*
29. "Pull the latest changes for cs-events." *(confirm)*
30. "Start the cs-events dev server." / "Stop the dev server."

**Productivity**
31. "Set a timer for 10 minutes for the pasta."
32. "Remind me to call mom at 6 pm."
33. "What reminders do I have?"
34. "Add renew passport to my to-do list."
35. "What's on my to-do list?" / "Mark passport as done."
36. "Give me my daily briefing."

**Media, clipboard, memory**
37. "Pause the music." / "Next track."
38. "Play lofi beats on YouTube." / "Open Spotify and search Arijit Singh."
39. "Summarize what's on my clipboard."
40. "Remember that my locker code is 4512." Later: "What's my locker code?" / "Forget my locker code." *(confirm)*

## Tools

**Bold** = asks for confirmation. `python main.py --text` then `/tools` lists what's loaded.

| Module | Tools |
|---|---|
| `system_tools` | `open_app`, **`close_app`**, `set_volume`, `get_volume`, `mute`, `unmute`, `set_brightness`, `lock_pc`, `system_stats`, `screenshot`, **`shutdown_pc`**, **`restart_pc`**, `cancel_shutdown` |
| `file_tools` | `search_files`, `open_file`, `read_text_file`, `create_note`, `list_folder`, **`delete_file`** |
| `web_tools` | `web_search`, `open_url`, `get_weather`, `get_news` |
| `dev_tools` | `open_project`, **`run_command_in_project`**, `git_status`, **`git_pull`**, `start_dev_server`, `stop_dev_server` |
| `productivity_tools` | `set_timer`, `set_reminder`, `list_reminders`, `cancel_reminder`, `add_todo`, `read_todos`, `complete_todo`, `daily_briefing` |
| `media_tools` | `play_pause`, `next_track`, `previous_track`, `media_volume`, `search_youtube`, `open_spotify` |
| `clipboard_tools` | `read_clipboard`, `write_clipboard`, `summarize_clipboard` |
| `memory_tools` | `remember`, `recall`, **`forget`**, `list_memories`, `recent_commands` |

At most 6 tool calls run per request (`llm.max_tool_calls`). If a tool fails,
Claude gets a short error and explains it; it never claims success unless the
tool reported it.

## Add a tool in 5 lines

Create any file in `tools\`, for example `tools\my_tools.py`:

```python
from tools import tool

@tool
def flip_coin() -> str:
    """Flip a coin and say heads or tails."""
    import random; return random.choice(["Heads.", "Tails."])
```

Restart Jarvis and say "Flip a coin". That's all: the decorator builds the
schema from the type hints and docstring. See
[docs/ADDING_TOOLS.md](docs/ADDING_TOOLS.md) for arguments, settings,
confirmations and tests.

## Using Gemini for free

Google's Gemini API has a free tier, so Jarvis can run without paying for Claude.

1. Create a key at https://aistudio.google.com/apikey (it starts with `AIza`).
2. Run `python main.py --setup` and paste it, or set it up by hand:
   - add `GEMINI_API_KEY=AIza...` to `jarvis\.env`
   - set `llm.provider: gemini` in `config.yaml`
3. Run `python main.py --check`. The `Gemini API` row should say PASS.

Notes:
- `llm.gemini_model` defaults to `gemini-flash-latest`. If Google retires that
  name, use any current model from the AI Studio model list, e.g. `gemini-2.5-flash`.
- The free tier has per-minute and per-day request limits. When you hit one,
  Jarvis says "I've hit Gemini's free-tier limit" and works again shortly after.
- Google may use free-tier prompts to improve its products; don't use it for
  anything private.
- Tools, confirmations, memory and offline mode work the same with either provider.
  Switch back any time with `llm.provider: anthropic`.

## Configuration reference (`config.yaml`)

Secrets live in `.env` (`ANTHROPIC_API_KEY=...` or `GEMINI_API_KEY=...`); everything else is in
`config.yaml`. Every value is checked when Jarvis starts. A typo like
`silense_timeout` or a value out of range stops it with a clear message.

| Section | Settings |
|---|---|
| `assistant` | `name`, `user_name` (how Jarvis addresses you) |
| `llm` | `provider` (`anthropic` or `gemini`), `model` (`claude-sonnet-5-5`), `gemini_model` (`gemini-flash-latest`), `effort` (`low`…`max`), `max_tokens`, `max_history_turns` (10), `max_tool_rounds`, `max_tool_calls` (6), `request_timeout_s`, `max_retries`, `refusal_fallback` |
| `tools` | `timeout_s`, `disabled` (tool names), `disabled_modules`, `folders` (spoken folder names) |
| `apps` | Spoken name -> program for `open_app` |
| `safe_roots` | The only folders file tools may touch |
| `notes_dir`, `screenshots_dir` | Where notes and screenshots are saved |
| `projects` | `name: {path, dev_command}` |
| `weather` | `city`, `lat`, `lon`, `units` |
| `news_feeds` | RSS feed URLs |
| `briefing.include` | `weather`, `news`, `todos`, `reminders` |
| `safety` | `confirm_dangerous`, `confirm_timeout` (8 s), `shell_allowlist`, `auto_approve_allowlist` |
| `audio` | `input_device`, `output_device` (index or name part), `sample_rate`, `min_press_s`, `beeps`, `headphones`, `echo_tail` |
| `hotkey`, `hotkey_backend` | Push-to-talk key (`f9`) and library (`auto`/`keyboard`/`pynput`) |
| `wake` | `model` (`hey_jarvis` or a .onnx path), `threshold` (0.5), `cooldown`, `listen_timeout` |
| `vad` | `engine` (`silero`/`energy`), `threshold`, `silence_timeout` (1.0), `max_utterance`, `min_speech`, `preroll` |
| `follow_up` | `enabled`, `window` (6 s) |
| `barge_in.min_speech` | Talk this long over Jarvis to interrupt (headphones mode) |
| `stt` | `model` (`small.en`), `device`, `compute_type`, `beam_size`, `download_root`, `min_rms` |
| `tts` | `engine` (`edge`), `voice`, `rate`, `volume`, `filler`, `filler_delay`, `fillers` |
| `ui` | `window`, `tray`, `overlay`, `overlay_position`, `overlay_fade_s` |
| `startup` | `chime`, `greeting`, `time_greeting`, `auto_briefing` |
| `paths` | `logs_dir`, `data_dir` |
| `logging.level` | `DEBUG`/`INFO`/`WARNING`/`ERROR` |

## Files

| Path | Contents |
|---|---|
| `.env` | Your API key (never committed) |
| `config.yaml` | All settings |
| `logs\jarvis.log` | Main log, rotating (5 files × 2 MB) |
| `logs\tool_calls.log` | One JSON line per tool call: arguments, confirmation, result |
| `data\jarvis.db` | Memory, command history, to-dos, reminders (SQLite) |
| `data\jarvis.lock` | Single-instance lock |

## Troubleshooting

| Problem | Fix |
|---|---|
| Anything at all | `python main.py --check`, then look at `logs\jarvis.log` |
| "No microphone found" | Settings > Privacy & security > Microphone > allow desktop apps; plug the mic in before starting |
| Wrong mic or speakers | `--list-devices`, then `--setup` or set `audio.input_device` / `output_device` |
| F9 does nothing | The `keyboard` library needs no admin for normal windows, but an elevated window (admin terminal, Task Manager) swallows keys: run Jarvis as admin too, or set `hotkey_backend: pynput`. Or pick another `hotkey` |
| Wake word fires by itself | Raise `wake.threshold` to 0.6-0.7 (watch `[wake]` scores with `--debug`) |
| Wake word misses you | Lower `wake.threshold` to 0.35-0.4; raise mic level in Windows Sound settings (~75%) |
| Cuts you off mid-sentence | Raise `vad.silence_timeout` to 1.3-1.5 |
| Waits too long after you stop | Lower `vad.silence_timeout` to 0.7-0.8 |
| Jarvis hears itself (echo) | On speakers keep `audio.headphones: false` (listening pauses while it talks) and raise `audio.echo_tail` to 0.5; or use headphones and set it to `true` |
| Transcription slow | `stt.model: base.en`; with an NVIDIA GPU, `stt.device: cuda` |
| No voice, text only | edge-tts needs internet; `--check` shows the Text-to-speech line |
| "Jarvis is already running" | Quit it from the tray. If no tray icon is visible, end `pythonw.exe` in Task Manager |
| Dark red icon that won't clear | It crashed 5 times in 10 minutes: Open logs folder, fix the cause, Restart Jarvis |
| Doesn't start at login | `install_startup.py --status`; Task Scheduler > "Jarvis Assistant" > History |

## Security notes

- **The API key** stays in `.env` and is only sent to your chosen provider (Anthropic or Google).
- **What Claude sees:** your spoken requests, tool results (file contents you
  ask about, clipboard text you ask to summarise, search results) and the
  facts Jarvis selects from memory. Don't ask it to read secrets aloud.
- **Shell commands:** only allowlisted programs, one at a time, never
  chaining or redirection, and denylisted commands never run. `python` and
  `node` can run any script, so keep `auto_approve_allowlist: false`.
- **File tools** can't leave `safe_roots`, can't open executables or
  scripts, and delete only to the Recycle Bin.
- **Content isn't instructions:** text read from files, the clipboard or web
  pages is treated as data, but prompt injection is never impossible. The
  confirmation prompt is the backstop, so read it before saying yes.
- **Audit log:** `logs\tool_calls.log` records everything Jarvis did.
- **Memory** is stored unencrypted in `data\jarvis.db`. Don't store
  passwords you wouldn't write in a text file.

## Development

```powershell
python -m pytest              # ~290 tests, no microphone or API key needed
python main.py --text         # exercise the agent and tools by typing
```

The architecture, conventions and the "how to add things" rules are in
[CLAUDE.md](../CLAUDE.md). Building a standalone .exe:
[build_exe.md](build_exe.md).

## Roadmap

- A custom-trained **"Jarvis"** wake word (no "hey").
- **Gmail and Google Calendar** tools via MCP ("what's my next meeting?").
- **Screen understanding**: "what's this error on my screen?" via a screenshot and vision.
- A **local LLM fallback** (e.g. Ollama) for smarter offline mode.
- A **mobile remote**: talk to your PC Jarvis from your phone.
- Piper (offline) and ElevenLabs voices (the stubs are already in `core/tts.py`).
