"""Microphone capture, devices, push-to-talk, wake word, VAD and the audio pipeline.

The mic stream stays open while Jarvis runs and keeps a short pre-roll buffer,
so the first syllable after pressing the hotkey isn't lost. Audio is only
collected between :meth:`MicRecorder.start_recording` and
:meth:`MicRecorder.stop_recording`.
"""

from __future__ import annotations

import collections
import enum
import logging
import queue
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import numpy as np

log = logging.getLogger(__name__)

TARGET_RATE = 16_000  # what Whisper expects


class AudioError(RuntimeError):
    """Microphone/speaker problems, with a message meant for the user."""


def _sd() -> Any:
    """Import sounddevice lazily so text mode and tests work without PortAudio."""
    try:
        import sounddevice as sd
    except OSError as exc:  # PortAudio missing (Linux without libportaudio2)
        raise AudioError(f"Audio system unavailable: {exc}. On Linux: sudo apt install libportaudio2") from exc
    except ImportError as exc:
        raise AudioError("sounddevice is not installed: pip install -r requirements.txt") from exc
    return sd


def list_devices() -> str:
    """Return a readable table of audio devices with their indexes."""
    sd = _sd()
    devices = sd.query_devices()
    hostapis = sd.query_hostapis()
    try:
        default_in, default_out = sd.default.device
    except Exception:  # noqa: BLE001
        default_in = default_out = -1
    lines = ["Index  In Out  Host API     Name", "-----  -- ---  -----------  ----"]
    for idx, dev in enumerate(devices):
        marks = ("<- default input " if idx == default_in else "") + (
            "<- default output" if idx == default_out else ""
        )
        api = hostapis[dev["hostapi"]]["name"].replace("Windows ", "")[:11]
        lines.append(
            f"{idx:>5}  {dev['max_input_channels']:>2} {dev['max_output_channels']:>3}  "
            f"{api:<11}  {dev['name']} {marks}".rstrip()
        )
    if len(devices) == 0:
        lines.append("  (no audio devices found - is a mic or headset connected?)")
    lines.append("")
    lines.append("Set audio.input_device / audio.output_device in config.yaml to an index or part of a name.")
    return "\n".join(lines)


def resolve_device(spec: Any, kind: str) -> int | None:
    """Turn a config value (None, index, or name fragment) into a device index.

    Args:
        spec: ``None`` for the system default, an int index, or a name substring.
        kind: ``"input"`` or ``"output"``.

    Raises:
        AudioError: If no matching device exists.
    """
    sd = _sd()
    channels_key = "max_input_channels" if kind == "input" else "max_output_channels"
    devices = sd.query_devices()
    if spec is None or spec == "":
        try:
            sd.query_devices(kind=kind)
        except Exception as exc:  # noqa: BLE001 - PortAudioError/ValueError
            if kind == "input":
                hint = "check Settings > Privacy & security > Microphone"
                noun = "microphone"
            else:
                hint = "check Settings > System > Sound > Output"
                noun = "speaker or headphones"
            raise AudioError(
                f"No {noun} found. Plug one in, {hint}, or run: python main.py --list-devices"
            ) from exc
        return None
    if isinstance(spec, int) or (isinstance(spec, str) and spec.strip().isdigit()):
        idx = int(spec)
        if 0 <= idx < len(devices) and devices[idx][channels_key] > 0:
            return idx
        raise AudioError(f"Audio device {idx} is not a valid {kind} device. Run: python main.py --list-devices")
    needle = str(spec).lower()
    for idx, dev in enumerate(devices):
        if needle in dev["name"].lower() and dev[channels_key] > 0:
            return idx
    raise AudioError(f"No {kind} device matching '{spec}'. Run: python main.py --list-devices")


def resample(audio: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
    """Linear-interpolation resampling; good enough for speech recognition."""
    if src_rate == dst_rate or audio.size == 0:
        return audio.astype(np.float32, copy=False)
    duration = audio.shape[0] / src_rate
    n_out = int(round(duration * dst_rate))
    x_old = np.linspace(0.0, duration, num=audio.shape[0], endpoint=False)
    x_new = np.linspace(0.0, duration, num=n_out, endpoint=False)
    return np.interp(x_new, x_old, audio).astype(np.float32)


FrameListener = Callable[[np.ndarray], None]


class MicRecorder:
    """Always-open mono microphone stream with on-demand recording.

    Args:
        device: Config value for the input device (None, index or name).
        sample_rate: Rate delivered to callers (16 kHz for Whisper).
        preroll_s: Audio kept from just before recording starts.
    """

    def __init__(self, device: Any = None, sample_rate: int = TARGET_RATE, preroll_s: float = 0.25) -> None:
        self.sample_rate = sample_rate
        self.device = resolve_device(device, "input")
        self._sd = _sd()
        self._lock = threading.Lock()
        self._recording = False
        self._chunks: list[np.ndarray] = []
        self._listeners: list[FrameListener] = []
        self._stream: Any = None
        self._stream_rate = sample_rate
        self._preroll: collections.deque[np.ndarray] = collections.deque()
        self._preroll_frames = 0
        self._preroll_max = int(preroll_s * sample_rate)

    # -- lifecycle ---------------------------------------------------------

    def open(self) -> None:
        """Open the microphone. Falls back to the device's native rate if needed."""
        if self._stream is not None:
            return
        info = self._sd.query_devices(self.device, kind="input")
        rates = [self.sample_rate, int(info["default_samplerate"])]
        last_exc: Exception | None = None
        for rate in dict.fromkeys(rates):
            try:
                stream = self._sd.InputStream(
                    device=self.device,
                    channels=1,
                    samplerate=rate,
                    dtype="float32",
                    blocksize=int(rate * 0.03),  # 30 ms frames
                    callback=self._callback,
                )
                stream.start()
            except Exception as exc:  # noqa: BLE001 - PortAudioError varies
                last_exc = exc
                log.info("Mic rejected %s Hz: %s", rate, exc)
                continue
            self._stream, self._stream_rate = stream, rate
            self._preroll_max = int(self._preroll_max * rate / self.sample_rate)
            log.info("Microphone open: %s at %s Hz", info["name"], rate)
            return
        raise AudioError(f"Could not open the microphone '{info['name']}': {last_exc}")

    def close(self) -> None:
        """Stop and release the microphone."""
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:  # noqa: BLE001
                log.debug("Error closing mic stream", exc_info=True)
            log.info("Microphone closed")

    def __enter__(self) -> "MicRecorder":
        self.open()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- recording ---------------------------------------------------------

    @property
    def is_recording(self) -> bool:
        return self._recording

    def start_recording(self) -> None:
        """Begin collecting audio (including the pre-roll buffer)."""
        with self._lock:
            if self._recording:
                return
            self._chunks = list(self._preroll)
            self._recording = True

    def stop_recording(self) -> np.ndarray:
        """Stop collecting and return the audio as float32 mono at ``sample_rate``."""
        with self._lock:
            self._recording = False
            chunks, self._chunks = self._chunks, []
        if not chunks:
            return np.zeros(0, dtype=np.float32)
        audio = np.concatenate(chunks)
        return resample(audio, self._stream_rate, self.sample_rate)

    def add_listener(self, listener: FrameListener) -> None:
        """Receive every mic frame (at ``sample_rate``), e.g. for wake-word detection."""
        self._listeners.append(listener)

    def remove_listener(self, listener: FrameListener) -> None:
        """Stop sending frames to ``listener`` (no-op if it isn't registered)."""
        if listener in self._listeners:
            self._listeners.remove(listener)

    def _callback(self, indata: np.ndarray, frames: int, time_info: Any, status: Any) -> None:
        if status:
            log.debug("Mic status: %s", status)
        mono = indata[:, 0].copy()
        with self._lock:
            if self._recording:
                self._chunks.append(mono)
            self._preroll.append(mono)
            self._preroll_frames += len(mono)
            while self._preroll and self._preroll_frames - len(self._preroll[0]) >= self._preroll_max:
                self._preroll_frames -= len(self._preroll.popleft())
        if self._listeners:
            frame = resample(mono, self._stream_rate, self.sample_rate)
            for listener in self._listeners:
                try:
                    listener(frame)
                except Exception:  # noqa: BLE001
                    log.exception("Mic listener failed")


# ---------------------------------------------------------------------------
# Push-to-talk hotkey
# ---------------------------------------------------------------------------


class HotkeyError(RuntimeError):
    """The hotkey could not be registered."""


class PushToTalk:
    """Calls ``on_press`` when the hotkey goes down and ``on_release`` when it comes up.

    Key auto-repeat while held is filtered out, so each physical press fires once.

    Args:
        hotkey: Key name, e.g. "f9", "right ctrl", "caps lock".
        on_press: Called (from the hook thread) when the key is pressed.
        on_release: Called when the key is released, with seconds held.
        backend: "keyboard", "pynput", or "auto" (keyboard on Windows, else pynput).
    """

    def __init__(
        self,
        hotkey: str,
        on_press: Callable[[], None],
        on_release: Callable[[float], None],
        backend: str = "auto",
    ) -> None:
        self.hotkey = hotkey.strip().lower()
        self.on_press = on_press
        self.on_release = on_release
        self.backend = backend if backend != "auto" else ("keyboard" if sys.platform == "win32" else "pynput")
        self._down_at: float | None = None
        self._handles: list[Any] = []
        self._listener: Any = None

    def _press(self) -> None:
        if self._down_at is not None:
            return  # auto-repeat
        self._down_at = time.monotonic()
        try:
            self.on_press()
        except Exception:  # noqa: BLE001
            log.exception("Push-to-talk press handler failed")

    def _release(self) -> None:
        if self._down_at is None:
            return
        held, self._down_at = time.monotonic() - self._down_at, None
        try:
            self.on_release(held)
        except Exception:  # noqa: BLE001
            log.exception("Push-to-talk release handler failed")

    def start(self) -> None:
        """Install the keyboard hook."""
        if self.backend == "keyboard":
            try:
                self._start_keyboard()
                return
            except HotkeyError:
                raise
            except Exception as exc:  # noqa: BLE001 - fall back to pynput
                log.warning("keyboard hook failed (%s); trying pynput", exc)
                self.backend = "pynput"
        self._start_pynput()

    def _start_keyboard(self) -> None:
        import keyboard

        try:
            keyboard.key_to_scan_codes(self.hotkey)
        except ValueError as exc:
            raise HotkeyError(f"Unknown hotkey '{self.hotkey}' in config.yaml") from exc
        self._handles = [
            keyboard.on_press_key(self.hotkey, lambda _e: self._press(), suppress=False),
            keyboard.on_release_key(self.hotkey, lambda _e: self._release(), suppress=False),
        ]
        log.info("Push-to-talk hotkey '%s' registered (keyboard)", self.hotkey)

    def _start_pynput(self) -> None:
        try:
            from pynput import keyboard as pk
        except Exception as exc:  # noqa: BLE001 - ImportError or no display server
            raise HotkeyError(f"No keyboard hook available: {exc}") from exc

        target = self._pynput_key(pk)

        def matches(key: Any) -> bool:
            if key == target:
                return True
            char = getattr(key, "char", None)
            return isinstance(target, pk.KeyCode) and char is not None and char.lower() == target.char

        self._listener = pk.Listener(
            on_press=lambda k: self._press() if matches(k) else None,
            on_release=lambda k: self._release() if matches(k) else None,
        )
        self._listener.daemon = True
        self._listener.start()
        log.info("Push-to-talk hotkey '%s' registered (pynput)", self.hotkey)

    def _pynput_key(self, pk: Any) -> Any:
        name = self.hotkey.replace(" ", "_")
        aliases = {"right_ctrl": "ctrl_r", "left_ctrl": "ctrl_l", "right_alt": "alt_r",
                   "left_alt": "alt_l", "right_shift": "shift_r", "left_shift": "shift_l",
                   "capslock": "caps_lock", "scrolllock": "scroll_lock", "pause_break": "pause"}
        name = aliases.get(name, name)
        if hasattr(pk.Key, name):
            return getattr(pk.Key, name)
        if len(self.hotkey) == 1:
            return pk.KeyCode.from_char(self.hotkey)
        raise HotkeyError(f"Unknown hotkey '{self.hotkey}' in config.yaml")

    def stop(self) -> None:
        """Remove the keyboard hook."""
        if self._handles:
            import keyboard

            for handle in self._handles:
                try:
                    keyboard.unhook(handle)
                except Exception:  # noqa: BLE001
                    pass
            self._handles = []
        if self._listener is not None:
            self._listener.stop()
            self._listener = None
        log.info("Push-to-talk hotkey released")


# ---------------------------------------------------------------------------
# Voice activity detection
# ---------------------------------------------------------------------------

VAD_WINDOW = 512  # samples per VAD decision at 16 kHz (32 ms)
WAKE_FRAME = 1280  # samples per openWakeWord step at 16 kHz (80 ms)


class SileroVAD:
    """Silero VAD run frame-by-frame with its recurrent state kept between calls.

    Uses the ONNX model bundled with faster-whisper, so no torch is needed.

    Args:
        model_path: Override the model file (defaults to faster-whisper's copy).
    """

    name = "silero"
    _CONTEXT = 64

    def __init__(self, model_path: str | None = None) -> None:
        import os

        import onnxruntime

        if model_path is None:
            import faster_whisper

            model_path = os.path.join(os.path.dirname(faster_whisper.__file__), "assets", "silero_vad_v6.onnx")
        opts = onnxruntime.SessionOptions()
        opts.inter_op_num_threads = 1
        opts.intra_op_num_threads = 1
        opts.log_severity_level = 4
        self._session = onnxruntime.InferenceSession(
            model_path, providers=["CPUExecutionProvider"], sess_options=opts
        )
        self.reset()

    def reset(self) -> None:
        """Forget previous audio (call when starting a new listening phase)."""
        self._h = np.zeros((1, 1, 128), dtype=np.float32)
        self._c = np.zeros((1, 1, 128), dtype=np.float32)
        self._context = np.zeros(self._CONTEXT, dtype=np.float32)

    def __call__(self, window: np.ndarray) -> float:
        """Speech probability (0-1) for one 512-sample window of 16 kHz audio."""
        x = np.concatenate([self._context, window.astype(np.float32, copy=False)])[None, :]
        prob, self._h, self._c = self._session.run(None, {"input": x, "h": self._h, "c": self._c})
        self._context = window[-self._CONTEXT :].astype(np.float32, copy=True)
        return float(np.ravel(prob)[0])


class EnergyVAD:
    """Loudness-based fallback VAD with an adaptive noise floor.

    Much cruder than Silero (any loud noise counts as speech), but needs no model.
    """

    name = "energy"

    def __init__(self, min_rms: float = 0.01, ratio: float = 3.0) -> None:
        self.min_rms = min_rms
        self.ratio = ratio
        self.reset()

    def reset(self) -> None:
        self._floor = self.min_rms / self.ratio

    def __call__(self, window: np.ndarray) -> float:
        rms = float(np.sqrt(np.mean(np.square(window)))) if window.size else 0.0
        speech = rms >= max(self.min_rms, self._floor * self.ratio)
        if not speech:  # track background noise slowly
            self._floor = 0.95 * self._floor + 0.05 * rms
        return 1.0 if speech else 0.0


def create_vad(engine: str = "silero") -> Any:
    """Silero if available, else the energy fallback (with a warning)."""
    if engine == "silero":
        try:
            return SileroVAD()
        except Exception as exc:  # noqa: BLE001 - missing onnxruntime/model
            log.warning("Silero VAD unavailable (%s); using the simpler energy VAD", exc)
    return EnergyVAD()


# ---------------------------------------------------------------------------
# Endpointing: deciding when the user has finished speaking
# ---------------------------------------------------------------------------


@dataclass
class EndpointConfig:
    """Timing rules for one listening phase (all in seconds)."""

    silence_timeout: float = 1.0  # trailing silence that ends an utterance
    max_utterance: float = 15.0   # hard cap on recording length
    min_speech: float = 0.3       # less speech than this is a cough/click, not a request
    preroll: float = 0.5          # audio kept from before speech starts
    start_timeout: float = 5.0    # give up if no speech starts within this
    keep_silence: float = 0.3     # trailing silence kept in the returned audio


@dataclass
class EndpointResult:
    """Outcome of a listening phase: ``kind`` is "speech" or "no_speech"."""

    kind: str
    audio: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=np.float32))
    speech_s: float = 0.0
    reason: str = ""


class Endpointer:
    """Turns a stream of (window, is_speech) decisions into one utterance.

    Pure logic with no audio I/O, so it is easy to test with synthetic arrays.
    Speech that turns out shorter than ``min_speech`` is treated as a false start
    and listening continues until ``start_timeout``.
    """

    def __init__(self, config: EndpointConfig, sample_rate: int = TARGET_RATE) -> None:
        self.config = config
        self.sample_rate = sample_rate
        self.reset()

    def reset(self, start_timeout: float | None = None, seed: list[np.ndarray] | None = None) -> None:
        """Start a new listening phase.

        Args:
            start_timeout: Override how long to wait for speech to start.
            seed: Audio already known to contain the start of speech (barge-in);
                the phase then begins in the "speaking" state.
        """
        self.start_timeout = self.config.start_timeout if start_timeout is None else start_timeout
        self._waited = 0.0
        self._preroll: collections.deque[np.ndarray] = collections.deque()
        self._preroll_s = 0.0
        self._chunks: list[np.ndarray] = []
        self._captured_s = 0.0
        self._speech_s = 0.0
        self._silence_s = 0.0
        self._in_speech = False
        if seed:
            for chunk in seed:
                self._chunks.append(chunk)
                self._captured_s += len(chunk) / self.sample_rate
            self._speech_s = self._captured_s
            self._in_speech = True

    @property
    def in_speech(self) -> bool:
        return self._in_speech

    def push(self, window: np.ndarray, is_speech: bool) -> EndpointResult | None:
        """Feed one window. Returns a result when the phase is over, else None."""
        dur = len(window) / self.sample_rate
        cfg = self.config
        if not self._in_speech:
            self._waited += dur
            if is_speech:
                self._in_speech = True
                self._chunks = list(self._preroll) + [window]
                self._captured_s = self._preroll_s + dur
                self._speech_s, self._silence_s = dur, 0.0
                return None
            self._preroll.append(window)
            self._preroll_s += dur
            while self._preroll and self._preroll_s - len(self._preroll[0]) / self.sample_rate >= cfg.preroll:
                self._preroll_s -= len(self._preroll.popleft()) / self.sample_rate
            if self._waited >= self.start_timeout:
                return EndpointResult("no_speech", reason="no speech started")
            return None

        self._chunks.append(window)
        self._captured_s += dur
        if is_speech:
            self._speech_s += dur
            self._silence_s = 0.0
        else:
            self._silence_s += dur
        if self._captured_s >= cfg.max_utterance:
            return self._finish("max length reached")
        if self._silence_s >= cfg.silence_timeout:
            if self._speech_s >= cfg.min_speech:
                return self._finish("silence after speech")
            # False start (cough, click): keep waiting for real speech.
            self._waited += self._captured_s
            self._in_speech = False
            self._preroll.clear()
            self._preroll_s = 0.0
            if self._waited >= self.start_timeout:
                return EndpointResult("no_speech", reason="only noise before timeout")
        return None

    def _finish(self, reason: str) -> EndpointResult:
        audio = np.concatenate(self._chunks) if self._chunks else np.zeros(0, dtype=np.float32)
        trim = int(max(0.0, self._silence_s - self.config.keep_silence) * self.sample_rate)
        if trim:
            audio = audio[:-trim]
        self._in_speech = False
        if self._speech_s < self.config.min_speech:
            return EndpointResult("no_speech", reason="too little speech")
        return EndpointResult("speech", audio.astype(np.float32, copy=False), self._speech_s, reason)


class BargeInDetector:
    """Fires once speech has been present for ``min_speech`` seconds.

    Short gaps (under ``max_gap``) don't reset the count, so natural pauses
    between syllables still add up.
    """

    def __init__(self, min_speech: float = 0.4, max_gap: float = 0.15, sample_rate: int = TARGET_RATE) -> None:
        self.min_speech = min_speech
        self.max_gap = max_gap
        self.sample_rate = sample_rate
        self.reset()

    def reset(self) -> None:
        self._speech_s = 0.0
        self._gap_s = 0.0

    def push(self, window: np.ndarray, is_speech: bool) -> bool:
        dur = len(window) / self.sample_rate
        if is_speech:
            self._speech_s += dur
            self._gap_s = 0.0
        else:
            self._gap_s += dur
            if self._gap_s > self.max_gap:
                self._speech_s = 0.0
        return self._speech_s >= self.min_speech


# ---------------------------------------------------------------------------
# Wake word
# ---------------------------------------------------------------------------


class WakeWordError(RuntimeError):
    """The wake word model could not be loaded."""


def wake_model_files_present(model: str) -> bool:
    """True if a built-in openWakeWord model (and its feature models) is downloaded,
    or ``model`` is a path to an existing file."""
    import os

    if model.endswith((".onnx", ".tflite")) or os.sep in model or "/" in model:
        return os.path.isfile(model)
    import openwakeword

    if model not in openwakeword.MODELS:
        return False
    needed = [openwakeword.MODELS[model]["model_path"]] + [
        m["model_path"] for m in openwakeword.FEATURE_MODELS.values()
    ]
    return all(os.path.isfile(p.replace(".tflite", ".onnx")) for p in needed)


class WakeWordDetector:
    """openWakeWord model fed 80 ms frames, with a threshold and cooldown.

    Args:
        model: A built-in name ("hey_jarvis", "alexa", ...) or a path to a custom .onnx model.
        threshold: Score (0-1) that counts as a detection.
        cooldown_s: Ignore further detections for this long after one fires.
        on_score: Optional callback receiving every score (used by --debug).
    """

    def __init__(
        self,
        model: str = "hey_jarvis",
        threshold: float = 0.5,
        cooldown_s: float = 1.5,
        on_score: Callable[[float], None] | None = None,
    ) -> None:
        self.threshold = threshold
        self.cooldown_s = cooldown_s
        self.on_score = on_score
        self._last_fire = -1e9
        self.model_name = model
        self._model = self._load(model)

    @staticmethod
    def _load(model: str) -> Any:
        import os

        try:
            import openwakeword
            from openwakeword.model import Model
            from openwakeword.utils import download_models
        except ImportError as exc:
            raise WakeWordError("openwakeword is not installed: pip install -r requirements.txt") from exc

        is_path = model.endswith((".onnx", ".tflite")) or os.sep in model or "/" in model
        if is_path and not os.path.isfile(model):
            raise WakeWordError(f"Wake word model file not found: {model}")
        if not is_path:
            if model not in openwakeword.MODELS:
                choices = ", ".join(sorted(openwakeword.MODELS))
                raise WakeWordError(f"Unknown wake word '{model}'. Built-in choices: {choices}")
            if not wake_model_files_present(model):
                print(f"Downloading wake word model '{model}' (a few MB, first run only)...", flush=True)
                try:
                    download_models(model_names=[model])
                except Exception as exc:  # noqa: BLE001 - network errors vary
                    raise WakeWordError(f"Could not download the wake word model: {exc}") from exc
        try:
            return Model(wakeword_models=[model], inference_framework="onnx")
        except Exception as exc:  # noqa: BLE001
            raise WakeWordError(f"Could not load wake word model '{model}': {exc}") from exc

    def process(self, frame: np.ndarray) -> bool:
        """Score one 1280-sample float32 frame; True when the wake word fires."""
        pcm = (np.clip(frame, -1.0, 1.0) * 32767).astype(np.int16)
        scores = self._model.predict(pcm)
        score = float(max(scores.values())) if scores else 0.0
        if self.on_score is not None:
            self.on_score(score)
        now = time.monotonic()
        if score >= self.threshold and now - self._last_fire >= self.cooldown_s:
            self._last_fire = now
            log.info("Wake word detected (score %.2f)", score)
            self.reset()
            return True
        return False

    def reset(self) -> None:
        """Clear the model's audio history (e.g. after Jarvis has been talking)."""
        try:
            self._model.reset()
        except Exception:  # noqa: BLE001
            log.debug("openWakeWord reset failed", exc_info=True)


# ---------------------------------------------------------------------------
# The always-on audio pipeline
# ---------------------------------------------------------------------------


class ListenMode(enum.Enum):
    OFF = "off"              # ignore the mic (push-to-talk recording, transcribing)
    WAKE = "wake"            # wait for the wake word
    CAPTURE = "capture"      # record one utterance with VAD endpointing
    FOLLOW_UP = "follow_up"  # like CAPTURE with a longer start window, wake word also accepted
    MONITOR = "monitor"      # Jarvis is thinking/speaking: watch for barge-in


@dataclass
class AudioEvent:
    """Something the voice loop must react to.

    kind: "wake", "utterance", "no_speech" or "barge_in" (and "text" for a typed message).
    """

    kind: str
    audio: np.ndarray | None = None
    detail: str = ""


class AudioPipeline:
    """Background thread running wake word, VAD, endpointing and barge-in on mic audio.

    The mic callback only queues frames; all model inference happens on this
    thread. Mode switches made by the voice loop are applied at the next frame
    boundary, so no audio is lost between, say, the wake word and capture.

    Args:
        recorder: The open microphone.
        vad: A VAD callable (window -> probability) with ``reset()``.
        endpoint: Timing rules for utterances.
        wake: Wake word detector, or None (push-to-talk only; VAD still used for follow-ups).
        vad_threshold: Probability above which a window counts as speech.
        barge_in: Barge-in detector, or None to disable voice barge-in.
        wake_listen_timeout: Seconds to start talking after the wake word.
    """

    def __init__(
        self,
        recorder: MicRecorder,
        vad: Any,
        endpoint: EndpointConfig,
        wake: WakeWordDetector | None = None,
        vad_threshold: float = 0.5,
        barge_in: BargeInDetector | None = None,
        wake_listen_timeout: float = 5.0,
    ) -> None:
        self.recorder = recorder
        self.vad = vad
        self.wake = wake
        self.vad_threshold = vad_threshold
        self.endpointer = Endpointer(endpoint)
        self.barge = barge_in
        self.wake_listen_timeout = wake_listen_timeout
        self.events: queue.Queue[AudioEvent] = queue.Queue()
        #: Called on the pipeline thread the moment barge-in fires (before the
        #: event is queued), so speech can be stopped without waiting for the
        #: main loop. Receives "wake word" or "voice".
        self.on_barge_in: Callable[[str], None] | None = None
        self._frames: queue.Queue[np.ndarray] = queue.Queue(maxsize=500)
        self._mode = ListenMode.OFF
        self._pending: tuple[ListenMode, float | None, float] | None = None
        self._ignore_s = 0.0
        self._vad_buf = np.zeros(0, dtype=np.float32)
        self._wake_buf = np.zeros(0, dtype=np.float32)
        self._recent: collections.deque[np.ndarray] = collections.deque(maxlen=30)  # ~1 s
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        recorder.add_listener(self._on_frame)

    # -- control (any thread) ---------------------------------------------------

    @property
    def mode(self) -> ListenMode:
        return self._mode

    def set_mode(self, mode: ListenMode, start_timeout: float | None = None, ignore_s: float = 0.0) -> None:
        """Switch mode at the next frame.

        Args:
            mode: New mode.
            start_timeout: For CAPTURE/FOLLOW_UP, seconds to wait for speech.
            ignore_s: Discard this much audio first (e.g. the tail of Jarvis's own voice).
        """
        with self._lock:
            self._pending = (mode, start_timeout, ignore_s)

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="audio-pipeline", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Stop the thread and detach from the microphone (it may be reused by a new pipeline)."""
        self._stop.set()
        remove = getattr(self.recorder, "remove_listener", None)
        if remove is not None:
            remove(self._on_frame)
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None

    # -- internals (pipeline thread) ----------------------------------------------

    def _on_frame(self, frame: np.ndarray) -> None:
        """Mic callback: never block the audio driver."""
        try:
            self._frames.put_nowait(frame)
        except queue.Full:
            try:
                self._frames.get_nowait()  # drop the oldest frame
                self._frames.put_nowait(frame)
            except (queue.Empty, queue.Full):
                pass

    def _apply_pending(self) -> None:
        with self._lock:
            pending, self._pending = self._pending, None
        if pending is None:
            return
        mode, start_timeout, ignore_s = pending
        self._enter(mode, start_timeout)
        self._ignore_s = ignore_s
        # Drop stale audio buffered before the switch (e.g. Jarvis's own voice).
        if ignore_s:
            self._vad_buf = self._vad_buf[:0]
            self._wake_buf = self._wake_buf[:0]

    def _enter(self, mode: ListenMode, start_timeout: float | None = None, seed: list[np.ndarray] | None = None) -> None:
        old, self._mode = self._mode, mode
        if mode in (ListenMode.CAPTURE, ListenMode.FOLLOW_UP):
            if seed is None:
                self.vad.reset()
            self.endpointer.reset(start_timeout, seed=seed)
        if mode is ListenMode.MONITOR:
            self._recent.clear()
            self.vad.reset()
            if self.barge is not None:
                self.barge.reset()
        if self.wake is not None and old in (ListenMode.OFF, ListenMode.MONITOR, ListenMode.CAPTURE) and mode in (
            ListenMode.WAKE,
            ListenMode.FOLLOW_UP,
            ListenMode.MONITOR,
        ):
            self.wake.reset()  # don't score audio from before this phase
            self._wake_buf = self._wake_buf[:0]
        log.debug("Audio pipeline: %s -> %s", old.value, mode.value)

    def _fire_barge_in(self, detail: str) -> None:
        log.info("Barge-in by %s", detail)
        if self.on_barge_in is not None:
            try:
                self.on_barge_in(detail)
            except Exception:  # noqa: BLE001
                log.exception("Barge-in callback failed")
        self.events.put(AudioEvent("barge_in", detail=detail))

    def _run(self) -> None:
        while not self._stop.is_set():
            self._apply_pending()
            try:
                frame = self._frames.get(timeout=0.1)
            except queue.Empty:
                continue
            if self._ignore_s > 0:
                self._ignore_s -= len(frame) / TARGET_RATE
                continue
            try:
                self._process(frame)
            except Exception:  # noqa: BLE001 - keep listening no matter what
                log.exception("Audio pipeline error")

    def _process(self, frame: np.ndarray) -> None:
        mode = self._mode
        if mode is ListenMode.OFF:
            return
        uses_wake = self.wake is not None and (
            mode in (ListenMode.WAKE, ListenMode.FOLLOW_UP)
            or (mode is ListenMode.MONITOR and self.barge is not None)
        )
        if uses_wake:
            self._wake_buf = np.concatenate([self._wake_buf, frame])
            while len(self._wake_buf) >= WAKE_FRAME:
                chunk, self._wake_buf = self._wake_buf[:WAKE_FRAME], self._wake_buf[WAKE_FRAME:]
                if self.wake.process(chunk):  # type: ignore[union-attr]
                    self._enter(ListenMode.CAPTURE, self.wake_listen_timeout)
                    if mode is ListenMode.MONITOR:
                        self._fire_barge_in("wake word")
                    else:
                        self.events.put(AudioEvent("wake", detail="wake word"))
                    return
        if mode is ListenMode.WAKE:
            return
        if mode is ListenMode.MONITOR and self.barge is None:
            return

        self._vad_buf = np.concatenate([self._vad_buf, frame])
        while len(self._vad_buf) >= VAD_WINDOW and self._mode is mode:
            window, self._vad_buf = self._vad_buf[:VAD_WINDOW], self._vad_buf[VAD_WINDOW:]
            is_speech = self.vad(window) >= self.vad_threshold
            if mode is ListenMode.MONITOR:
                self._recent.append(window)
                if self.barge is not None and self.barge.push(window, is_speech):
                    # Seed the capture with the speech that triggered barge-in.
                    seed = list(self._recent)[-int(0.8 * TARGET_RATE / VAD_WINDOW) :]
                    self._enter(ListenMode.CAPTURE, seed=seed)
                    self._fire_barge_in("voice")
                continue
            result = self.endpointer.push(window, is_speech)
            if result is not None:
                self._mode = ListenMode.OFF  # the voice loop decides what comes next
                if result.kind == "speech":
                    self.events.put(AudioEvent("utterance", result.audio, result.reason))
                else:
                    self.events.put(AudioEvent("no_speech", detail=result.reason))
