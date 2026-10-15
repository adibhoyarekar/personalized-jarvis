"""Jarvis entry point.

Usage:
    python main.py                   # same as --push-to-talk
    python main.py --push-to-talk    # hold the hotkey (F9), speak, release
    python main.py --text            # type to Jarvis, read replies (debug mode)
    python main.py --text --speak    # type, and hear the replies
    python main.py --wake            # say "Jarvis" hands-free        (Phase 3)
    python main.py --list-devices    # show audio devices and their indexes
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from core.agent import Agent, AgentError
from core.config import Config, ConfigError, load_config
from core.logging_setup import setup_logging
from core.safety import SafetyGate, parse_yes_no
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
    parser.add_argument("--debug", action="store_true", help="verbose logging (logs/jarvis.log)")
    return parser.parse_args(argv)


def _fix_console_encoding() -> None:
    """Windows consoles default to a legacy code page; make printing unicode safe."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass


def typed_confirm(question: str) -> bool:
    """Ask a yes/no question in the terminal."""
    try:
        answer = input(f"\n  [confirm] {question} (yes/no) > ")
    except (EOFError, KeyboardInterrupt):
        return False
    return parse_yes_no(answer)


def build_agent(config, confirm) -> Agent:  # type: ignore[no-untyped-def]
    """Wire up tools, safety gate and the agent from config."""
    set_context(ToolContext(config=config, notify=lambda msg: print(f"\n[Jarvis] {msg}")))
    registry = ToolRegistry.from_config(config)
    gate = SafetyGate(
        confirm=confirm,
        audit_log=config.path("paths.logs_dir", "logs") / "tool_calls.jsonl",
        confirm_dangerous=bool(config.get("safety.confirm_dangerous", True)),
    )
    log.info("Loaded %d tools: %s", len(registry.specs), ", ".join(sorted(registry.specs)))
    return Agent(config, registry, gate)


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
                flag = " [needs confirmation]" if spec.dangerous else ""
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


def run_voice_mode(config: Config) -> int:
    """Push-to-talk voice loop. Returns a process exit code."""
    from core.audio_in import AudioError, HotkeyError, MicRecorder, resolve_device
    from core.sounds import Sounds
    from core.stt import STTError, create_stt
    from core.voice import VoiceAssistant

    try:
        agent = build_agent(config, typed_confirm)  # confirm is re-pointed below
    except AgentError as exc:
        print(f"Startup error: {exc}", file=sys.stderr)
        return 2

    try:
        recorder = MicRecorder(config.get("audio.input_device"), int(config.get("audio.sample_rate", 16000)))
        recorder.open()
    except AudioError as exc:
        print(f"Microphone error: {exc}", file=sys.stderr)
        return 2

    model = config.get("stt.model", "small.en")
    print(f"Loading speech recognition ({model}); the first run downloads the model...")
    try:
        stt = create_stt(config)
    except STTError as exc:
        recorder.close()
        print(f"Speech recognition error: {exc}", file=sys.stderr)
        return 2
    print(f"Speech recognition ready on {stt.device.upper()}.")

    try:
        out_device = resolve_device(config.get("audio.output_device"), "output")
    except AudioError:
        out_device = None
    sounds = Sounds(out_device, enabled=bool(config.get("audio.beeps", True)))

    assistant: VoiceAssistant | None = None

    def tts_error(text: str, exc: Exception) -> None:
        if assistant is not None:
            assistant.on_tts_error(text, exc)

    tts = make_tts(config, on_error=tts_error)
    assistant = VoiceAssistant(
        agent,
        recorder,
        stt,
        tts,
        hotkey=str(config.get("hotkey", "f9")),
        sounds=sounds,
        min_press_s=float(config.get("audio.min_press_s", 0.3)),
        hotkey_backend=str(config.get("hotkey_backend", "auto")),
    )
    agent.gate.confirm = assistant.confirm
    try:
        assistant.run()
    except HotkeyError as exc:
        print(f"Hotkey error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nGoodbye.")
    finally:
        assistant.close()
        agent.registry.shutdown()
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

    if args.wake:
        print("Wake word mode arrives in Phase 3. For now: python main.py --push-to-talk")
        return 1

    try:
        if not args.text:
            return run_voice_mode(config)

        try:
            agent = build_agent(config, typed_confirm)
        except AgentError as exc:
            print(f"Startup error: {exc}", file=sys.stderr)
            return 2
        tts = make_tts(config) if args.speak else None
        try:
            run_text_mode(agent, tts)
        finally:
            if tts is not None:
                tts.shutdown()
            agent.registry.shutdown()
        return 0
    finally:
        log.info("Jarvis stopped. Log: %s", log_file)


if __name__ == "__main__":
    sys.exit(main())
