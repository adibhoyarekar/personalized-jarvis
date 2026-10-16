"""``python main.py --setup``: an interactive first-run wizard.

Asks for the API key (written to .env), microphone and speakers, your name,
wake word sensitivity, headphones, weather city, projects and extra safe
folders, then writes config.yaml (a backup is kept as config.yaml.bak).
"""

from __future__ import annotations

import getpass
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import yaml

from core.config import DEFAULT_CONFIG_PATH, ConfigError, load_config, resolve_path
from core.config_editor import set_block, set_env, set_value

Ask = Callable[[str], str]


def _list_devices(kind: str) -> list[tuple[int, str]]:
    from core.audio_in import _sd

    sd = _sd()
    key = "max_input_channels" if kind == "input" else "max_output_channels"
    hostapis = sd.query_hostapis()
    out = []
    for idx, dev in enumerate(sd.query_devices()):
        if dev[key] > 0:
            api = hostapis[dev["hostapi"]]["name"].replace("Windows ", "")
            out.append((idx, f"{dev['name']} ({api})"))
    return out


def _test_key(key: str) -> str:
    import anthropic

    try:
        anthropic.Anthropic(api_key=key, timeout=10, max_retries=0).models.list(limit=1)
        return "ok"
    except anthropic.AuthenticationError:
        return "rejected"
    except anthropic.APIConnectionError:
        return "offline"
    except Exception as exc:  # noqa: BLE001
        return f"error: {exc}"


def _geocode(city: str) -> tuple[float, float, str] | None:
    try:
        from tools.web_tools import geocode

        return geocode(city)
    except Exception:  # noqa: BLE001 - offline or unknown city
        return None


@dataclass
class WizardIO:
    """All interaction, injectable for tests."""

    ask: Ask = input
    secret: Ask = getpass.getpass
    out: Callable[[str], None] = print
    devices: Callable[[str], list[tuple[int, str]]] = _list_devices
    test_key: Callable[[str], str] = _test_key
    geocode: Callable[[str], tuple[float, float, str] | None] = _geocode
    extra: dict[str, Any] = field(default_factory=dict)


def _yes(answer: str, default: bool = False) -> bool:
    a = answer.strip().lower()
    return default if not a else a in ("y", "yes", "yeah", "true", "1")


def run_wizard(config_path: Path | None = None, io: WizardIO | None = None) -> int:
    """Run the wizard. Returns 0 on success."""
    io = io or WizardIO()
    path = config_path or DEFAULT_CONFIG_PATH
    if not path.is_file():
        io.out(f"config.yaml not found at {path}")
        return 2
    text = path.read_text(encoding="utf-8")
    current = yaml.safe_load(text) or {}

    def cur(key: str, default: Any = None) -> Any:
        node: Any = current
        for part in key.split("."):
            node = node.get(part) if isinstance(node, dict) else None
        return default if node is None else node

    io.out("Jarvis setup - press Enter to keep the value in [brackets].\n")

    # 1. API key -> .env
    env_path = path.parent / ".env"
    existing = os.environ.get("ANTHROPIC_API_KEY", "")
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("ANTHROPIC_API_KEY="):
                existing = line.split("=", 1)[1].strip()
    shown = f"{existing[:7]}...{existing[-4:]}" if len(existing) > 12 and not existing.endswith("...") else "not set"
    io.out("1) Anthropic API key (from https://platform.claude.com/settings/keys)")
    key = io.secret(f"   Paste your key [{shown}]: ").strip()
    if key:
        if not key.startswith("sk-ant-"):
            io.out("   That doesn't look like an Anthropic key (they start with sk-ant-). Keeping the old one.")
        else:
            result = io.test_key(key)
            io.out({"ok": "   Key works.", "rejected": "   Warning: the API rejected this key.",
                    "offline": "   Couldn't reach the API to test it (offline?) - saving anyway."}.get(
                result, f"   Couldn't test the key ({result}) - saving anyway."))
            set_env(env_path, "ANTHROPIC_API_KEY", key)
            io.out(f"   Saved to {env_path}")

    # 2. Name
    name = io.ask(f"\n2) What should Jarvis call you? [{cur('assistant.user_name', '') or 'sir'}]: ").strip()
    if name:
        text = set_value(text, "assistant.user_name", name)

    # 3. Microphone and speakers
    for kind, label, key_name in (("input", "microphone", "audio.input_device"),
                                  ("output", "speakers/headphones", "audio.output_device")):
        try:
            devices = io.devices(kind)
        except Exception as exc:  # noqa: BLE001 - no audio system
            io.out(f"\n   (Can't list audio devices: {exc}. Skipping.)")
            continue
        io.out(f"\n3) Which {label}?")
        for idx, desc in devices:
            io.out(f"   {idx:>3}: {desc}")
        now = cur(key_name)
        choice = io.ask(f"   Number, or Enter for {'the Windows default' if now is None else now}: ").strip()
        if choice.isdigit() and int(choice) in {i for i, _ in devices}:
            text = set_value(text, key_name, int(choice))
        elif choice:
            io.out("   Not in the list - keeping the current setting.")

    # 4. Wake word sensitivity
    io.out("\n4) Wake word threshold: lower = triggers more easily, higher = fewer false alarms.")
    thr = io.ask(f"   Threshold 0.2-0.9 [{cur('wake.threshold', 0.5)}]: ").strip()
    if thr:
        try:
            value = float(thr)
            if not 0.2 <= value <= 0.9:
                raise ValueError
            text = set_value(text, "wake.threshold", value)
        except ValueError:
            io.out("   Please use a number between 0.2 and 0.9 - keeping the current value.")

    # 5. Headphones
    head = io.ask(f"\n5) Do you use headphones (allows interrupting Jarvis by voice)? "
                  f"[{'y' if cur('audio.headphones', False) else 'n'}]: ")
    if head.strip():
        text = set_value(text, "audio.headphones", _yes(head))

    # 6. Weather city
    city = io.ask(f"\n6) Your city for weather [{cur('weather.city', '')}]: ").strip()
    if city:
        found = io.geocode(city)
        if found:
            lat, lon, label = found
            io.out(f"   Found {label}.")
            text = set_value(text, "weather.city", city)
            text = set_value(text, "weather.lat", round(lat, 4))
            text = set_value(text, "weather.lon", round(lon, 4))
        else:
            io.out("   Couldn't look it up now; Jarvis will find it on first use.")
            text = set_value(text, "weather.city", city)
            text = set_value(text, "weather.lat", None)
            text = set_value(text, "weather.lon", None)

    # 7. Projects
    projects: dict[str, Any] = dict(cur("projects", {}) or {})
    io.out("\n7) Coding projects (for 'open cs-events', 'git status', dev servers).")
    for pname, spec in projects.items():
        io.out(f"   - {pname}: {(spec or {}).get('path') or '(no path yet)'}")
    changed = False
    for pname, spec in list(projects.items()):
        if not (spec or {}).get("path"):
            ppath = io.ask(f"   Folder for '{pname}' (Enter to skip): ").strip().strip('"')
            if ppath:
                projects[pname] = {**(spec or {}), "path": ppath}
                changed = True
                if not resolve_path(ppath, Path.home()).is_dir():
                    io.out("   (That folder doesn't exist yet - check the path.)")
    while True:
        pname = io.ask("   Add another project? Name (Enter to finish): ").strip()
        if not pname:
            break
        ppath = io.ask(f"   Folder for '{pname}': ").strip().strip('"')
        cmd = io.ask("   Dev server command [npm run dev]: ").strip() or "npm run dev"
        projects[pname] = {"path": ppath, "dev_command": cmd}
        changed = True
        if ppath and not resolve_path(ppath, Path.home()).is_dir():
            io.out("   (That folder doesn't exist yet - check the path.)")
    if changed:
        text = set_block(text, "projects", projects)

    # 8. Extra safe folders
    roots = list(cur("safe_roots", ["~/Documents", "~/Downloads", "~/Desktop"]))
    io.out(f"\n8) Folders file tools may use: {', '.join(roots)}")
    added = False
    while True:
        folder = io.ask("   Add another folder (Enter to finish): ").strip().strip('"')
        if not folder:
            break
        if folder not in roots:
            roots.append(folder)
            added = True
    if added:
        text = set_value(text, "safe_roots", roots)

    # Write + validate (restore the backup if the result is invalid)
    backup = path.with_suffix(".yaml.bak")
    shutil.copyfile(path, backup)
    path.write_text(text, encoding="utf-8")
    try:
        load_config(path)
    except ConfigError as exc:
        shutil.copyfile(backup, path)
        io.out(f"\nSomething went wrong, so config.yaml was left unchanged:\n{exc}")
        return 1
    io.out(f"\nSaved {path} (previous version: {backup.name}).")
    io.out("Next: python main.py --check   then   python main.py")
    return 0
