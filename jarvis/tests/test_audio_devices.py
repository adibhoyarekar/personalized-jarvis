"""Choosing the microphone and speaker: device helpers, saving, and switching live."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pytest

import core.app as app_module
import core.audio_in as audio_in
from core.app import VoiceApp
from core.audio_in import AudioError
from core.config import load_config

DEVICES = [
    {"name": "Microsoft Sound Mapper - Input", "hostapi": 0, "max_input_channels": 2, "max_output_channels": 0, "default_samplerate": 44100.0},
    {"name": "Microphone (Kreo Sonik)", "hostapi": 0, "max_input_channels": 1, "max_output_channels": 0, "default_samplerate": 48000.0},
    {"name": "Speakers (Realtek(R) Audio)", "hostapi": 0, "max_input_channels": 0, "max_output_channels": 2, "default_samplerate": 48000.0},
    {"name": "Microphone (Kreo Sonik)", "hostapi": 1, "max_input_channels": 1, "max_output_channels": 0, "default_samplerate": 48000.0},
]


class FakeStream:
    def __init__(self, sd: "FakeSD", callback: Any = None, **kwargs: Any) -> None:
        self.sd, self.callback, self.kwargs = sd, callback, kwargs

    def __enter__(self) -> "FakeStream":
        if self.callback is not None:
            self.callback(np.full((480, 1), 0.05, dtype=np.float32), 480, None, None)
        return self

    def __exit__(self, *exc: object) -> None:
        pass

    def write(self, data: np.ndarray) -> None:
        self.sd.played.append((self.kwargs["device"], self.kwargs["samplerate"], data.shape))


class FakeSD:
    def __init__(self) -> None:
        self.default = type("D", (), {"device": [1, 2]})()
        self.broken: set[int] = set()
        self.played: list[Any] = []

    def query_hostapis(self) -> list[dict[str, str]]:
        return [{"name": "MME"}, {"name": "Windows WASAPI"}]

    def query_devices(self, device: Any = None, kind: str | None = None) -> Any:
        if device is None and kind is None:
            return DEVICES
        return DEVICES[device if device is not None else self.default.device[0 if kind == "input" else 1]]

    def check_input_settings(self, device: int, **_: Any) -> None:
        if device in self.broken:
            raise ValueError("Invalid sample rate")

    check_output_settings = check_input_settings

    def InputStream(self, **kwargs: Any) -> FakeStream:  # noqa: N802 - sounddevice API
        return FakeStream(self, **kwargs)

    def OutputStream(self, **kwargs: Any) -> FakeStream:  # noqa: N802
        return FakeStream(self, **kwargs)


@pytest.fixture
def sd(monkeypatch: pytest.MonkeyPatch) -> FakeSD:
    fake = FakeSD()
    monkeypatch.setattr(audio_in, "_sd", lambda: fake)
    monkeypatch.setattr(audio_in.time, "sleep", lambda _s: None)
    return fake


def test_device_options_and_checks(sd: FakeSD) -> None:
    inputs = audio_in.device_options("input")
    assert [(d["index"], d["api"]) for d in inputs] == [(0, "MME"), (1, "MME"), (3, "WASAPI")]
    assert [d["index"] for d in audio_in.device_options("output")] == [2]
    assert audio_in.default_device("input") == 1 and audio_in.default_device("output") == 2
    assert audio_in.check_device(3, "input") == 3 and audio_in.check_device(None, "output") is None
    with pytest.raises(AudioError, match="not a valid input"):
        audio_in.check_device(2, "input")
    sd.broken.add(3)
    with pytest.raises(AudioError, match="can't be opened"):
        audio_in.check_device(3, "input")


def test_mic_probe_and_speaker_tone(sd: FakeSD) -> None:
    assert audio_in.probe_input(1, seconds=0) == pytest.approx(0.05, rel=1e-3)
    audio_in.play_test_tone(2)
    device, rate, shape = sd.played[0]
    assert (device, rate, shape[1]) == (2, 48000, 1) and shape[0] > 48000 * 0.5


CONFIG = """\
audio:
  input_device: null        # null = Windows default mic
  output_device: null       # name from `python main.py --list-devices`
  sample_rate: 16000
"""


class FakeRecorder:
    opened: list["FakeRecorder"] = []

    def __init__(self, device: Any = None, sample_rate: int = 16000) -> None:
        if device == 1 and FakeRecorder.fail:
            raise AudioError("busy")
        self.device, self.closed, self.listeners = device, False, []
        FakeRecorder.opened.append(self)

    fail = False

    def open(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True

    def add_listener(self, fn: Any) -> None:
        self.listeners.append(fn)


class FakeTTS:
    def __init__(self, device: Any) -> None:
        self.device, self.down = device, False

    def shutdown(self) -> None:
        self.down = True


class Window:
    def __init__(self) -> None:
        self.notes: list[str] = []
        self.logs: list[str] = []

    def on_notice(self, text: str) -> None:
        self.notes.append(text)

    def log(self, tag: str, text: str) -> None:
        self.logs.append(text)


def make_app(tmp_path: Path) -> VoiceApp:
    path = tmp_path / "config.yaml"
    path.write_text(CONFIG, encoding="utf-8")
    return VoiceApp(load_config(path, validate=False), print_fn=lambda *a, **k: None)


def test_saving_devices_keeps_comments_and_reloads_the_session(sd: FakeSD, tmp_path: Path) -> None:
    app = make_app(tmp_path)
    stopped: list[bool] = []
    app.assistant = type("A", (), {"stop": lambda self: stopped.append(True)})()
    info = app.audio_devices()
    assert info["input"]["selected"] is None and info["input"]["default"] == 1
    assert [d["index"] for d in info["output"]["options"]] == [2]

    app.set_audio_devices(1, 2)
    text = (tmp_path / "config.yaml").read_text(encoding="utf-8")
    assert "input_device: 1" in text and "output_device: 2" in text and "# null = Windows default mic" in text
    assert app.config.get("audio.input_device") == 1 and app.audio_devices()["input"]["selected"] == 1
    assert app._audio_changed and app._reload_session and stopped == [True]

    sd.broken.add(2)
    with pytest.raises(AudioError):
        app.set_audio_devices(None, 2)
    assert app.config.get("audio.input_device") == 1  # nothing saved on failure


def test_switching_audio_reopens_mic_and_speech(sd: FakeSD, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(audio_in, "MicRecorder", FakeRecorder)
    monkeypatch.setattr(app_module, "make_tts", lambda config, **_: FakeTTS(config.get("audio.output_device")))
    FakeRecorder.opened, FakeRecorder.fail = [], False
    app = make_app(tmp_path)
    app.window = Window()
    old_rec, old_tts = FakeRecorder(None), FakeTTS(None)
    app.recorder, app.tts = old_rec, old_tts
    app.sounds = type("S", (), {"output_device": None})()

    app.set_audio_devices(3, 2)
    app._switch_audio()
    assert old_rec.closed and app.recorder.device == 3 and app.recorder.listeners == [app._on_mic_frame]
    assert old_tts.down and app.tts.device == 2 and app.sounds.output_device == 2
    assert app.window.logs == ["Audio routing updated."] and not app._audio_changed

    FakeRecorder.fail = True  # a mic that won't open keeps the working one
    current = app.recorder
    app.set_audio_devices(1, None)
    app._switch_audio()
    assert app.recorder is current and not current.closed
    assert "kept the old one" in app.window.notes[-1]
