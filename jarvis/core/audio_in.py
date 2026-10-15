"""Microphone capture, device selection and the push-to-talk hotkey.

The mic stream stays open while Jarvis runs and keeps a short pre-roll buffer,
so the first syllable after pressing the hotkey isn't lost. Audio is only
collected between :meth:`MicRecorder.start_recording` and
:meth:`MicRecorder.stop_recording`.
"""

from __future__ import annotations

import collections
import logging
import sys
import threading
import time
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
            noun = "microphone" if kind == "input" else "speaker/headphones"
            raise AudioError(
                f"No {noun} found. Plug one in, check Windows privacy settings "
                "(Settings > Privacy > Microphone), or run: python main.py --list-devices"
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
