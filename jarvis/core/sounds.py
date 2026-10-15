"""Short UI sounds (beeps, chimes) generated with numpy - no asset files needed."""

from __future__ import annotations

import logging
from typing import Any

import numpy as np

log = logging.getLogger(__name__)

SAMPLE_RATE = 24_000


def tone(freqs: list[float], duration_s: float = 0.08, volume: float = 0.25) -> np.ndarray:
    """Build a soft tone (one or more frequencies played in sequence) as float32 samples."""
    parts = []
    for freq in freqs:
        t = np.arange(int(SAMPLE_RATE * duration_s)) / SAMPLE_RATE
        wave = np.sin(2 * np.pi * freq * t)
        fade = min(len(t) // 4, int(0.01 * SAMPLE_RATE))  # 10 ms fades avoid clicks
        envelope = np.ones_like(t)
        envelope[:fade] = np.linspace(0, 1, fade)
        envelope[-fade:] = np.linspace(1, 0, fade)
        parts.append(wave * envelope)
    return (np.concatenate(parts) * volume).astype(np.float32)


START_BEEP = tone([880.0])  # rising: "I'm listening"
STOP_BEEP = tone([660.0])   # lower: "got it"
CHIME = tone([660.0, 990.0], duration_s=0.07)  # wake-word chime (Phase 3)


class Sounds:
    """Plays UI sounds on the configured output device without blocking."""

    def __init__(self, output_device: Any = None, enabled: bool = True) -> None:
        self.output_device = output_device
        self.enabled = enabled

    def play(self, samples: np.ndarray) -> None:
        if not self.enabled:
            return
        try:
            import sounddevice as sd

            sd.play(samples, SAMPLE_RATE, device=self.output_device, blocking=False)
        except Exception as exc:  # noqa: BLE001 - a missing beep must never break the loop
            log.debug("Could not play UI sound: %s", exc)

    def start(self) -> None:
        self.play(START_BEEP)

    def stop(self) -> None:
        self.play(STOP_BEEP)

    def chime(self) -> None:
        self.play(CHIME)
