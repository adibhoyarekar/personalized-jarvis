"""``python main.py --check``: a PASS/FAIL table covering the whole setup.

Each probe is a plain function on :class:`Probes`, so tests can swap in fakes.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from core.agent import DEFAULT_GEMINI_MODEL, gemini_api_key, llm_provider
from core.config import Config, resolve_path

PASS, WARN, FAIL, SKIP = "PASS", "WARN", "FAIL", "SKIP"


@dataclass
class CheckResult:
    name: str
    status: str
    detail: str = ""


# -- real probes ----------------------------------------------------------------------


def _api_key() -> str | None:
    return os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")


def _gemini_ping(config: Config) -> str:
    import openai

    from core.agent import DEFAULT_GEMINI_MODEL, GEMINI_BASE_URL, gemini_api_key

    model = config.get("llm.gemini_model", DEFAULT_GEMINI_MODEL)
    try:
        client = openai.OpenAI(api_key=gemini_api_key(), base_url=GEMINI_BASE_URL, timeout=10, max_retries=0)
        client.models.retrieve(model)
        return "ok"
    except openai.APIConnectionError:
        return "offline"
    except (openai.AuthenticationError, openai.PermissionDeniedError):
        return "auth"
    except openai.NotFoundError:
        return "model"
    except openai.BadRequestError as exc:
        return "auth" if "API key" in str(exc) or "API_KEY" in str(exc) else f"error: {exc}"
    except Exception as exc:  # noqa: BLE001
        return f"error: {exc}"


def _api_ping(config: Config) -> str:
    import anthropic

    try:
        anthropic.Anthropic(timeout=10, max_retries=0).models.retrieve(config.get("llm.model", "claude-sonnet-5-5"))
        return "ok"
    except (anthropic.APIConnectionError, anthropic.APITimeoutError):
        return "offline"
    except (anthropic.AuthenticationError, anthropic.PermissionDeniedError):
        return "auth"
    except anthropic.NotFoundError:
        return "model"
    except Exception as exc:  # noqa: BLE001
        return f"error: {exc}"


def _device(config: Config, kind: str) -> str:
    from core.audio_in import _sd, resolve_device

    idx = resolve_device(config.get(f"audio.{kind}_device"), kind)
    return str(_sd().query_devices(idx, kind=kind)["name"])


def _whisper_cached(config: Config) -> bool:
    from faster_whisper.utils import download_model

    model = str(config.get("stt.model", "small.en"))
    if Path(model).is_dir():
        return True
    try:
        download_model(model, local_files_only=True, cache_dir=config.get("stt.download_root"))
        return True
    except Exception:  # noqa: BLE001 - not cached
        return False


def _wake_present(config: Config) -> bool:
    from core.audio_in import wake_model_files_present

    return wake_model_files_present(str(config.get("wake.model", "hey_jarvis")))


def _tts_test(config: Config) -> str:
    from core.tts import create_tts

    engine = create_tts(config)
    try:
        samples, rate = engine.synthesize("Testing.")
        return f"{config.get('tts.voice', 'en-GB-RyanNeural')}, {len(samples) / rate:.1f}s of audio"
    finally:
        engine.shutdown()


def _hotkey() -> str:
    if sys.platform == "win32":
        import keyboard  # noqa: F401

        return "keyboard library"
    import pynput  # noqa: F401

    return "pynput"


@dataclass
class Probes:
    """Everything --check touches outside pure config. Replace any of them in tests."""

    api_key: Callable[[], str | None] = _api_key
    api_ping: Callable[[Config], str] = _api_ping
    gemini_key: Callable[[], str | None] = gemini_api_key
    gemini_ping: Callable[[Config], str] = _gemini_ping
    device: Callable[[Config, str], str] = _device
    whisper_cached: Callable[[Config], bool] = _whisper_cached
    wake_present: Callable[[Config], bool] = _wake_present
    tts_test: Callable[[Config], str] = _tts_test
    hotkey: Callable[[], str] = _hotkey
    extra: dict[str, Any] = field(default_factory=dict)


# -- checks -----------------------------------------------------------------------------


def _safe(fn: Callable[[], CheckResult], name: str) -> CheckResult:
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 - a probe crash is a FAIL, not a crash of --check
        first_line = (str(exc).strip().splitlines() or [""])[0]
        return CheckResult(name, FAIL, first_line or type(exc).__name__)


def collect_checks(config: Config, probes: Probes | None = None) -> list[CheckResult]:
    p = probes or Probes()
    results = [CheckResult("Config file", PASS, f"{config.base_dir / 'config.yaml'} is valid")]

    gemini = llm_provider(config) == "gemini"
    if gemini:
        key, env_name, label, host = p.gemini_key(), "GEMINI_API_KEY", "Gemini API", "generativelanguage.googleapis.com"
        model_key, model = "llm.gemini_model", config.get("llm.gemini_model", DEFAULT_GEMINI_MODEL)
        ping = p.gemini_ping
    else:
        key, env_name, label, host = p.api_key(), "ANTHROPIC_API_KEY", "Claude API", "api.anthropic.com"
        model_key, model = "llm.model", config.get("llm.model", "claude-sonnet-5-5")
        ping = p.api_ping
    if key:
        masked = key[:7] + "..." + key[-4:] if len(key) > 12 else "set"
        results.append(CheckResult("API key", PASS, f"{env_name} {masked}"))
    else:
        results.append(CheckResult("API key", FAIL, f"missing: run --setup or put {env_name} in .env"))

    def api() -> CheckResult:
        if not key:
            return CheckResult(label, SKIP, "no API key")
        status = ping(config)
        return {
            "ok": CheckResult(label, PASS, f"reachable, model {model}"),
            "offline": CheckResult(label, FAIL, f"can't reach {host} (internet/firewall?)"),
            "auth": CheckResult(label, FAIL, "API key rejected - create a new key"),
            "model": CheckResult(label, FAIL, f"model '{model}' not found - check {model_key}"),
        }.get(status, CheckResult(label, WARN, status))

    results.append(_safe(api, label))
    for kind, label in (("input", "Microphone"), ("output", "Speakers")):
        results.append(_safe(lambda k=kind, n=label: CheckResult(n, PASS, p.device(config, k)), label))

    def whisper() -> CheckResult:
        model = config.get("stt.model", "small.en")
        if p.whisper_cached(config):
            return CheckResult("Speech model", PASS, f"Whisper {model} downloaded")
        return CheckResult("Speech model", WARN, f"Whisper {model} not downloaded yet (downloads on first voice run)")

    def wake() -> CheckResult:
        model = config.get("wake.model", "hey_jarvis")
        if p.wake_present(config):
            return CheckResult("Wake word model", PASS, f"{model} present")
        return CheckResult("Wake word model", WARN, f"{model} not downloaded yet (downloads on first run)")

    def tts() -> CheckResult:
        if str(config.get("tts.engine", "edge")) != "edge":
            return CheckResult("Text-to-speech", WARN, f"engine '{config.get('tts.engine')}' isn't implemented")
        return CheckResult("Text-to-speech", PASS, p.tts_test(config))

    results += [_safe(whisper, "Speech model"), _safe(wake, "Wake word model"), _safe(tts, "Text-to-speech")]
    results.append(_safe(lambda: CheckResult("Hotkey", PASS, f"{config.get('hotkey', 'f9')} via {p.hotkey()}"), "Hotkey"))
    results += _path_checks(config)
    return results


def _path_checks(config: Config) -> list[CheckResult]:
    out = []
    roots = [str(r) for r in (config.get("safe_roots") or [])]
    resolved = [(r, resolve_path(r, Path.home())) for r in roots]
    missing = [r for r, path in resolved if not path.is_dir()]
    if not roots:
        out.append(CheckResult("Safe folders", FAIL, "safe_roots is empty - file tools can't work"))
    elif len(missing) == len(roots):
        out.append(CheckResult("Safe folders", FAIL, f"none exist: {', '.join(missing)}"))
    elif missing:
        out.append(CheckResult("Safe folders", WARN, f"missing: {', '.join(missing)}"))
    else:
        out.append(CheckResult("Safe folders", PASS, ", ".join(roots)))

    notes = resolve_path(str(config.get("notes_dir", "~/Documents/Jarvis Notes")), Path.home())
    if resolved and not any(_inside(notes, path) for _, path in resolved):
        out.append(CheckResult("Notes folder", WARN, f"{notes} is outside safe_roots, so create_note will refuse"))

    projects = config.get("projects") or {}
    if not projects:
        out.append(CheckResult("Projects", WARN, "none configured (optional)"))
    for name, spec in projects.items():
        raw = str((spec or {}).get("path") or "")
        label = f"Project {name}"
        if not raw:
            out.append(CheckResult(label, WARN, "path not set"))
            continue
        path = resolve_path(raw, Path.home())
        out.append(CheckResult(label, PASS, str(path)) if path.is_dir() else CheckResult(label, FAIL, f"not found: {path}"))
    return out


def _inside(path: Path, root: Path) -> bool:
    try:
        return os.path.commonpath([os.path.normcase(path.resolve()), os.path.normcase(root.resolve())]) == os.path.normcase(root.resolve())
    except ValueError:
        return False


def format_table(results: list[CheckResult]) -> str:
    width = max(len(r.name) for r in results)
    lines = [f"{'Check':<{width}}  Result  Details", f"{'-' * width}  ------  -------"]
    lines += [f"{r.name:<{width}}  {r.status:<6}  {' '.join(r.detail.split())[:160]}" for r in results]
    counts = {s: sum(r.status == s for r in results) for s in (PASS, WARN, FAIL, SKIP)}
    summary = f"{counts[PASS]} passed, {counts[WARN]} warning(s), {counts[FAIL]} failed"
    if counts[SKIP]:
        summary += f", {counts[SKIP]} skipped"
    lines += ["", summary]
    return "\n".join(lines)


def run_checks(config: Config, probes: Probes | None = None, out: Callable[[str], None] = print) -> int:
    """Print the table; exit code 1 if anything FAILed."""
    out("Checking your Jarvis setup...\n")
    results = collect_checks(config, probes)
    out(format_table(results))
    failed = any(r.status == FAIL for r in results)
    out("\nAll good - start Jarvis with: python main.py" if not failed
        else "\nFix the FAIL lines above (python main.py --setup helps), then run --check again.")
    return 1 if failed else 0
