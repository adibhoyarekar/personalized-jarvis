"""Tests for the TTS pipeline and push-to-talk loop, using fake audio devices."""

from __future__ import annotations

import io
import sys
import threading
import time
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest

from core.agent import Reply
from core.tts import SpeechStream, TTSEngine, decode_audio
from core.voice import VoiceAssistant


class FakeOutputStream:
    """Stands in for sounddevice.OutputStream; records what was played."""

    played: list[float] = []  # first sample of each write -> identifies the sentence
    write_delay = 0.0

    def __init__(self, samplerate: int, **_: Any) -> None:
        self.samplerate = samplerate

    def start(self) -> None: ...
    def close(self) -> None: ...
    def abort(self) -> None: ...

    def write(self, data: np.ndarray) -> None:
        FakeOutputStream.played.append(float(data[0, 0]))
        time.sleep(FakeOutputStream.write_delay)


@pytest.fixture
def fake_sd(monkeypatch: pytest.MonkeyPatch) -> type[FakeOutputStream]:
    FakeOutputStream.played = []
    FakeOutputStream.write_delay = 0.0
    module = SimpleNamespace(OutputStream=FakeOutputStream, query_devices=lambda *a, **k: {"default_samplerate": 48000})
    monkeypatch.setitem(sys.modules, "sounddevice", module)
    return FakeOutputStream


class ToneEngine(TTSEngine):
    """Each sentence becomes 0.2 s of a constant value = its index, so order is visible."""

    name = "tone"

    def __init__(self, fail_on: str | None = None) -> None:
        self.errors: list[str] = []
        super().__init__(on_error=lambda text, exc: self.errors.append(text))
        self.spoken: list[str] = []
        self.fail_on = fail_on

    def synthesize(self, text: str) -> tuple[np.ndarray, int]:
        if text == self.fail_on:
            raise RuntimeError("synth failed")
        self.spoken.append(text)
        return np.full(4800, float(len(self.spoken)), dtype=np.float32), 24000


def test_tts_plays_sentences_in_order(fake_sd: type[FakeOutputStream]) -> None:
    engine = ToneEngine()
    for sentence in ["One.", "**Two.**", "Three."]:
        engine.speak(sentence)
    assert engine.is_speaking
    assert engine.wait(timeout=5)
    assert not engine.is_speaking
    assert engine.spoken == ["One.", "Two.", "Three."]  # markdown stripped
    values = [v for i, v in enumerate(fake_sd.played) if i == 0 or v != fake_sd.played[i - 1]]
    assert values == [1.0, 2.0, 3.0]
    engine.shutdown()


def test_tts_stop_interrupts_immediately(fake_sd: type[FakeOutputStream]) -> None:
    fake_sd.write_delay = 0.02
    engine = ToneEngine()
    engine.speak("A fairly long first sentence.")
    engine.speak("A second sentence.")
    time.sleep(0.1)
    start = time.perf_counter()
    engine.stop()
    assert not engine.is_speaking
    assert time.perf_counter() - start < 0.1
    played_at_stop = len(fake_sd.played)
    time.sleep(0.2)
    assert len(fake_sd.played) <= played_at_stop + 1  # at most the chunk in flight
    engine.shutdown()


def test_tts_failure_reported_and_queue_continues(fake_sd: type[FakeOutputStream]) -> None:
    engine = ToneEngine(fail_on="Bad one.")
    engine.speak("Bad one.")
    engine.speak("Good one.")
    assert engine.wait(timeout=5)
    assert engine.errors == ["Bad one."]
    assert engine.spoken == ["Good one."]
    engine.shutdown()


def test_speech_stream_feeds_whole_sentences(fake_sd: type[FakeOutputStream]) -> None:
    engine = ToneEngine()
    stream = SpeechStream(engine)
    for chunk in ["Opening Chr", "ome now. It sh", "ould be up in a sec", "ond."]:
        stream.feed(chunk)
    stream.finish()
    assert engine.wait(timeout=5)
    assert engine.spoken == ["Opening Chrome now.", "It should be up in a second."]
    engine.shutdown()


def test_decode_mp3_without_ffmpeg() -> None:
    sf = pytest.importorskip("soundfile")
    if "MP3" not in sf.available_formats():
        pytest.skip("libsndfile without MP3 support")
    tone = (0.3 * np.sin(np.linspace(0, 2 * np.pi * 440, 24000))).astype(np.float32)
    buf = io.BytesIO()
    sf.write(buf, tone, 24000, format="MP3")
    samples, rate = decode_audio(buf.getvalue())
    assert rate == 24000 and samples.ndim == 1 and abs(len(samples) - 24000) < 3000


# --- push-to-talk loop ---------------------------------------------------------


class FakeRecorder:
    def __init__(self) -> None:
        self.recording = False
        self.closed = False

    def start_recording(self) -> None:
        self.recording = True

    def stop_recording(self) -> np.ndarray:
        self.recording = False
        return np.ones(16000, dtype=np.float32)

    def close(self) -> None:
        self.closed = True


class FakeSTT:
    def __init__(self, texts: list[str]) -> None:
        self.texts = texts

    def transcribe(self, audio: np.ndarray) -> str:
        return self.texts.pop(0)


class FakeTTS:
    def __init__(self) -> None:
        self.spoken: list[str] = []
        self.speaking = False
        self.stops = 0

    def speak(self, text: str) -> None:
        self.spoken.append(text)

    def stop(self) -> None:
        self.stops += 1
        self.speaking = False

    @property
    def is_speaking(self) -> bool:
        return self.speaking

    def wait(self, timeout: float | None = None) -> bool:
        return True

    def shutdown(self) -> None: ...


class FakeAgent:
    name = "Jarvis"

    def __init__(self) -> None:
        self.cancelled = threading.Event()
        self.asked: list[str] = []

    def cancel(self) -> None:
        self.cancelled.set()

    def ask(self, text: str, on_text: Any = None, on_status: Any = None) -> Reply:
        self.asked.append(text)
        on_text("CPU is at twelve percent. All quiet.")
        return Reply("CPU is at twelve percent. All quiet.")


def make_assistant(texts: list[str]) -> tuple[VoiceAssistant, FakeAgent, FakeTTS, FakeRecorder]:
    agent, tts, rec = FakeAgent(), FakeTTS(), FakeRecorder()
    assistant = VoiceAssistant(
        agent, rec, FakeSTT(texts), tts, print_fn=lambda *a, **k: None  # type: ignore[arg-type]
    )
    return assistant, agent, tts, rec


def test_press_release_queues_audio_and_short_taps_are_ignored() -> None:
    assistant, _agent, _tts, rec = make_assistant([])
    assistant._on_press()
    assert rec.recording
    assistant._on_release(0.1)
    assert assistant.utterances.empty()
    assistant._on_press()
    assistant._on_release(1.2)
    assert assistant.utterances.qsize() == 1


def test_handle_speaks_reply_sentence_by_sentence() -> None:
    assistant, agent, tts, _rec = make_assistant([])
    assistant.handle("what's my CPU usage?")
    assert agent.asked == ["what's my CPU usage?"]
    assert tts.spoken == ["CPU is at twelve percent.", "All quiet."]


def test_barge_in_stops_speech_and_cancels_agent() -> None:
    assistant, agent, tts, rec = make_assistant([])
    tts.speaking = True
    assistant._busy.set()
    assistant._on_press()
    assert tts.stops == 1 and agent.cancelled.is_set() and rec.recording


def test_spoken_confirmation() -> None:
    assistant, agent, tts, _rec = make_assistant(["Yes, go ahead."])
    assistant._busy.set()  # the agent is mid-reply while it asks
    assistant.utterances.put(np.ones(16000, dtype=np.float32))  # stale audio: discarded

    def answer() -> None:  # the user holds the hotkey and answers
        assistant._on_press()
        assistant._on_release(1.0)

    threading.Timer(0.05, answer).start()
    assert assistant.confirm("Should I close Chrome?", timeout_s=2)
    assert tts.spoken[0] == "Should I close Chrome?"
    assert not agent.cancelled.is_set()  # answering must not cancel the agent


def test_confirmation_times_out_as_no() -> None:
    assistant, _agent, _tts, _rec = make_assistant([])
    assert not assistant.confirm("Should I shut down?", timeout_s=0.05)
