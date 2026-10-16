"""Jarvis entry point.

Usage:
    python main.py                   # same as --wake
    python main.py --wake            # say "Hey Jarvis" hands-free (F9 also works)
    python main.py --push-to-talk    # hold the hotkey (F9), speak, release
    python main.py --text            # type to Jarvis, read replies
    python main.py --text --speak    # type, and hear the replies
    python main.py --tray            # with a system tray icon (default under pythonw)
    python main.py --setup           # first-run wizard: API key, mic, wake word, projects
    python main.py --check           # PASS/FAIL health check of the whole setup
    python main.py --list-devices    # show audio devices and their indexes
    python main.py --debug           # any mode: show state changes and wake word scores
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from core.agent import Agent, AgentError
from core.app import RESTART_ENV, build_runtime, make_tts
from core.config import Config, ConfigError, load_config
from core.logging_setup import setup_logging
from core.notifications import show_toast
from core.single_instance import SingleInstance
from core.tts import SpeechStream, TTSEngine

log = logging.getLogger("jarvis")


def running_windowless() -> bool:
    """True under pythonw.exe (no console), e.g. when started at login."""
    return sys.stdout is None or Path(sys.executable).stem.lower() == "pythonw"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="jarvis", description="Jarvis personal voice assistant")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--wake", action="store_true", help="voice mode, wake word activation (default)")
    mode.add_argument("--push-to-talk", action="store_true", help="voice mode, hold a hotkey to talk")
    mode.add_argument("--text", action="store_true", help="text-only chat in the terminal")
    mode.add_argument("--list-devices", action="store_true", help="list audio input/output devices")
    mode.add_argument("--check", action="store_true", help="check API key, mic, models, paths, TTS and API")
    mode.add_argument("--setup", action="store_true", help="interactive first-run setup wizard")
    tray = parser.add_mutually_exclusive_group()
    tray.add_argument("--tray", dest="tray", action="store_true", default=None,
                      help="show the system tray icon (default when run with pythonw)")
    tray.add_argument("--no-tray", dest="tray", action="store_false", help="no tray icon")
    parser.add_argument("--overlay", dest="overlay", action="store_true", default=None,
                        help="show the on-screen overlay (or set ui.overlay in config.yaml)")
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


def _error(message: str) -> None:
    """Report a startup problem on the console, or as a toast when there is none."""
    if sys.stderr is not None:
        print(message, file=sys.stderr)
    if running_windowless():
        show_toast("Jarvis", message)


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


def run_text(config: Config, speak: bool) -> int:
    try:
        runtime = build_runtime(config)
    except AgentError as exc:
        _error(f"Startup error: {exc}")
        return 2
    tts = make_tts(config) if speak else None
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


def acquire_instance_lock(config: Config) -> SingleInstance | None:
    """One Jarvis at a time. After a restart, wait for the old process to exit first."""
    lock = SingleInstance(config.path("paths.data_dir", "data"))
    wait = 15.0 if os.environ.pop(RESTART_ENV, None) else 0.0
    if lock.acquire(wait_s=wait):
        return lock
    pid = lock.other_pid()
    message = "Jarvis is already running" + (f" (process {pid})" if pid else "") + "."
    log.warning(message)
    if sys.stderr is not None:
        print(message + " Quit it from the tray icon first.", file=sys.stderr)
    show_toast("Jarvis", "Jarvis is already running.", sound=False)
    return None


def main(argv: list[str] | None = None) -> int:
    _fix_console_encoding()
    args = parse_args(argv)

    if args.setup:
        from core.setup_wizard import run_wizard

        return run_wizard(args.config)

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        _error(f"Config error: {exc}")
        return 2

    if args.check:
        from core.checks import run_checks

        return run_checks(config)

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

    lock = acquire_instance_lock(config)
    if lock is None:
        return 1
    try:
        if args.text:
            return run_text(config, args.speak)
        from core.app import VoiceApp

        tray = args.tray if args.tray is not None else (running_windowless() or bool(config.get("ui.tray", False)))
        app = VoiceApp(
            config,
            wake=not args.push_to_talk,
            debug=args.debug,
            tray=tray,
            overlay=args.overlay,
            argv=sys.argv[1:] if argv is None else argv,
        )
        return app.run()
    finally:
        lock.release()
        log.info("Jarvis stopped. Log: %s", log_file)


if __name__ == "__main__":
    sys.exit(main())
