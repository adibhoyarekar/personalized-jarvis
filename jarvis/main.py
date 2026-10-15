"""Jarvis entry point.

Usage:
    python main.py --text            # type to Jarvis, read replies (debug mode)
    python main.py --push-to-talk    # hold-to-talk voice mode        (Phase 2)
    python main.py --wake            # say "Jarvis" hands-free         (Phase 3)
    python main.py --list-devices    # show audio devices              (Phase 2)
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from core.agent import Agent, AgentError
from core.config import ConfigError, load_config
from core.logging_setup import setup_logging
from core.safety import SafetyGate, parse_yes_no
from tools import ToolContext, ToolRegistry, set_context

log = logging.getLogger("jarvis")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="jarvis", description="Jarvis personal voice assistant")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--text", action="store_true", help="text-only chat in the terminal")
    mode.add_argument("--push-to-talk", action="store_true", help="voice mode, hold a hotkey to talk")
    mode.add_argument("--wake", action="store_true", help="voice mode, wake word activation")
    mode.add_argument("--list-devices", action="store_true", help="list audio input/output devices")
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


def run_text_mode(agent: Agent) -> None:
    """Interactive terminal chat: the debugging front-end for the agent."""
    name = agent.name
    print(f"{name} text mode. {HELP}\n")
    while True:
        try:
            user_text = input("You > ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
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

        try:
            agent.ask(user_text, on_text=lambda t: print(t, end="", flush=True), on_status=show_status)
        except KeyboardInterrupt:
            agent.cancel()
            print(" (interrupted)")
        print("\n")
    print("Goodbye.")


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

    if args.list_devices or args.push_to_talk or args.wake:
        print("Voice features arrive in Phase 2/3. For now run: python main.py --text")
        return 1

    try:
        agent = build_agent(config, typed_confirm)
    except AgentError as exc:
        print(f"Startup error: {exc}", file=sys.stderr)
        return 2

    try:
        run_text_mode(agent)
    finally:
        agent.registry.shutdown()
        log.info("Jarvis stopped. Log: %s", log_file)
    return 0


if __name__ == "__main__":
    sys.exit(main())
