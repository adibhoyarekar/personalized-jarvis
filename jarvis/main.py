"""Jarvis entry point.

Usage:
    python main.py                   # same as --wake
    python main.py --wake            # say "Hey Jarvis" hands-free (F9 also works)
    python main.py --push-to-talk    # hold the hotkey (F9), speak, release
    python main.py --text            # type to Jarvis, read replies
    python main.py --text --speak    # type, and hear the replies
    python main.py --list-devices    # show audio devices and their indexes
    python main.py --debug           # any mode: show state changes and wake word scores
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from core.agent import Agent, AgentError
from core.config import Config, ConfigError, load_config
from core.logging_setup import setup_logging
from core.memory import Memory
from core.safety import SafetyGate, typed_confirm_factory
from core.scheduler import ReminderScheduler
from core.tts import SpeechStream, TTSEngine
from tools import ToolContext, ToolRegistry, set_context

log = logging.getLogger("jarvis")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="jarvis", description="Jarvis personal voice assistant")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--text", action="store_true", help="text-only chat in the terminal")
    mode.add_argument("--push-to-talk", action="store_true", help="voice mode, hold a hotkey to talk")
    mode.add_argument("--wake", action="store_true", help="voice mode, wake word activation")
    mode.add_argument("--list-devices", action="store_true", help="list audio input/output devices")
    parser.add_argument("--speak", action="store_true", help="with --text: also speak replies")
    parser.add_argument("--config", type=Path, default=None, help="path to config.yaml")
    parser.add_argument(
        "--debug", action="store_true", help="print state changes and wake word scores; verbose log file"
    )
    return parser.parse_args(argv)


def _fix_console_encoding() -> None:
    """Windows consoles default to a legacy code page; make printing unicode safe."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass


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
    """Wire up memory, tools, the safety gate, the agent and the reminder scheduler."""
    if confirm is None:
        confirm = typed_confirm_factory(float(config.get("safety.confirm_timeout", 8)))
    memory = Memory.from_config(config)
    context = ToolContext(config=config, notify=lambda msg: print(f"\n[{config.get('assistant.name', 'Jarvis')}] {msg}"))
    context.memory = memory
    set_context(context)
    registry = ToolRegistry.from_config(config)
    gate = SafetyGate.from_config(config, confirm)
    agent = Agent(
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


HELP = """Commands: /tools  list tools   /reset  forget conversation   /quit  exit"""


def run_text_mode(agent: Agent, tts: TTSEngine | None = None) -> None:
    """Interactive terminal chat: the debugging front-end for the agent."""
    name = agent.name
    print(f"{name} text mode{' (speaking)' if tts else ''}. {HELP}\n")
    while True:
        try:
            user_text = input("You > ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if tts is not None:
            tts.stop()  # typing something new interrupts the previous reply
        if not user_text:
            continue
        cmd = user_text.lower()
        if cmd in ("/quit", "/exit", "quit", "exit"):
            break
        if cmd == "/reset":
            agent.reset()
            print("(conversation cleared)\n")
            continue
        if cmd == "/tools":
            for spec in sorted(agent.registry.specs.values(), key=lambda s: s.name):
                flag = " [asks first]" if spec.dangerous or spec.shell_arg else ""
                print(f"  {spec.name}{flag}: {spec.description.splitlines()[0]}")
            print()
            continue
        if cmd in ("/help", "?"):
            print(HELP + "\n")
            continue

        print(f"{name} > ", end="", flush=True)

        def show_status(state: str, detail: str) -> None:
            if state == "tool":
                print(f"[{detail}] ", end="", flush=True)

        stream = SpeechStream(tts)

        def on_text(chunk: str) -> None:
            print(chunk, end="", flush=True)
            stream.feed(chunk)

        try:
            agent.ask(user_text, on_text=on_text, on_status=show_status)
            stream.finish()
        except KeyboardInterrupt:
            agent.cancel()
            if tts is not None:
                tts.stop()
            print(" (interrupted)")
        print("\n")
    print("Goodbye.")


def make_tts(config: Config, on_error=None) -> TTSEngine | None:  # type: ignore[no-untyped-def]
    """Create the configured TTS engine, or None (print-only) if it can't start."""
    from core.audio_in import AudioError, resolve_device
    from core.tts import TTSError, create_tts

    try:
        device = resolve_device(config.get("audio.output_device"), "output")
        return create_tts(config, output_device=device, on_error=on_error)
    except (AudioError, TTSError, NotImplementedError) as exc:
        print(f"Speech output disabled: {exc}\nReplies will be printed only.\n")
        log.warning("TTS unavailable: %s", exc)
        return None


def _build_pipeline(config: Config, recorder, wake_wanted: bool, debug: bool):  # type: ignore[no-untyped-def]
    """Create the wake word + VAD pipeline, or None to run push-to-talk only."""
    from core.audio_in import (
        AudioPipeline,
        BargeInDetector,
        EndpointConfig,
        WakeWordDetector,
        WakeWordError,
        create_vad,
    )
    from core.voice import WakeScoreMonitor

    if not wake_wanted:
        return None
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


def run_voice_mode(config: Config, wake: bool = True, debug: bool = False) -> int:
    """Wake word (with push-to-talk alongside) or push-to-talk-only voice loop."""
    from core.audio_in import AudioError, HotkeyError, MicRecorder, resolve_device
    from core.sounds import Sounds
    from core.state import StateManager
    from core.stt import STTError, create_stt
    from core.voice import VoiceAssistant, VoiceSettings, debug_state_printer

    try:
        runtime = build_runtime(config)  # confirm and notify are re-pointed below
    except AgentError as exc:
        print(f"Startup error: {exc}", file=sys.stderr)
        return 2
    agent = runtime.agent

    try:
        recorder = MicRecorder(config.get("audio.input_device"), int(config.get("audio.sample_rate", 16000)))
        recorder.open()
    except AudioError as exc:
        print(f"Microphone error: {exc}", file=sys.stderr)
        return 2

    pipeline = None
    assistant: VoiceAssistant | None = None
    try:
        pipeline = _build_pipeline(config, recorder, wake, debug)

        model = config.get("stt.model", "small.en")
        print(f"Loading speech recognition ({model}); the first run downloads the model...")
        try:
            stt = create_stt(config)
        except STTError as exc:
            print(f"Speech recognition error: {exc}", file=sys.stderr)
            return 2
        print(f"Speech recognition ready on {stt.device.upper()}.")

        try:
            out_device = resolve_device(config.get("audio.output_device"), "output")
        except AudioError:
            out_device = None
        sounds = Sounds(out_device, enabled=bool(config.get("audio.beeps", True)))

        def tts_error(text: str, exc: Exception) -> None:
            if assistant is not None:
                assistant.on_tts_error(text, exc)

        tts = make_tts(config, on_error=tts_error)
        settings = VoiceSettings(
            hotkey=str(config.get("hotkey", "f9")) or None,
            hotkey_backend=str(config.get("hotkey_backend", "auto")),
            min_press_s=float(config.get("audio.min_press_s", 0.3)),
            follow_up=bool(config.get("follow_up.enabled", True)),
            follow_up_window=float(config.get("follow_up.window", 6)),
            wake_listen_timeout=float(config.get("wake.listen_timeout", 5.0)),
            headphones=bool(config.get("audio.headphones", False)),
            echo_tail_s=float(config.get("audio.echo_tail", 0.3)),
            filler=bool(config.get("tts.filler", True)),
            filler_delay=float(config.get("tts.filler_delay", 2.5)),
            confirm_timeout=float(config.get("safety.confirm_timeout", 8)),
        )
        fillers = config.get("tts.fillers")
        if fillers:
            settings.fillers = list(fillers)
        state = StateManager()
        if debug:
            state.subscribe(debug_state_printer())
        assistant = VoiceAssistant(
            agent, recorder, stt, tts, settings=settings, pipeline=pipeline, state=state, sounds=sounds
        )
        agent.gate.confirm = assistant.confirm
        runtime.set_notify(assistant.announce)
        assistant.run()
    except HotkeyError as exc:
        print(f"Hotkey error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nGoodbye.")
    finally:
        if assistant is not None:
            assistant.close()
        else:
            if pipeline is not None:
                pipeline.stop()
            recorder.close()
        runtime.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    _fix_console_encoding()
    args = parse_args(argv)

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 2

    level = "DEBUG" if args.debug else str(config.get("logging.level", "INFO"))
    log_file = setup_logging(config.path("paths.logs_dir", "logs"), level, console=args.debug)
    log.info("Starting Jarvis (args=%s)", vars(args))

    if args.list_devices:
        from core.audio_in import AudioError, list_devices

        try:
            print(list_devices())
        except AudioError as exc:
            print(f"Audio error: {exc}", file=sys.stderr)
            return 2
        return 0

    try:
        if not args.text:
            return run_voice_mode(config, wake=not args.push_to_talk, debug=args.debug)

        try:
            runtime = build_runtime(config)
        except AgentError as exc:
            print(f"Startup error: {exc}", file=sys.stderr)
            return 2
        tts = make_tts(config) if args.speak else None
        if tts is not None:
            name = runtime.agent.name

            def notify(message: str) -> None:
                print(f"\n[{name}] {message}")
                tts.speak(message)

            runtime.set_notify(notify)
        try:
            run_text_mode(runtime.agent, tts)
        finally:
            if tts is not None:
                tts.shutdown()
            runtime.close()
        return 0
    finally:
        log.info("Jarvis stopped. Log: %s", log_file)


if __name__ == "__main__":
    sys.exit(main())
