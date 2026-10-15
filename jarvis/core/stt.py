"""Speech-to-text with faster-whisper (runs locally)."""

from __future__ import annotations

import logging
import re
import time
from typing import Any

import numpy as np

log = logging.getLogger(__name__)

SAMPLE_RATE = 16_000

# Whisper's favourite inventions on silence or noise. Only dropped when they are
# the whole transcript.
_HALLUCINATIONS = {
    "you", "thanks for watching", "thank you for watching", "please subscribe",
    "subtitles by the amaraorg community", "bye", "uh", "um", "hmm", "so", "okay",
}
_TAGS = re.compile(r"\[[^\]]*\]|\([^)]*\)|\*[^*]*\*|♪+")


class STTError(RuntimeError):
    """Speech recognition failed; the message is meant for the user."""


def clean_transcript(text: str) -> str:
    """Remove sound tags like "[BLANK_AUDIO]" and drop noise-only transcripts."""
    text = _TAGS.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip()
    normalized = re.sub(r"[^a-z ]", "", text.lower()).strip()
    if not normalized or normalized in _HALLUCINATIONS:
        return ""
    return text


class SpeechToText:
    """Loads a Whisper model once and transcribes 16 kHz mono float32 audio.

    Args:
        model: Model name ("tiny.en", "base.en", "small.en", ...) or a local path.
        device: "auto" (CUDA if available, else CPU), "cuda" or "cpu".
        compute_type: "auto" picks float16 on GPU and int8 on CPU.
        beam_size: 1 is fastest; 5 is slightly more accurate.
        download_root: Where to cache models (default: Hugging Face cache).
        min_rms: Audio quieter than this is treated as silence.
    """

    def __init__(
        self,
        model: str = "small.en",
        device: str = "auto",
        compute_type: str = "auto",
        beam_size: int = 1,
        download_root: str | None = None,
        min_rms: float = 0.004,
    ) -> None:
        self.model_name = model
        self.beam_size = beam_size
        self.min_rms = min_rms
        self.language = "en" if model.endswith(".en") else None
        self.model, self.device = self._load(model, device, compute_type, download_root)

    @staticmethod
    def _cuda_available() -> bool:
        try:
            import ctranslate2

            return ctranslate2.get_cuda_device_count() > 0
        except Exception:  # noqa: BLE001
            return False

    def _load(self, model: str, device: str, compute_type: str, root: str | None) -> tuple[Any, str]:
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise STTError("faster-whisper is not installed: pip install -r requirements.txt") from exc

        candidates: list[tuple[str, str]] = []
        if device in ("auto", "cuda") and (device == "cuda" or self._cuda_available()):
            candidates.append(("cuda", "float16" if compute_type == "auto" else compute_type))
        if device != "cuda" or not candidates:
            candidates.append(("cpu", "int8" if compute_type == "auto" else compute_type))
        if device == "cuda":
            candidates.append(("cpu", "int8"))

        last_exc: Exception | None = None
        for dev, ctype in candidates:
            start = time.perf_counter()
            try:
                whisper = WhisperModel(model, device=dev, compute_type=ctype, download_root=root)
                # Warm-up: surfaces missing CUDA libraries now (not mid-conversation)
                # and makes the first real transcription fast.
                list(whisper.transcribe(np.zeros(SAMPLE_RATE // 2, dtype=np.float32), beam_size=1)[0])
            except Exception as exc:  # noqa: BLE001 - CUDA/cuDNN/download errors vary
                last_exc = exc
                log.warning("Whisper %s on %s/%s failed: %s", model, dev, ctype, exc)
                continue
            log.info("Whisper %s loaded on %s (%s) in %.1fs", model, dev, ctype, time.perf_counter() - start)
            return whisper, dev
        raise STTError(
            f"Could not load speech model '{model}': {last_exc}. "
            "Check your internet connection for the first download, or try stt.model: base.en"
        )

    def transcribe(self, audio: np.ndarray) -> str:
        """Transcribe 16 kHz mono float32 audio. Returns "" for silence or noise."""
        if audio.size < SAMPLE_RATE * 0.2:
            return ""
        rms = float(np.sqrt(np.mean(np.square(audio))))
        if rms < self.min_rms:
            log.info("Skipping transcription: too quiet (rms=%.4f)", rms)
            return ""
        start = time.perf_counter()
        try:
            segments, _info = self.model.transcribe(
                audio.astype(np.float32, copy=False),
                language=self.language,
                beam_size=self.beam_size,
                vad_filter=True,
                vad_parameters={"min_silence_duration_ms": 500},
                condition_on_previous_text=False,
                initial_prompt="Jarvis,",  # biases spelling of the assistant's name
            )
            kept = [
                s.text for s in segments if not (s.no_speech_prob > 0.6 and s.avg_logprob < -1.0)
            ]
        except Exception as exc:  # noqa: BLE001
            log.exception("Transcription failed")
            raise STTError("I couldn't process that audio.") from exc
        text = clean_transcript(" ".join(kept))
        log.info(
            "Transcribed %.1fs of audio in %.2fs: %r",
            audio.size / SAMPLE_RATE,
            time.perf_counter() - start,
            text,
        )
        return text


def create_stt(config: Any) -> SpeechToText:
    """Build the speech recogniser from the ``stt`` config section."""
    return SpeechToText(
        model=str(config.get("stt.model", "small.en")),
        device=str(config.get("stt.device", "auto")),
        compute_type=str(config.get("stt.compute_type", "auto")),
        beam_size=int(config.get("stt.beam_size", 1)),
        download_root=config.get("stt.download_root"),
        min_rms=float(config.get("stt.min_rms", 0.004)),
    )
