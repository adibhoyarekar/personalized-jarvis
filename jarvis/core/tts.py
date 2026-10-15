"""Text-to-speech with pluggable engines and an interruptible playback pipeline.

Engines only implement :meth:`TTSEngine.synthesize` (text -> samples). The base
class runs two background threads: one synthesises queued sentences, the other
plays them in order. While sentence N plays, sentence N+1 is already being
synthesised (prefetch), so multi-sentence replies flow without gaps.
:meth:`stop` cuts playback off within ~40 ms, drops everything queued and
abandons any synthesis request still in flight.
"""

from __future__ import annotations

import abc
import asyncio
import io
import logging
import queue
import threading
from typing import Any, Callable

import numpy as np

from core.text_utils import SentenceSplitter, strip_markdown

log = logging.getLogger(__name__)

ErrorCallback = Callable[[str, Exception], None]

_CHUNK_S = 0.04  # playback granularity; also the worst-case stop() latency


class TTSError(RuntimeError):
    """Speech synthesis/playback failed."""


class SynthesisCancelled(Exception):
    """Raised inside :meth:`TTSEngine.synthesize` when the request became stale."""


class TTSEngine(abc.ABC):
    """Base class: queueing, background synthesis and interruptible playback.

    Args:
        output_device: sounddevice output index (None = system default).
        on_error: Called with (sentence, exception) when a sentence can't be spoken,
            so the caller can fall back to printing.
    """

    name = "base"

    def __init__(self, output_device: int | None = None, on_error: ErrorCallback | None = None) -> None:
        self.output_device = output_device
        self.on_error = on_error
        self._text_q: queue.Queue[tuple[int, str] | None] = queue.Queue()
        self._audio_q: queue.Queue[tuple[int, np.ndarray, int] | None] = queue.Queue(maxsize=4)
        self._generation = 0
        self._pending = 0
        self._cond = threading.Condition()
        self._threads: list[threading.Thread] = []
        self._closed = False
        self._synth_gen = -1
        self._playing = False
        #: Called with True when audio starts playing and False when all queued
        #: speech has finished (or was stopped). Used for the SPEAKING state.
        self.on_activity: Callable[[bool], None] | None = None

    # -- to implement --------------------------------------------------------

    @abc.abstractmethod
    def synthesize(self, text: str) -> tuple[np.ndarray, int]:
        """Return (mono float32 samples, sample_rate) for ``text``.

        Long-running engines should check :meth:`synthesis_cancelled` and raise
        :class:`SynthesisCancelled` so a barge-in doesn't wait for stale audio.
        """

    def synthesis_cancelled(self) -> bool:
        """True if the sentence being synthesised was cancelled by :meth:`stop`."""
        return self._synth_gen != self._generation

    # -- public API ----------------------------------------------------------

    def speak(self, text: str) -> None:
        """Queue text to be spoken (non-blocking). Markdown is stripped first."""
        clean = strip_markdown(text)
        if not clean or self._closed:
            return
        self._ensure_threads()
        with self._cond:
            self._pending += 1
            gen = self._generation
        self._text_q.put((gen, clean))

    def stop(self) -> None:
        """Stop speaking immediately and discard anything queued."""
        with self._cond:
            self._generation += 1
            self._pending = 0
            self._cond.notify_all()
        for q in (self._text_q, self._audio_q):
            try:
                while True:
                    q.get_nowait()
            except queue.Empty:
                pass
        self._set_playing(False)

    @property
    def is_speaking(self) -> bool:
        """True while anything is queued, being synthesised, or playing."""
        return self._pending > 0

    def wait(self, timeout: float | None = None) -> bool:
        """Block until everything queued has been spoken. Returns False on timeout."""
        with self._cond:
            return self._cond.wait_for(lambda: self._pending == 0, timeout)

    def shutdown(self) -> None:
        """Stop speaking and end the worker threads."""
        self.stop()
        self._closed = True
        self._text_q.put(None)
        try:
            self._audio_q.put_nowait(None)
        except queue.Full:
            pass

    # -- internals -----------------------------------------------------------

    def _ensure_threads(self) -> None:
        if self._threads:
            return
        self._threads = [
            threading.Thread(target=self._synth_loop, name="tts-synth", daemon=True),
            threading.Thread(target=self._play_loop, name="tts-play", daemon=True),
        ]
        for t in self._threads:
            t.start()

    def _is_current(self, gen: int) -> bool:
        return gen == self._generation

    def _done_one(self, gen: int) -> None:
        with self._cond:
            if gen == self._generation and self._pending > 0:
                self._pending -= 1
            idle = self._pending == 0
            self._cond.notify_all()
        if idle:
            self._set_playing(False)

    def _set_playing(self, playing: bool) -> None:
        if playing == self._playing:
            return
        self._playing = playing
        if self.on_activity is not None:
            try:
                self.on_activity(playing)
            except Exception:  # noqa: BLE001
                log.exception("TTS activity callback failed")

    def _report(self, text: str, exc: Exception) -> None:
        log.warning("%s TTS failed for %r: %s", self.name, text[:60], exc)
        if self.on_error:
            try:
                self.on_error(text, exc)
            except Exception:  # noqa: BLE001
                log.exception("TTS error callback failed")

    def _synth_loop(self) -> None:
        while True:
            item = self._text_q.get()
            if item is None:
                return
            gen, text = item
            if not self._is_current(gen):
                continue
            self._synth_gen = gen
            try:
                samples, rate = self.synthesize(text)
            except SynthesisCancelled:
                log.debug("Synthesis cancelled: %r", text[:40])
                continue
            except Exception as exc:  # noqa: BLE001
                self._report(text, exc)
                self._done_one(gen)
                continue
            if self._is_current(gen):
                self._audio_q.put((gen, samples, rate))

    def _play_loop(self) -> None:
        try:
            import sounddevice as sd
        except Exception as exc:  # noqa: BLE001
            log.error("Audio output unavailable: %s", exc)
            sd = None
        stream: Any = None
        stream_rate = 0  # rate of the audio we feed it
        device_rate = 0  # rate the device was actually opened at
        try:
            while True:
                try:
                    item = self._audio_q.get(timeout=0.5)
                except queue.Empty:
                    if stream is not None:  # idle: release the device
                        stream.close()
                        stream = None
                    continue
                if item is None:
                    return
                gen, samples, rate = item
                if not self._is_current(gen):
                    continue
                if sd is None:
                    self._report("(audio)", TTSError("no audio output device"))
                    self._done_one(gen)
                    continue
                try:
                    if stream is None or stream_rate != rate:
                        if stream is not None:
                            stream.close()
                        stream, device_rate = self._open_stream(sd, rate)
                        stream_rate = rate
                    if device_rate != rate:
                        samples = _resample(samples, rate, device_rate)
                    if self._is_current(gen):
                        self._set_playing(True)
                    if not self._write(stream, samples, device_rate, gen):
                        stream.abort()  # interrupted: drop buffered audio now
                        stream.close()
                        stream = None
                except Exception as exc:  # noqa: BLE001
                    self._report("(playback)", exc)
                    stream = None
                self._done_one(gen)
        finally:
            if stream is not None:
                stream.close()

    def _open_stream(self, sd: Any, rate: int) -> tuple[Any, int]:
        """Open an output stream at ``rate``, or at the device's native rate."""
        try:
            stream = sd.OutputStream(samplerate=rate, channels=1, dtype="float32", device=self.output_device)
            stream.start()
            return stream, rate
        except Exception:  # noqa: BLE001 - device doesn't support this rate
            native = int(sd.query_devices(self.output_device, kind="output")["default_samplerate"])
            stream = sd.OutputStream(samplerate=native, channels=1, dtype="float32", device=self.output_device)
            stream.start()
            return stream, native

    def _write(self, stream: Any, samples: np.ndarray, rate: int, gen: int) -> bool:
        """Write in small chunks so stop() takes effect quickly. False if interrupted."""
        step = max(1, int(rate * _CHUNK_S))
        data = samples.reshape(-1, 1).astype(np.float32, copy=False)
        for i in range(0, len(data), step):
            if not self._is_current(gen):
                return False
            stream.write(data[i : i + step])
        return True


def _resample(audio: np.ndarray, src: int, dst: int) -> np.ndarray:
    from core.audio_in import resample

    return resample(audio, src, dst)


def decode_audio(data: bytes) -> tuple[np.ndarray, int]:
    """Decode MP3/WAV/OGG bytes to mono float32 using soundfile (no ffmpeg needed)."""
    import soundfile as sf

    try:
        samples, rate = sf.read(io.BytesIO(data), dtype="float32", always_2d=True)
    except Exception as exc:  # noqa: BLE001 - soundfile raises LibsndfileError/RuntimeError
        raise TTSError(
            f"Could not decode audio ({exc}). MP3 needs soundfile>=0.12: pip install -U soundfile"
        ) from exc
    return samples.mean(axis=1).astype(np.float32), int(rate)


# ---------------------------------------------------------------------------
# Engines
# ---------------------------------------------------------------------------


class EdgeTTSEngine(TTSEngine):
    """Microsoft Edge online neural voices via the ``edge-tts`` package (free, needs internet).

    Voices: ``edge-tts --list-voices`` (e.g. en-GB-RyanNeural, en-US-GuyNeural).
    """

    name = "edge"

    def __init__(
        self,
        voice: str = "en-GB-RyanNeural",
        rate: str = "+0%",
        volume: str = "+0%",
        output_device: int | None = None,
        on_error: ErrorCallback | None = None,
    ) -> None:
        super().__init__(output_device, on_error)
        try:
            import edge_tts  # noqa: F401
        except ImportError as exc:
            raise TTSError("edge-tts is not installed: pip install -r requirements.txt") from exc
        self.voice, self.rate, self.volume = voice, rate, volume
        self._loop: asyncio.AbstractEventLoop | None = None

    async def _fetch(self, text: str) -> bytes:
        import edge_tts

        communicate = edge_tts.Communicate(text, self.voice, rate=self.rate, volume=self.volume)
        audio = bytearray()
        async for chunk in communicate.stream():
            if self.synthesis_cancelled():
                raise SynthesisCancelled
            if chunk.get("type") == "audio":
                audio.extend(chunk["data"])
        return bytes(audio)

    def synthesize(self, text: str) -> tuple[np.ndarray, int]:
        if self._loop is None:  # one event loop, owned by the synth thread
            self._loop = asyncio.new_event_loop()
        try:
            mp3 = self._loop.run_until_complete(asyncio.wait_for(self._fetch(text), timeout=15))
        except SynthesisCancelled:
            raise
        except Exception as exc:  # noqa: BLE001 - aiohttp/edge_tts errors vary
            raise TTSError(f"edge-tts request failed (is the internet up?): {exc}") from exc
        if not mp3:
            raise TTSError("edge-tts returned no audio (check tts.voice in config.yaml)")
        return decode_audio(mp3)


class PiperTTSEngine(TTSEngine):
    """Local, offline Piper voices. Not implemented yet."""

    name = "piper"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        raise NotImplementedError("The Piper engine isn't implemented yet; set tts.engine: edge")

    def synthesize(self, text: str) -> tuple[np.ndarray, int]:  # pragma: no cover
        raise NotImplementedError


class ElevenLabsTTSEngine(TTSEngine):
    """ElevenLabs cloud voices. Not implemented yet."""

    name = "elevenlabs"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        raise NotImplementedError("The ElevenLabs engine isn't implemented yet; set tts.engine: edge")

    def synthesize(self, text: str) -> tuple[np.ndarray, int]:  # pragma: no cover
        raise NotImplementedError


ENGINES: dict[str, type[TTSEngine]] = {
    "edge": EdgeTTSEngine,
    "piper": PiperTTSEngine,
    "elevenlabs": ElevenLabsTTSEngine,
}


def create_tts(config: Any, output_device: int | None = None, on_error: ErrorCallback | None = None) -> TTSEngine:
    """Build the engine named by ``tts.engine`` in config.

    Raises:
        TTSError: Unknown engine or missing dependency.
        NotImplementedError: Engine is still a stub.
    """
    engine = str(config.get("tts.engine", "edge")).lower()
    if engine not in ENGINES:
        raise TTSError(f"Unknown tts.engine '{engine}'. Choose one of: {', '.join(ENGINES)}")
    if engine == "edge":
        return EdgeTTSEngine(
            voice=str(config.get("tts.voice", "en-GB-RyanNeural")),
            rate=str(config.get("tts.rate", "+0%")),
            volume=str(config.get("tts.volume", "+0%")),
            output_device=output_device,
            on_error=on_error,
        )
    return ENGINES[engine](output_device=output_device, on_error=on_error)


class SpeechStream:
    """Feeds streamed reply text to a TTS engine one sentence at a time.

    Example:
        stream = SpeechStream(engine)
        agent.ask(text, on_text=stream.feed)
        stream.finish()
    """

    def __init__(self, engine: TTSEngine | None, min_chars: int = 8) -> None:
        self.engine = engine
        self._splitter = SentenceSplitter(min_chars)

    def feed(self, chunk: str) -> None:
        for sentence in self._splitter.feed(chunk):
            if self.engine is not None:
                self.engine.speak(sentence)

    def finish(self) -> None:
        """Speak whatever is left once the reply is complete."""
        tail = self._splitter.flush()
        if tail and self.engine is not None:
            self.engine.speak(tail)
