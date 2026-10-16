"""--check on a mocked environment."""

from __future__ import annotations

from pathlib import Path

from core.checks import FAIL, PASS, SKIP, WARN, Probes, collect_checks, format_table, run_checks
from core.config import Config


def config(tmp_path: Path, **overrides: object) -> Config:
    docs = tmp_path / "Documents"
    docs.mkdir(exist_ok=True)
    (tmp_path / "proj").mkdir(exist_ok=True)
    data = {
        "safe_roots": [str(docs)],
        "notes_dir": str(docs / "Notes"),
        "projects": {"cs-events": {"path": str(tmp_path / "proj")}},
        "tts": {"engine": "edge", "voice": "en-GB-RyanNeural"},
    }
    data.update(overrides)
    return Config(data, tmp_path)


def healthy() -> Probes:
    return Probes(
        api_key=lambda: "sk-ant-api03-abcdefghijkl1234",
        api_ping=lambda cfg: "ok",
        device=lambda cfg, kind: {"input": "Headset Mic", "output": "Speakers"}[kind],
        whisper_cached=lambda cfg: True,
        wake_present=lambda cfg: True,
        tts_test=lambda cfg: "en-GB-RyanNeural, 1.1s of audio",
        hotkey=lambda: "keyboard library",
    )


def by_name(results: list) -> dict[str, tuple[str, str]]:  # type: ignore[type-arg]
    return {r.name: (r.status, r.detail) for r in results}


def test_all_green(tmp_path: Path) -> None:
    lines: list[str] = []
    assert run_checks(config(tmp_path), healthy(), out=lines.append) == 0
    table = "\n".join(lines)
    for name in ("Config file", "API key", "Claude API", "Microphone", "Speakers", "Speech model",
                 "Wake word model", "Text-to-speech", "Hotkey", "Safe folders", "Project cs-events"):
        assert name in table
    assert "FAIL" not in table and "0 failed" in table and "sk-ant-...1234" in table
    assert "All good" in table


def test_failures_are_reported(tmp_path: Path) -> None:
    def no_mic(cfg: Config, kind: str) -> str:
        if kind == "input":
            raise RuntimeError("No microphone found.\nsecond line hidden")
        return "Speakers"

    probes = healthy()
    probes.api_ping = lambda cfg: "auth"
    probes.device = no_mic
    probes.whisper_cached = lambda cfg: False
    probes.tts_test = lambda cfg: (_ for _ in ()).throw(RuntimeError("edge-tts request failed"))
    cfg = config(tmp_path, safe_roots=[str(tmp_path / "nope")],
                 projects={"site": {"path": str(tmp_path / "missing")}, "later": {"path": ""}})
    results = by_name(collect_checks(cfg, probes))
    assert results["Claude API"] == (FAIL, "API key rejected - create a new key")
    assert results["Microphone"] == (FAIL, "No microphone found.")
    assert results["Speech model"][0] == WARN
    assert results["Text-to-speech"] == (FAIL, "edge-tts request failed")
    assert results["Safe folders"][0] == FAIL
    assert results["Project site"][0] == FAIL and results["Project later"] == (WARN, "path not set")
    assert run_checks(cfg, probes, out=lambda s: None) == 1


def test_missing_key_skips_api(tmp_path: Path) -> None:
    probes = healthy()
    probes.api_key = lambda: None
    calls: list[str] = []
    probes.api_ping = lambda cfg: calls.append("pinged") or "ok"  # type: ignore[func-returns-value]
    results = by_name(collect_checks(config(tmp_path), probes))
    assert results["API key"][0] == FAIL and results["Claude API"][0] == SKIP and calls == []


def test_notes_outside_safe_roots_warns(tmp_path: Path) -> None:
    results = by_name(collect_checks(config(tmp_path, notes_dir=str(tmp_path / "elsewhere")), healthy()))
    assert results["Notes folder"][0] == WARN


def test_table_format() -> None:
    from core.checks import CheckResult

    table = format_table([CheckResult("A", PASS, "fine"), CheckResult("Longer name", FAIL, "bad\nthing")])
    lines = table.splitlines()
    assert lines[0].startswith("Check        Result")
    assert lines[3] == "Longer name  FAIL    bad thing"
    assert lines[-1] == "1 passed, 0 warning(s), 1 failed"


def test_gemini_provider_checks_gemini_key_and_api(tmp_path: Path) -> None:
    probes = healthy()
    probes.api_key = lambda: None  # no Anthropic key is fine when using Gemini
    probes.gemini_key = lambda: "AIzaSyabcdefghijkl9876"
    probes.gemini_ping = lambda cfg: "model"
    cfg = config(tmp_path, llm={"provider": "gemini", "gemini_model": "gemini-x"})
    results = by_name(collect_checks(cfg, probes))
    assert results["API key"] == (PASS, "GEMINI_API_KEY AIzaSya...9876")
    assert results["Gemini API"] == (FAIL, "model 'gemini-x' not found - check llm.gemini_model")
    assert "Claude API" not in results
