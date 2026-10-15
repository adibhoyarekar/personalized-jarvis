"""Voice front-end: push-to-talk -> speech-to-text -> agent -> streamed speech."""

from __future__ import annotations

import logging
import queue
import threading
import time
from typing import Any, Callable

import numpy as np

from core.agent import Agent
from core.audio_in import MicRecorder, PushToTalk
from core.safety import parse_yes_no
from core.sounds import Sounds
from core.stt import SpeechToText, STTError
from core.tts import SpeechStream, TTSEngine

log = logging.getLogger(__name__)

PrintFn = Callable[..., None]


class VoiceAssistant:
    """Runs the push-to-talk conversation loop.

    Threads: the keyboard hook thread only starts/stops recording and queues
    audio; transcription, the agent and the confirmation prompts all run on
    the caller's thread inside :meth:`run`, so Ctrl+C always lands there.

    Args:
        agent: The configured agent.
        recorder: An opened :class:`MicRecorder`.
        stt: Loaded speech recogniser.
        tts: TTS engine, or None to print replies only.
        hotkey: Push-to-talk key name.
        sounds: Beep player.
        min_press_s: Presses shorter than this are ignored.
        hotkey_backend: "auto", "keyboard" or "pynput".
    """

    def __init__(
        self,
        agent: Agent,
        recorder: MicRecorder,
        stt: SpeechToText,
        tts: TTSEngine | None,
        hotkey: str = "f9",
        sounds: Sounds | None = None,
        min_press_s: float = 0.3,
        hotkey_backend: str = "auto",
        print_fn: PrintFn = print,
    ) -> None:
        self.agent = agent
        self.recorder = recorder
        self.stt = stt
        self.tts = tts
        self.hotkey = hotkey
        self.sounds = sounds or Sounds(enabled=False)
        self.min_press_s = min_press_s
        self.print = print_fn
        self.utterances: queue.Queue[np.ndarray] = queue.Queue()
        self._busy = threading.Event()  # agent is working on a reply
        self._awaiting_answer = threading.Event()  # a yes/no question is open
        self._interrupted = threading.Event()  # user barged in on the current reply
        self._speech_failed_notice = False
        self.ptt = PushToTalk(hotkey, self._on_press, self._on_release, backend=hotkey_backend)

    # -- hotkey callbacks (keyboard hook thread) ------------------------------

    def _on_press(self) -> None:
        if self._busy.is_set() and not self._awaiting_answer.is_set():
            self._interrupted.set()  # stop feeding speech before stopping it
            self.agent.cancel()
        if self.tts is not None and self.tts.is_speaking:
            self.tts.stop()  # barge-in: stop talking and listen
            log.info("Barge-in: speech stopped")
        self.sounds.start()
        self.recorder.start_recording()

    def _on_release(self, held_s: float) -> None:
        audio = self.recorder.stop_recording()
        if held_s < self.min_press_s:
            log.info("Ignored %.2fs tap of the hotkey", held_s)
            return
        self.sounds.stop()
        self.utterances.put(audio)

    # -- main loop ------------------------------------------------------------

    def run(self) -> None:
        """Listen for push-to-talk utterances until Ctrl+C."""
        self.ptt.start()
        key = self.hotkey.upper()
        self.print(f"{self.agent.name} is ready. Hold {key} and speak; release to send. Ctrl+C quits.\n")
        try:
            while True:
                try:
                    audio = self.utterances.get(timeout=0.3)
                except queue.Empty:
                    continue
                text = self._transcribe(audio)
                if text:
                    self.handle(text)
        finally:
            self.close()

    def handle(self, text: str) -> None:
        """Send one utterance to the agent and speak the streamed reply."""
        self.print(f"You: {text}")
        self.print(f"{self.agent.name}: ", end="", flush=True)
        stream = SpeechStream(self.tts)
        self._speech_failed_notice = False
        self._interrupted.clear()

        def on_text(chunk: str) -> None:
            if self._interrupted.is_set():
                return
            self.print(chunk, end="", flush=True)
            stream.feed(chunk)

        def on_status(state: str, detail: str) -> None:
            if state == "tool":
                log.info("Running tool %s", detail)

        self._busy.set()
        try:
            reply = self.agent.ask(text, on_text=on_text, on_status=on_status)
        finally:
            self._busy.clear()
        if reply.interrupted or self._interrupted.is_set():
            self.print(" (interrupted)\n")
            return
        stream.finish()
        self.print("\n")

    def _transcribe(self, audio: np.ndarray) -> str:
        start = time.perf_counter()
        try:
            text = self.stt.transcribe(audio)
        except STTError as exc:
            self.say(str(exc))
            return ""
        if not text:
            log.info("Nothing intelligible in %.1fs of audio", audio.size / 16_000)
            self.print("(didn't catch that - hold the key a little longer and speak up)")
        log.debug("STT round trip %.2fs", time.perf_counter() - start)
        return text

    # -- helpers --------------------------------------------------------------

    def say(self, text: str) -> None:
        """Print and speak a short message."""
        self.print(f"{self.agent.name}: {text}")
        if self.tts is not None:
            self.tts.speak(text)

    def on_tts_error(self, text: str, exc: Exception) -> None:
        """TTS failed: the reply is already printed, so just tell the user once."""
        if not self._speech_failed_notice:
            self._speech_failed_notice = True
            self.print(f"\n(speech unavailable: {exc} - showing text only)")

    def confirm(self, question: str, timeout_s: float = 20.0) -> bool:
        """Ask a yes/no question out loud and listen for the push-to-talk answer."""
        self._awaiting_answer.set()
        try:
            # Drop anything said before the question was asked.
            while not self.utterances.empty():
                self.utterances.get_nowait()
            self.print("")
            self.say(question)
            if self.tts is not None:
                self.tts.wait(timeout=15)
            self.print(f"(hold {self.hotkey.upper()} and answer yes or no)")
            try:
                audio = self.utterances.get(timeout=timeout_s)
            except queue.Empty:
                self.say("No answer, so I'll leave it.")
                return False
            answer = self._transcribe(audio)
            self.print(f"You: {answer}")
            approved = parse_yes_no(answer)
            if not approved:
                self.say("Okay, cancelled.")
            return approved
        finally:
            self._awaiting_answer.clear()

    def close(self) -> None:
        """Release the hotkey, microphone and audio output."""
        try:
            self.ptt.stop()
        finally:
            self.recorder.close()
            if self.tts is not None:
                self.tts.shutdown()
