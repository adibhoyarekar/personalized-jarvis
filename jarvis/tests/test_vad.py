"""Tests for VAD endpointing, barge-in detection and follow-up exit phrases.

Audio is synthetic: silence, faint background noise, and loud amplitude-modulated
noise standing in for speech. The energy VAD classifies it, so the tests exercise
the endpointing logic itself without model files.
"""

from __future__ import annotations

import numpy as np
import pytest

from core.audio_in import VAD_WINDOW, BargeInDetector, EndpointConfig, Endpointer, EnergyVAD
from core.text_utils import is_exit_phrase, strip_wake_phrase

RATE = 16_000
rng = np.random.default_rng(0)


def silence(seconds: float) -> np.ndarray:
    return (rng.standard_normal(int(seconds * RATE)) * 0.001).astype(np.float32)


def speech(seconds: float) -> np.ndarray:
    """Loud noise with a 4 Hz syllable-like envelope that never fully drops out."""
    n = int(seconds * RATE)
    t = np.arange(n) / RATE
    envelope = 0.6 + 0.4 * np.abs(np.sin(2 * np.pi * 4 * t))
    return (rng.standard_normal(n) * 0.2 * envelope).astype(np.float32)


def run(audio: np.ndarray, config: EndpointConfig | None = None, start_timeout: float | None = None):  # type: ignore[no-untyped-def]
    """Feed audio window by window; return (result, seconds consumed)."""
    vad = EnergyVAD()
    ep = Endpointer(config or EndpointConfig())
    ep.reset(start_timeout)
    for i in range(0, len(audio) - VAD_WINDOW + 1, VAD_WINDOW):
        window = audio[i : i + VAD_WINDOW]
        result = ep.push(window, vad(window) >= 0.5)
        if result is not None:
            return result, (i + VAD_WINDOW) / RATE
    return None, len(audio) / RATE


def test_energy_vad_separates_speech_from_silence() -> None:
    vad = EnergyVAD()
    assert vad(silence(0.032)) == 0.0
    assert vad(speech(0.032)) == 1.0


def test_silence_only_times_out_as_no_speech() -> None:
    result, at = run(silence(8), start_timeout=5.0)
    assert result is not None and result.kind == "no_speech"
    assert at == pytest.approx(5.0, abs=0.05)


def test_speech_then_silence_ends_after_silence_timeout() -> None:
    audio = np.concatenate([silence(1.0), speech(2.0), silence(3.0)])
    result, at = run(audio)
    assert result is not None and result.kind == "speech"
    assert at == pytest.approx(3.0 + 1.0, abs=0.1)  # speech ends at 3.0 s, +1.0 s silence
    # preroll (0.5 s) + speech (2.0 s) + kept trailing silence (0.3 s)
    assert len(result.audio) / RATE == pytest.approx(2.8, abs=0.1)
    assert result.speech_s == pytest.approx(2.0, abs=0.1)


def test_preroll_keeps_audio_from_before_speech_onset() -> None:
    audio = np.concatenate([silence(1.0), speech(1.0), silence(2.0)])
    result, _ = run(audio, EndpointConfig(preroll=0.5, keep_silence=0.0))
    assert result is not None
    assert len(result.audio) / RATE == pytest.approx(1.5, abs=0.07)
    assert np.abs(result.audio[: int(0.4 * RATE)]).max() < 0.02  # starts with the quiet preroll


def test_short_pauses_do_not_end_the_utterance() -> None:
    audio = np.concatenate([speech(1.0), silence(0.6), speech(1.0), silence(1.5)])
    result, at = run(audio)
    assert result is not None and result.kind == "speech"
    assert result.speech_s == pytest.approx(2.0, abs=0.1)
    assert at == pytest.approx(2.6 + 1.0, abs=0.1)


def test_longer_silence_timeout_waits_longer() -> None:
    audio = np.concatenate([speech(1.0), silence(1.2), speech(1.0), silence(2.0)])
    short, _ = run(audio, EndpointConfig(silence_timeout=1.0))
    long, _ = run(audio, EndpointConfig(silence_timeout=1.5))
    assert short is not None and short.speech_s == pytest.approx(1.0, abs=0.1)
    assert long is not None and long.speech_s == pytest.approx(2.0, abs=0.1)


def test_max_utterance_caps_recording() -> None:
    result, at = run(speech(20), EndpointConfig(max_utterance=4.0))
    assert result is not None and result.kind == "speech" and result.reason == "max length reached"
    assert at == pytest.approx(4.0, abs=0.1)


def test_click_shorter_than_min_speech_is_ignored() -> None:
    audio = np.concatenate([silence(0.5), speech(0.1), silence(1.5), speech(1.0), silence(1.5)])
    result, _ = run(audio, EndpointConfig(min_speech=0.3), start_timeout=10)
    assert result is not None and result.kind == "speech"
    assert result.speech_s == pytest.approx(1.0, abs=0.1)  # the click didn't count


def test_only_clicks_end_as_no_speech() -> None:
    audio = np.concatenate([speech(0.1), silence(1.5)] * 4)
    result, _ = run(audio, EndpointConfig(min_speech=0.3), start_timeout=3.0)
    assert result is not None and result.kind == "no_speech"


def test_seeded_capture_starts_in_speech() -> None:
    ep = Endpointer(EndpointConfig())
    ep.reset(seed=[speech(0.5)])
    assert ep.in_speech
    vad = EnergyVAD()
    result = None
    audio = silence(1.5)
    for i in range(0, len(audio) - VAD_WINDOW + 1, VAD_WINDOW):
        window = audio[i : i + VAD_WINDOW]
        result = result or ep.push(window, vad(window) >= 0.5)
    assert result is not None and result.kind == "speech"


def test_barge_in_needs_sustained_speech() -> None:
    def feed(audio: np.ndarray, det: BargeInDetector) -> bool:
        vad = EnergyVAD()
        fired = False
        for i in range(0, len(audio) - VAD_WINDOW + 1, VAD_WINDOW):
            window = audio[i : i + VAD_WINDOW]
            fired = det.push(window, vad(window) >= 0.5) or fired
        return fired

    assert not feed(np.concatenate([speech(0.2), silence(0.5), speech(0.2)]), BargeInDetector(0.4))
    assert feed(np.concatenate([silence(0.5), speech(0.6)]), BargeInDetector(0.4))
    assert not feed(silence(3.0), BargeInDetector(0.4))


@pytest.mark.parametrize(
    "text", ["Thanks!", "Thank you, Jarvis.", "That's all.", "That's all for now.", "Stop listening.",
             "No thanks", "Okay, thanks", "Nope.", "Goodbye", "never mind"]
)
def test_exit_phrases(text: str) -> None:
    assert is_exit_phrase(text)


@pytest.mark.parametrize(
    "text", ["Thanks, now open Spotify", "What time is it?", "Stop the music", "Thank you for opening "
             "Chrome, can you also check the weather?", "", "Set a timer for ten minutes"]
)
def test_requests_are_not_exit_phrases(text: str) -> None:
    assert not is_exit_phrase(text)


def test_strip_wake_phrase() -> None:
    assert strip_wake_phrase("Hey Jarvis, what time is it?") == "What time is it?"
    assert strip_wake_phrase("jarvis open chrome") == "Open chrome"
    assert strip_wake_phrase("Hey Jarvis.") == ""
    assert strip_wake_phrase("Who is Jarvis?") == "Who is Jarvis?"
