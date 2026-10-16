"""Application wiring: the runtime shared by all modes, and the voice app with tray,
overlay, crash recovery, degraded (offline) mode and the startup greeting."""

from __future__ import annotations

import datetime as dt
import logging
import os
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from core.agent import Agent, create_agent
from core.config import PROJECT_DIR, Config
from core.connectivity import ConnectivityMonitor
from core.memory import Memory
from core.notifications import show_toast
from core.safety import SafetyGate, typed_confirm_factory
from core.scheduler import ReminderScheduler
from core.state import StateManager
from core.tts import TTSEngine
from tools import ToolContext, ToolRegistry, set_context

log = logging.getLogger(__name__)

RESTART_ENV = "JARVIS_WAIT_FOR_PID"


# ---------------------------------------------------------------------------
# Runtime: agent + tools + memory + reminders (shared by text and voice modes)
# ---------------------------------------------------------------------------


@dataclass
class Runtime:
    """Everything that must be shut down cleanly on exit."""

    agent: Agent
    memory: Memory
    scheduler: ReminderScheduler
    context: ToolContext

    def set_notify(self, notify: Callable[[str], None]) -> None:
        """Where timers/reminders and other asynchronous messages go."""
        self.context.notify = notify

    def close(self) -> None:
        self.scheduler.stop()
        self.agent.registry.shutdown()
        self.agent.gate.close()
        self.memory.close()


def build_runtime(config: Config, confirm: Callable[[str], bool] | None = None) -> Runtime:
    """Wire up memory, tools, the safety gate, the agent and the reminder scheduler.

    Raises:
        AgentError: No API key.
    """
    if confirm is None:
        confirm = typed_confirm_factory(float(config.get("safety.confirm_timeout", 8)))
    memory = Memory.from_config(config)
    name = config.get("assistant.name", "Jarvis")
    context = ToolContext(config=config, notify=lambda msg: print(f"\n[{name}] {msg}"))
    context.memory = memory
    set_context(context)
    registry = ToolRegistry.from_config(config)
    gate = SafetyGate.from_config(config, confirm)
    agent = create_agent(
        config,
        registry,
        gate,
        facts_provider=memory.prompt_context,
        on_turn=lambda text, reply, calls: memory.add_command(text, reply, calls),
    )
    context.complete = agent.complete
    log.info("Loaded %d tools: %s", len(registry.specs), ", ".join(sorted(registry.specs)))
    # Late-bound so voice mode can re-point notify after startup.
    scheduler = ReminderScheduler(memory, announce=lambda msg: context.notify(msg))
    scheduler.start()
    return Runtime(agent, memory, scheduler, context)


def make_tts(config: Config, on_error: Any = None, quiet: bool = False) -> TTSEngine | None:
    """Create the configured TTS engine, or None (print-only) if it can't start."""
    from core.audio_in import AudioError, resolve_device
    from core.tts import TTSError, create_tts

    try:
        device = resolve_device(config.get("audio.output_device"), "output")
        return create_tts(config, output_device=device, on_error=on_error)
    except (AudioError, TTSError, NotImplementedError) as exc:
        if not quiet:
            print(f"Speech output disabled: {exc}\nReplies will be printed only.\n")
        log.warning("TTS unavailable: %s", exc)
        return None


def build_pipeline(config: Config, recorder: Any, debug: bool = False) -> Any:
    """Wake word + VAD pipeline, or None (push-to-talk only) if the wake model can't load."""
    from core.audio_in import (
        AudioPipeline,
        BargeInDetector,
        EndpointConfig,
        WakeWordDetector,
        WakeWordError,
        create_vad,
    )
    from core.voice import WakeScoreMonitor

    threshold = float(config.get("wake.threshold", 0.5))
    model = str(config.get("wake.model", "hey_jarvis"))
    try:
        wake = WakeWordDetector(
            model=model,
            threshold=threshold,
            cooldown_s=float(config.get("wake.cooldown", 1.5)),
            on_score=WakeScoreMonitor(threshold) if debug else None,
        )
    except WakeWordError as exc:
        print(f"WARNING: wake word unavailable ({exc}).\nFalling back to push-to-talk.\n")
        log.warning("Wake word unavailable: %s", exc)
        show_toast("Jarvis", "Wake word unavailable - using push-to-talk (F9).", sound=False)
        return None

    vad = create_vad(str(config.get("vad.engine", "silero")))
    print(f"Wake word '{model}' ready (threshold {threshold}); voice detection: {vad.name}.")
    headphones = bool(config.get("audio.headphones", False))
    endpoint = EndpointConfig(
        silence_timeout=float(config.get("vad.silence_timeout", 1.0)),
        max_utterance=float(config.get("vad.max_utterance", 15)),
        min_speech=float(config.get("vad.min_speech", 0.3)),
        preroll=float(config.get("vad.preroll", 0.5)),
        start_timeout=float(config.get("wake.listen_timeout", 5.0)),
    )
    return AudioPipeline(
        recorder,
        vad,
        endpoint,
        wake=wake,
        vad_threshold=float(config.get("vad.threshold", 0.5)),
        # Voice barge-in only with headphones; on speakers Jarvis would hear itself.
        barge_in=BargeInDetector(float(config.get("barge_in.min_speech", 0.4))) if headphones else None,
        wake_listen_timeout=float(config.get("wake.listen_timeout", 5.0)),
    )


def greeting_text(config: Config, now: dt.datetime | None = None) -> str:
    """The spoken startup line, e.g. "Good morning, Aditya. Jarvis online." ("" = silent)."""
    greeting = str(config.get("startup.greeting", "Jarvis online.") or "").strip()
    if not config.get("startup.time_greeting", True):
        return greeting
    hour = (now or dt.datetime.now()).hour
    part = "morning" if 5 <= hour < 12 else "afternoon" if 12 <= hour < 17 else "evening"
    user = str(config.get("assistant.user_name", "") or "").strip()
    salute = f"Good {part}, {user}." if user else f"Good {part}."
    return f"{salute} {greeting}".strip()


def relaunch(argv: list[str]) -> None:
    """Start a fresh Jarvis process that waits for this one to exit (used by "Restart")."""
    env = dict(os.environ, **{RESTART_ENV: str(os.getpid())})
    args = [sys.executable, str(PROJECT_DIR / "main.py"), *argv]
    flags = 0
    if sys.platform == "win32":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
    log.info("Relaunching: %s", args)
    subprocess.Popen(args, cwd=PROJECT_DIR, env=env, creationflags=flags, close_fds=True)  # noqa: S603


def open_path(path: Path) -> None:
    from tools._platform import open_with_default_handler

    try:
        open_with_default_handler(str(path))
    except Exception:  # noqa: BLE001
        log.exception("Could not open %s", path)


# ---------------------------------------------------------------------------
# The voice app
# ---------------------------------------------------------------------------


class VoiceApp:
    """Wake word / push-to-talk Jarvis with tray, overlay, supervisor and degraded mode.

    Implements :class:`ui.tray.TrayActions`, so the tray menu calls these methods.

    Args:
        config: Loaded config.
        wake: Start in wake word mode (False = push-to-talk only).
        debug: Print state changes and wake word scores.
        tray: Show the system tray icon.
        overlay: Show the on-screen overlay (None = use ``ui.overlay`` from config).
        window: Show the HUD window (None = use ``ui.window`` from config).
        start_hidden: Start with the window hidden in the tray (ignored without a tray).
        argv: Command line to reuse when restarting.
        print_fn: Console output (pythonw has no console; print then does nothing).
    """

    def __init__(
        self,
        config: Config,
        wake: bool = True,
        debug: bool = False,
        tray: bool = False,
        overlay: bool | None = None,
        window: bool | None = None,
        start_hidden: bool = False,
        argv: list[str] | None = None,
        print_fn: Callable[..., None] = print,
    ) -> None:
        self.config = config
        self.wake_mode = wake
        self.debug = debug
        self.use_tray = tray
        self.use_overlay = bool(config.get("ui.overlay", False)) if overlay is None else overlay
        self.use_window = bool(config.get("ui.window", True)) if window is None else window
        self.start_hidden = start_hidden
        self.argv = argv if argv is not None else sys.argv[1:]
        self.print = print_fn
        self.state = StateManager()
        self.runtime: Runtime | None = None
        self.assistant: Any = None
        self.tray: Any = None
        self.overlay: Any = None
        self.window: Any = None
        self.show_requests: Any = None
        self._hidden_notice_given = False
        self._mic_rms = 0.0
        self.monitor: ConnectivityMonitor | None = None
        self.muted = False
        self._quit = threading.Event()
        self._restart_process = False
        self._reload_session = False
        self._audio_changed = False
        self._settings_lock = threading.Lock()
        self._first_session = True
        self._offline_notice_pending = False
        self.recorder: Any = None
        self.stt: Any = None
        self.tts: TTSEngine | None = None
        self.sounds: Any = None

    # -- TrayActions ----------------------------------------------------------------

    def toggle_mute(self) -> None:
        self.muted = not self.muted
        if self.assistant is not None:
            self.assistant.set_muted(self.muted)
        if self.tray is not None:
            self.tray.refresh()

    def is_muted(self) -> bool:
        return self.muted

    def current_mode(self) -> str:
        if self.assistant is not None:
            return "wake" if self.assistant.wake_enabled else "push-to-talk"
        return "wake" if self.wake_mode else "push-to-talk"

    def set_mode(self, mode: str) -> None:
        want_wake = mode == "wake"
        self.wake_mode = want_wake
        assistant = self.assistant
        if assistant is None:
            return
        if assistant.set_wake_active(want_wake):
            return
        # Push-to-talk session without a wake model loaded: rebuild the session.
        self._reload_session = True
        assistant.stop()

    def open_logs(self) -> None:
        open_path(self.config.path("paths.logs_dir", "logs"))

    def open_config(self) -> None:
        open_path(PROJECT_DIR / "config.yaml")

    def open_readme(self) -> None:
        open_path(PROJECT_DIR / "README.md")

    def restart(self) -> None:
        self._restart_process = True
        self.argv = [a for a in self.argv if a != "--minimized"]  # come back on screen
        self.quit()

    def quit(self) -> None:
        self._quit.set()
        if self.assistant is not None:
            self.assistant.stop()

    def has_window(self) -> bool:
        return self.use_window

    def show_window(self) -> None:
        if self.window is not None:
            self.window.show()

    # -- audio devices (called from HUD server threads) -------------------------------

    def audio_devices(self) -> dict[str, Any]:
        """Selectable microphones and speakers, plus which ones are in use."""
        from core.audio_in import AudioError, default_device, device_options, resolve_device

        result: dict[str, Any] = {}
        for kind in ("input", "output"):
            try:
                options = device_options(kind)
            except AudioError as exc:
                result[kind] = {"options": [], "selected": None, "default": None, "error": str(exc)}
                continue
            spec = self.config.get(f"audio.{kind}_device")
            try:
                selected = None if spec is None else resolve_device(spec, kind)
            except AudioError:
                selected = None
            result[kind] = {"options": options, "selected": selected, "default": default_device(kind)}
        return result

    def set_audio_devices(self, input_device: int | None, output_device: int | None) -> None:
        """Save the chosen devices to config.yaml and switch to them (None = Windows default).

        Raises:
            AudioError: If a device can't be opened; nothing is saved then.
            OSError: If config.yaml can't be written.
        """
        from core.audio_in import check_device

        check_device(input_device, "input")
        check_device(output_device, "output")
        self._save_settings({"audio.input_device": input_device, "audio.output_device": output_device})
        log.info("Audio devices set: input=%s output=%s", input_device, output_device)
        self._audio_changed = True
        assistant = self.assistant
        if assistant is not None:
            self._reload_session = True
            assistant.stop()

    def test_speaker(self, device: int | None) -> None:
        from core.audio_in import play_test_tone

        play_test_tone(device)

    def test_microphone(self, device: int | None) -> float:
        from core.audio_in import probe_input

        return probe_input(device)

    def _save_settings(self, values: dict[str, Any]) -> None:
        from core.config_editor import set_value

        path = self.config.source or PROJECT_DIR / "config.yaml"
        with self._settings_lock:
            text = path.read_text(encoding="utf-8")
            for key, value in values.items():
                text = set_value(text, key, value)
            path.write_text(text, encoding="utf-8")
            for key, value in values.items():
                self.config.set(key, value)

    def _switch_audio(self) -> None:
        """Reopen the mic and speakers on the configured devices (voice thread, between sessions)."""
        from core.audio_in import AudioError, MicRecorder

        self._audio_changed = False
        problems = []
        if self.recorder is not None:
            try:
                recorder = MicRecorder(
                    self.config.get("audio.input_device"), int(self.config.get("audio.sample_rate", 16000))
                )
                recorder.open()
            except AudioError as exc:
                log.warning("Keeping the old microphone: %s", exc)
                problems.append(f"Couldn't open that microphone, so I kept the old one. {exc}")
            else:
                self.recorder.close()
                if self.window is not None:
                    recorder.add_listener(self._on_mic_frame)
                self.recorder = recorder
        if self.tts is not None:
            self.tts.shutdown()
        self.tts = make_tts(self.config, on_error=self._tts_error, quiet=True)
        if self.tts is None:
            problems.append("Speech output couldn't start on that speaker.")
        if self.sounds is not None:
            self.sounds.output_device = self._output_device()
        if self.window is not None:
            for text in problems:
                self.window.on_notice(text)
            if not problems:
                self.window.log("AUDIO", "Audio routing updated.")

    # -- WindowActions ------------------------------------------------------------------

    def submit_text(self, text: str) -> None:
        """A message typed in the window."""
        if self.assistant is not None:
            self.assistant.submit_text(text)
        elif self.window is not None:
            self.window.on_transcript(text)
            self.window.on_notice("I'm still starting up. Give me a few seconds and try again.")

    def window_closed(self) -> None:
        """The window's X: hide to the tray if there is one, otherwise quit."""
        if self.tray is None:
            self.quit()
            return
        if self.window is not None:
            self.window.hide()
        if not self._hidden_notice_given:
            self._hidden_notice_given = True
            name = self.config.get("assistant.name", "Jarvis")
            self.tray.notify(f"{name} is still running here. Right-click the icon and choose Quit to exit.")

    def _set_status_note(self, note: str) -> None:
        for ui in (self.tray, self.window):
            if ui is not None:
                ui.set_status_note(note)

    def _set_error(self, error: bool) -> None:
        for ui in (self.tray, self.window):
            if ui is not None:
                ui.set_error(error)

    def _on_mic_frame(self, frame: Any) -> None:
        """Mic callback thread: keep the latest loudness for the window's animation."""
        self._mic_rms = float((frame * frame).mean()) ** 0.5 if frame.size else 0.0

    # -- notifications ----------------------------------------------------------------

    def notify(self, message: str, speak: bool = True) -> None:
        """Toast + console, and speech when a voice session is running."""
        show_toast("Jarvis", message, sound=False)
        if speak and self.assistant is not None:
            self.assistant.announce(message)  # prints too, and reaches the window
        else:
            self.print(f"[{self.config.get('assistant.name', 'Jarvis')}] {message}")
            if self.window is not None:
                self.window.on_notice(message)

    def _on_offline(self) -> None:
        if self.runtime is not None:
            self.runtime.agent.online = False
        self._set_status_note("offline")
        message = ("I can't reach the internet, so I'm in offline mode. Timers, volume, music and apps "
                   "still work, and I'll keep trying to reconnect.")
        if self.assistant is None:
            self._offline_notice_pending = True  # said once listening has started
            show_toast("Jarvis", "Offline mode - local commands only. Retrying in the background.", sound=False)
        else:
            self.notify(message)

    def _on_online(self) -> None:
        if self.runtime is not None:
            self.runtime.agent.online = True
        self._set_status_note("")
        if self.assistant is not None:
            self.notify("I'm back online.")

    # -- run ------------------------------------------------------------------------

    def run(self) -> int:
        """Start everything, run until Quit, then release everything. Returns an exit code."""
        from core.agent import AgentError
        from core.audio_in import AudioError, MicRecorder
        from core.sounds import Sounds
        from core.stt import STTError, create_stt
        from core.supervisor import Supervisor

        if self.debug:
            from core.voice import debug_state_printer

            self.state.subscribe(debug_state_printer(self.print))
        try:
            if self.use_tray:
                self._start_tray()
            if self.use_window:
                self._start_window()
            try:
                self.runtime = build_runtime(self.config)  # confirm/notify are re-pointed per session
            except AgentError as exc:
                return self._fatal(f"Startup error: {exc}")
            agent = self.runtime.agent

            self.monitor = ConnectivityMonitor(agent.ping, on_offline=self._on_offline, on_online=self._on_online)
            agent.on_connection_lost = self.monitor.mark_offline
            agent.on_connection_restored = self.monitor.mark_online
            if not self.monitor.check_now():
                self.print("Offline: the AI service is unreachable. Local commands still work; retrying every 30 s.")
            self.monitor.start()

            try:
                self.recorder = MicRecorder(
                    self.config.get("audio.input_device"), int(self.config.get("audio.sample_rate", 16000))
                )
                self.recorder.open()
            except AudioError as exc:
                return self._fatal(f"Microphone error: {exc}")
            if self.window is not None:
                self.recorder.add_listener(self._on_mic_frame)

            model = self.config.get("stt.model", "small.en")
            self.print(f"Loading speech recognition ({model}); the first run downloads the model...")
            self._set_status_note("loading")
            try:
                self.stt = create_stt(self.config)
            except STTError as exc:
                return self._fatal(f"Speech recognition error: {exc}")
            self.print(f"Speech recognition ready on {self.stt.device.upper()}.")

            self.sounds = Sounds(self._output_device(), enabled=bool(self.config.get("audio.beeps", True)))
            self.tts = make_tts(self.config, on_error=self._tts_error)
            if self.use_overlay:
                self._start_overlay()
            self._set_status_note("" if agent.online else "offline")

            supervisor = Supervisor(
                self._session_loop,
                on_crash=self._on_crash,
                on_give_up=self._on_give_up,
                should_stop=self._quit.is_set,
            )
            supervisor.run()
        except KeyboardInterrupt:
            self.print("\nGoodbye.")
        finally:
            self._shutdown()
        if self._restart_process:
            relaunch(self.argv)
        return 0

    def _session_loop(self) -> None:
        """One or more voice sessions (a mode switch may rebuild the session)."""
        while not self._quit.is_set():
            self._reload_session = False
            self._run_session()
            if not self._reload_session:
                return

    def _run_session(self) -> None:
        from core.voice import VoiceAssistant, VoiceSettings

        assert self.runtime is not None
        if self._audio_changed:
            self._switch_audio()
        cfg = self.config
        pipeline = build_pipeline(cfg, self.recorder, self.debug) if self.wake_mode else None
        settings = VoiceSettings(
            hotkey=str(cfg.get("hotkey", "f9")) or None,
            hotkey_backend=str(cfg.get("hotkey_backend", "auto")),
            min_press_s=float(cfg.get("audio.min_press_s", 0.3)),
            follow_up=bool(cfg.get("follow_up.enabled", True)),
            follow_up_window=float(cfg.get("follow_up.window", 6)),
            wake_listen_timeout=float(cfg.get("wake.listen_timeout", 5.0)),
            headphones=bool(cfg.get("audio.headphones", False)),
            echo_tail_s=float(cfg.get("audio.echo_tail", 0.3)),
            filler=bool(cfg.get("tts.filler", True)),
            filler_delay=float(cfg.get("tts.filler_delay", 2.5)),
            confirm_timeout=float(cfg.get("safety.confirm_timeout", 8)),
        )
        if cfg.get("tts.fillers"):
            settings.fillers = list(cfg.get("tts.fillers"))
        assistant = VoiceAssistant(
            self.runtime.agent, self.recorder, self.stt, self.tts,
            settings=settings, pipeline=pipeline, state=self.state, sounds=self.sounds, print_fn=self.print,
        )
        if self.tray is not None:
            assistant.transcript_listeners.append(self.tray.set_last_command)
        if self.overlay is not None:
            assistant.transcript_listeners.append(self.overlay.on_transcript)
            assistant.reply_listeners.append(self.overlay.on_reply)
        if self.window is not None:
            assistant.transcript_listeners.append(self.window.on_transcript)
            assistant.reply_listeners.append(self.window.on_reply)
            assistant.reply_done_listeners.append(self.window.on_reply_done)
            assistant.notice_listeners.append(self.window.on_notice)
            assistant.status_listeners.append(self.window.on_status)
        if self.muted:
            assistant.set_muted(True)
        self.runtime.agent.gate.confirm = assistant.confirm
        self.runtime.set_notify(assistant.announce)
        self.assistant = assistant
        self._set_error(False)
        on_ready = self._on_ready if self._first_session else None
        self._first_session = False
        try:
            assistant.run(on_ready=on_ready)
        finally:
            self.assistant = None

    def _on_ready(self) -> None:
        """First session only: chime, greeting, offline notice, optional daily briefing."""
        assistant = self.assistant
        if assistant is None:
            return
        if self.config.get("startup.chime", True):
            self.sounds.chime()
        greeting = greeting_text(self.config)
        if greeting and self.tts is not None:
            self.tts.speak(greeting)
            self.print(f"{self.runtime.agent.name if self.runtime else 'Jarvis'}: {greeting}")
        if greeting and self.window is not None:
            self.window.on_notice(greeting)
        if self._offline_notice_pending:
            self._offline_notice_pending = False
            self.notify("I'm offline right now, so only simple local commands work. I'll keep trying.")
        if self.config.get("startup.auto_briefing", False) and self.runtime and self.runtime.agent.online:
            today = dt.date.today().isoformat()
            if self.runtime.memory.get_state("last_auto_briefing") != today:
                self.runtime.memory.set_state("last_auto_briefing", today)
                if self.tts is not None:
                    self.tts.wait(timeout=10)
                assistant.respond("Give me my daily briefing.")

    # -- helpers ----------------------------------------------------------------------

    def _output_device(self) -> Any:
        from core.audio_in import AudioError, resolve_device

        try:
            return resolve_device(self.config.get("audio.output_device"), "output")
        except AudioError:
            return None

    def _tts_error(self, text: str, exc: Exception) -> None:
        if self.assistant is not None:
            self.assistant.on_tts_error(text, exc)

    def _start_tray(self) -> None:
        try:
            from ui.tray import TrayIcon

            self.tray = TrayIcon(self.state, self, name=str(self.config.get("assistant.name", "Jarvis")))
            self.tray.start()
            self.tray.set_status_note("starting")
        except Exception as exc:  # noqa: BLE001 - Jarvis works without a tray
            log.warning("Tray icon unavailable: %s", exc)
            self.print(f"(tray icon unavailable: {exc})")
            self.tray = None

    def _start_window(self) -> None:
        try:
            from core.single_instance import ShowRequests
            from ui.hud import HudWindow

            self.window = HudWindow(
                self,
                name=str(self.config.get("assistant.name", "Jarvis")),
                user_name=str(self.config.get("assistant.user_name", "") or ""),
                hotkey=str(self.config.get("hotkey", "f9") or ""),
                start_hidden=self.start_hidden and self.tray is not None,
                level=lambda: self._mic_rms,
            )
            self.state.subscribe(self.window.on_state)
            self.window.start()
            # A second launch (desktop shortcut) brings this window forward instead.
            self.show_requests = ShowRequests(self.window.show)
            self.show_requests.start()
        except Exception as exc:  # noqa: BLE001 - Jarvis works without its window
            log.warning("Window unavailable: %s", exc)
            self.window = None

    def _start_overlay(self) -> None:
        try:
            from ui.overlay import Overlay

            self.overlay = Overlay(
                corner=str(self.config.get("ui.overlay_position", "bottom-right")),
                fade_s=float(self.config.get("ui.overlay_fade_s", 4.0)),
            )
            self.state.subscribe(self.overlay.on_state)
            self.overlay.start()
        except Exception as exc:  # noqa: BLE001
            log.warning("Overlay unavailable: %s", exc)
            self.overlay = None

    def _on_crash(self, exc: BaseException) -> None:
        self._set_error(True)
        self.print(f"\n[error] {type(exc).__name__}: {exc} - restarting in 3 seconds (details in logs/jarvis.log)")

    def _on_give_up(self, exc: BaseException) -> None:
        self._set_error(True)
        message = "Jarvis stopped after crashing repeatedly. Check logs/jarvis.log, then restart from the tray."
        self.print(message)
        show_toast("Jarvis stopped", message)
        if self.tray is not None:
            self._quit.wait()  # keep the red tray icon so the user can open logs or restart

    def _fatal(self, message: str) -> int:
        """Startup failure: console, log and a toast (pythonw has no console)."""
        log.error(message)
        if sys.stderr is not None:
            print(message, file=sys.stderr)
        show_toast("Jarvis couldn't start", message)
        if self.tray is not None:
            self.tray.set_error(True)
            self.tray.notify(message)
        return 2

    def _shutdown(self) -> None:
        for name, stop in (
            ("connectivity", lambda: self.monitor and self.monitor.stop()),
            ("show requests", lambda: self.show_requests and self.show_requests.stop()),
            ("window", lambda: self.window and self.window.stop()),
            ("overlay", lambda: self.overlay and self.overlay.stop()),
            ("tts", lambda: self.tts and self.tts.shutdown()),
            ("microphone", lambda: self.recorder and self.recorder.close()),
            ("runtime", lambda: self.runtime and self.runtime.close()),
            ("tray", lambda: self.tray and self.tray.stop()),
        ):
            try:
                stop()
            except Exception:  # noqa: BLE001 - keep releasing the rest
                log.exception("Error stopping %s", name)
        log.info("Voice app stopped")
