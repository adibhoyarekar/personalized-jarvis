"""config.yaml validation, the comment-preserving editor, and the setup wizard."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from core.config import DEFAULT_CONFIG_PATH, ConfigError, load_config
from core.config_editor import set_block, set_env, set_value
from core.config_schema import validate_config
from core.setup_wizard import WizardIO, run_wizard


def test_shipped_config_is_valid() -> None:
    data = yaml.safe_load(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))
    assert validate_config(data) == []
    assert validate_config({}) == []  # every setting is optional


@pytest.mark.parametrize(
    "data,expected",
    [
        ({"llm": {"effort": "fast"}}, "llm.effort: must be one of"),
        ({"llm": {"max_tokens": "lots"}}, "llm.max_tokens"),
        ({"wake": {"threshold": 1.5}}, "wake.threshold"),
        ({"vad": {"silense_timeout": 1.0}}, "vad.silense_timeout: unknown setting (typo?)"),
        ({"tts": {"rate": "15%"}}, "tts.rate: must look like '+10%'"),
        ({"tts": {"engine": "siri"}}, "tts.engine: must be one of"),
        ({"safe_roots": "~/Documents"}, "safe_roots"),
        ({"news_feeds": ["ftp://x"]}, "is not an http(s) URL"),
        ({"weather": {"lat": 123}}, "weather.lat"),
        ({"safety": {"confirm_timeout": 0}}, "safety.confirm_timeout"),
        ({"ui": {"overlay_position": "middle"}}, "ui.overlay_position: must be one of"),
        ({"projects": {"x": {"pth": "~/code"}}}, "projects.x.pth: unknown setting"),
    ],
)
def test_bad_values_are_explained(data: dict, expected: str) -> None:
    problems = validate_config(data)
    assert any(expected in p for p in problems), problems


def test_custom_top_level_sections_are_allowed() -> None:
    assert validate_config({"my_tool": {"anything": 1}}) == []


def test_load_config_lists_every_problem(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("llm:\n  effort: turbo\nwake:\n  threshold: 2\n", encoding="utf-8")
    with pytest.raises(ConfigError) as err:
        load_config(path)
    message = str(err.value)
    assert "2 problem(s)" in message and "llm.effort" in message and "wake.threshold" in message
    path.write_text("llm: [unclosed\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="not valid YAML"):
        load_config(path)


SAMPLE = """\
# Jarvis config
assistant:
  name: Jarvis
  user_name: ""            # how Jarvis addresses you

audio:
  input_device: null        # mic

safe_roots: ["~/Documents"]

projects:
  cs-events:
    path: ""                # set me
    dev_command: "npm run dev"

weather:
  city: "X"
"""


def test_editor_preserves_comments_and_structure() -> None:
    text = set_value(SAMPLE, "assistant.user_name", "Aditya")
    text = set_value(text, "audio.input_device", 3)
    text = set_value(text, "safe_roots", ["~/Documents", "D:/Work"])
    text = set_value(text, "audio.headphones", True)  # new key in an existing section
    text = set_value(text, "ui.overlay", True)  # new section
    text = set_block(text, "projects", {"cs-events": {"path": "~/code/cs", "dev_command": "npm run dev"}})
    assert "# Jarvis config" in text and "# how Jarvis addresses you" in text and "# mic" in text
    data = yaml.safe_load(text)
    assert data["assistant"]["user_name"] == "Aditya"
    assert data["audio"] == {"input_device": 3, "headphones": True}
    assert data["safe_roots"] == ["~/Documents", "D:/Work"]
    assert data["projects"]["cs-events"]["path"] == "~/code/cs"
    assert data["weather"]["city"] == "X" and data["ui"]["overlay"] is True


def test_set_env_keeps_other_lines(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("# comment\nOTHER=1\nANTHROPIC_API_KEY=old\n")
    set_env(env, "ANTHROPIC_API_KEY", "sk-ant-new")
    assert env.read_text() == "# comment\nOTHER=1\nANTHROPIC_API_KEY=sk-ant-new\n"


def test_wizard_writes_valid_config(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"), encoding="utf-8")
    (tmp_path / "proj").mkdir()
    answers = iter(["Aditya", "2", "", "0.6", "y", "Pune", str(tmp_path / "proj"), "", "D:/Work", ""])
    out: list[str] = []
    io = WizardIO(
        ask=lambda q: next(answers),
        secret=lambda q: "sk-ant-api03-test-key-123456",
        out=out.append,
        devices=lambda kind: [(1, "Mic"), (2, "Headset")] if kind == "input" else [(3, "Speakers")],
        test_key=lambda key: "ok",
        geocode=lambda city: (18.52, 73.86, "Pune, India"),
    )
    assert run_wizard(cfg, io) == 0
    data = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    assert data["assistant"]["user_name"] == "Aditya"
    assert data["audio"]["input_device"] == 2 and data["audio"]["output_device"] is None
    assert data["wake"]["threshold"] == 0.6 and data["audio"]["headphones"] is True
    assert data["weather"] == {"city": "Pune", "lat": 18.52, "lon": 73.86, "units": "celsius"}
    assert data["projects"]["cs-events"]["path"] == str(tmp_path / "proj")
    assert "D:/Work" in data["safe_roots"]
    assert "ANTHROPIC_API_KEY=sk-ant-api03-test-key-123456" in (tmp_path / ".env").read_text()
    assert (tmp_path / "config.yaml.bak").exists()
    assert "# --- Voice (Phase 2)" in cfg.read_text(encoding="utf-8")  # comments survived


def test_wizard_rejects_bad_input_and_keeps_values(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"), encoding="utf-8")
    answers = iter(["", "99", "", "5", "", "", "", "", ""])
    io = WizardIO(ask=lambda q: next(answers), secret=lambda q: "not-a-key", out=lambda s: None,
                  devices=lambda kind: [(1, "Mic")], test_key=lambda k: "ok", geocode=lambda c: None)
    assert run_wizard(cfg, io) == 0
    data = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    assert data["audio"]["input_device"] is None and data["wake"]["threshold"] == 0.5
    assert not (tmp_path / ".env").exists()
