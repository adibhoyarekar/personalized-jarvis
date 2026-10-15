"""Voice front-end: wake word / push-to-talk -> speech-to-text -> agent -> streamed speech.

One event loop serves both modes. Audio events (wake word, finished
utterances, silence, barge-in) come from :class:`core.audio_in.AudioPipeline`
and from the push-to-talk hotkey; they are handled on the caller's thread
inside :meth:`VoiceAssistant.run`, so Ctrl+C always lands there.
"""

from __future__ import annotations

import logging
import queue
import random
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import numpy as np

from core.agent import Agent
from core.audio_in import AudioEvent, AudioPipeline, HotkeyError, ListenMode, MicRecorder, PushToTalk
from core.safety import parse_yes_no
from core.sounds import Sounds
from core.state import AssistantState, StateManager
from core.stt import SpeechToText, STTError
from core.text_utils import is_exit_phrase, strip_wake_phrase
from core.tts import SpeechStream, TTSEngine

log = logging.getLogger(__name__)

S = AssistantState
PrintFn = Callable[..., None]


@dataclass
class VoiceSettings:
    """Behaviour knobs for the voice loop (filled from config.yaml by main.py)."""

    hotkey: str | None = "f9"            # None disables push-to-talk
    hotkey_backend: str = "auto"
    min_press_s: float = 0.3
    follow_up: bool = True
    follow_up_window: float = 6.0
    wake_listen_timeout: float = 5.0
    headphones: bool = False
    echo_tail_s: float = 0.3             # speakers: ignore this much audio after Jarvis stops
    filler: bool = True
    filler_delay: float = 2.5
    fillers: list[str] = field(default_factory=lambda: ["One moment.", "On it.", "Just a second."])
    exit_replies: list[str] = field(default_factory=lambda: ["Anytime.", "Happy to help.", "Righto."])
    confirm_timeout: float = 20.0


class FillerTimer:
    """Speaks a short filler if a tool is running and no reply text has arrived in time."""

    def __init__(self, delay: float, speak: Callable[[], None]) -> None:
        self.delay = delay
        self._speak = speak
        self._start = time.monotonic()
        self._tool = False
        self._done = False
        self._lock = threading.Lock()
        self._timer = threading.Timer(delay, self._check)
        self._timer.daemon = True
        self._timer.start()

    def _check(self) -> None:
        with self._lock:
            if self._done or not self._tool or time.monotonic() - self._start < self.delay:
                return
            self._done = True
        self._speak()

    def tool_started(self) -> None:
        with self._lock:
            self._tool = True
        self._check()

    def text_arrived(self) -> None:
        self.cancel()

    def cancel(self) -> None:
        with self._lock:
            self._done = True
        self._timer.cancel()


class VoiceAssistant:
    """Runs the spoken conversation loop.

    Args:
        agent: The configured agent.
        recorder: An opened :class:`MicRecorder`.
        stt: Loaded speech recogniser.
        tts: TTS engine, or None to print replies only.
        settings: Behaviour settings.
        pipeline: Always-on audio pipeline (wake word/VAD), or None for push-to-talk only.
        state: Shared state manager (created if not given).
        sounds: Beep/chime player.
    """

    def __init__(
        self,
        agent: Agent,
        recorder: MicRecorder,
        stt: SpeechToText,
        tts: TTSEngine | None,
        settings: VoiceSettings | None = None,
        pipeline: AudioPipeline | None = None,
        state: StateManager | None = None,
        sounds: Sounds | None = None,
        print_fn: PrintFn = print,
    ) -> None:
        self.agent = agent
        self.recorder = recorder
        self.stt = stt
        self.tts = tts
        self.settings = settings or VoiceSettings()
        self.pipeline = pipeline
        self.state = state or StateManager()
        self.sounds = sounds or Sounds(enabled=False)
        self.print = print_fn
        self.events: queue.Queue[AudioEvent] = queue.Queue()
        if pipeline is not None:
            pipeline.events = self.events  # one queue for hotkey and pipeline events
            pipeline.on_barge_in = self._on_voice_barge_in
        if tts is not None:
            tts.on_activity = self._on_tts_activity
        self._busy = threading.Event()             # agent is working on a reply
        self._awaiting_answer = threading.Event()  # a yes/no question is open
        self._interrupted = threading.Event()      # user barged in on the current reply
        self._epoch = 0                            # bumps on every interruption
        self._speech_failed_notice = False
        self.ptt: PushToTalk | None = None
        if self.settings.hotkey:
            self.ptt = PushToTalk(
                self.settings.hotkey, self._on_press, self._on_release, backend=self.settings.hotkey_backend
            )

    @property
    def wake_enabled(self) -> bool:
        return self.pipeline is not None and self.pipeline.wake is not None

    # -- interruption (any thread) ----------------------------------------------

    def _interrupt(self, reason: str) -> None:
        """Stop the current reply: cancel the agent and silence speech at once."""
        self._epoch += 1
        if self._busy.is_set():
            self._interrupted.set()  # stop feeding speech before stopping it
            self.agent.cancel()
        if self.tts is not None:
            self.tts.stop()
        log.info("Interrupted (%s)", reason)

    def _on_voice_barge_in(self, detail: str) -> None:
        """Pipeline thread: the user spoke over Jarvis (headphones mode)."""
        if self._awaiting_answer.is_set():
            return
        self._interrupt(f"barge-in by {detail}")
        if detail == "wake word":
            self.sounds.chime()
        self.state.set(S.LISTENING, f"barge-in ({detail})")

    # -- hotkey callbacks (keyboard hook thread) ----------------------------------

    def _on_press(self) -> None:
        if self._awaiting_answer.is_set():
            if self.tts is not None:
                self.tts.stop()
        else:
            self._interrupt("push-to-talk")
        if self.pipeline is not None:
            self.pipeline.set_mode(ListenMode.OFF)
        self.sounds.start()
        self.recorder.start_recording()
        self.state.set(S.LISTENING, "push-to-talk")

    def _on_release(self, held_s: float) -> None:
        audio = self.recorder.stop_recording()
        if held_s < self.settings.min_press_s:
            log.info("Ignored %.2fs tap of the hotkey", held_s)
            self.events.put(AudioEvent("tap"))
            return
        self.sounds.stop()
        self.events.put(AudioEvent("utterance", audio, "push-to-talk"))

    # -- TTS activity (playback thread) ------------------------------------------

    def _on_tts_activity(self, playing: bool) -> None:
        if playing:
            self.state.transition_if({S.THINKING}, S.SPEAKING, "speaking")
        elif self._busy.is_set():
            self.state.transition_if({S.SPEAKING}, S.THINKING, "waiting for the rest of the reply")

    # -- main loop ------------------------------------------------------------------

    def run(self) -> None:
        """Handle voice events until Ctrl+C."""
        if self.ptt is not None:
            try:
                self.ptt.start()
            except HotkeyError:
                if not self.wake_enabled:
                    raise
                log.warning("Push-to-talk hotkey unavailable; wake word only", exc_info=True)
                self.print("(push-to-talk hotkey unavailable; use the wake word)")
                self.ptt = None
        if self.pipeline is not None:
            self.pipeline.start()
        self._go_idle("ready")
        self.print(self._banner())
        try:
            while True:
                try:
                    event = self.events.get(timeout=0.3)
                except queue.Empty:
                    continue
                self.dispatch(event)
        finally:
            self.close()

    def _banner(self) -> str:
        name = self.agent.name
        ways = []
        if self.wake_enabled:
            ways.append(f'say "Hey {name}"')
        if self.ptt is not None:
            ways.append(f"hold {self.settings.hotkey.upper()}")  # type: ignore[union-attr]
        return f"{name} is ready: {' or '.join(ways)} and speak. Ctrl+C quits.\n"

    def dispatch(self, event: AudioEvent) -> None:
        """React to one audio/hotkey event."""
        log.debug("Event: %s %s", event.kind, event.detail)
        if event.kind == "wake":
            self.state.set(S.WAKE_DETECTED, "wake word")
            self.sounds.chime()
            self.state.set(S.LISTENING, "listening for a request")
        elif event.kind == "barge_in":
            pass  # already handled on the pipeline thread; the capture is running
        elif event.kind in ("no_speech", "tap"):
            if self.state.is_(S.LISTENING, S.FOLLOW_UP, S.WAKE_DETECTED):
                self._go_idle(event.detail or "nothing heard")
        elif event.kind == "utterance":
            self.process(event)

    def process(self, event: AudioEvent) -> None:
        """Transcribe one utterance, answer it, then go to follow-up or idle."""
        assert event.audio is not None
        epoch = self._epoch
        self.state.set(S.THINKING, "transcribing")
        if self.pipeline is not None:
            self.pipeline.set_mode(ListenMode.MONITOR)
        text = self._transcribe(event.audio, quiet=event.detail != "push-to-talk")
        if epoch != self._epoch:
            return  # the user started talking again meanwhile
        if not text:
            self._go_idle("nothing intelligible")
            return
        if is_exit_phrase(text):
            self.print(f"You: {text}")
            self._end_conversation()
            return
        self.handle(text)
        if epoch != self._epoch:
            return  # barged in: the new request is already being captured
        if self.tts is not None:
            while not self.tts.wait(timeout=0.1):
                if epoch != self._epoch:
                    return
        if epoch == self._epoch:
            self._after_reply()

    def handle(self, text: str) -> None:
        """Send one utterance to the agent and speak the streamed reply."""
        self.print(f"You: {text}")
        self.print(f"{self.agent.name}: ", end="", flush=True)
        stream = SpeechStream(self.tts)
        self._speech_failed_notice = False
        self._interrupted.clear()
        filler: FillerTimer | None = None
        if self.settings.filler and self.tts is not None:
            filler = FillerTimer(self.settings.filler_delay, self._speak_filler)

        def on_text(chunk: str) -> None:
            if self._interrupted.is_set():
                return
            if filler is not None:
                filler.text_arrived()
            self.print(chunk, end="", flush=True)
            stream.feed(chunk)

        def on_status(state: str, detail: str) -> None:
            if state == "tool":
                log.info("Running tool %s", detail)
                if filler is not None:
                    filler.tool_started()

        self._busy.set()
        try:
            reply = self.agent.ask(text, on_text=on_text, on_status=on_status)
        finally:
            self._busy.clear()
            if filler is not None:
                filler.cancel()
        if reply.interrupted or self._interrupted.is_set():
            self.print(" (interrupted)\n")
            return
        stream.finish()
        self.print("\n")

    def _speak_filler(self) -> None:
        if self.tts is not None and not self._interrupted.is_set():
            self.tts.speak(random.choice(self.settings.fillers))

    def _after_reply(self) -> None:
        if self.settings.follow_up and self.pipeline is not None:
            self.state.set(S.FOLLOW_UP, f"{self.settings.follow_up_window:.0f}s follow-up window")
            self.pipeline.set_mode(
                ListenMode.FOLLOW_UP, start_timeout=self.settings.follow_up_window, ignore_s=self._echo_tail()
            )
        else:
            self._go_idle("reply finished")

    def _end_conversation(self) -> None:
        self.say(random.choice(self.settings.exit_replies))
        if self.tts is not None:
            self.tts.wait(timeout=5)
        self._go_idle("conversation ended")

    def _go_idle(self, reason: str) -> None:
        self.state.set(S.IDLE, reason)
        if self.pipeline is not None:
            mode = ListenMode.WAKE if self.wake_enabled else ListenMode.OFF
            self.pipeline.set_mode(mode, ignore_s=self._echo_tail())

    def _echo_tail(self) -> float:
        return 0.0 if self.settings.headphones or self.tts is None else self.settings.echo_tail_s

    def _transcribe(self, audio: np.ndarray, quiet: bool = False) -> str:
        try:
            text = strip_wake_phrase(self.stt.transcribe(audio))
        except STTError as exc:
            self.say(str(exc))
            return ""
        if not text:
            log.info("Nothing intelligible in %.1fs of audio", audio.size / 16_000)
            if not quiet:
                self.print("(didn't catch that - hold the key a little longer and speak up)")
        return text

    # -- helpers --------------------------------------------------------------------

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

    def confirm(self, question: str) -> bool:
        """Ask a yes/no question out loud and listen for the answer (voice or hotkey)."""
        self._awaiting_answer.set()
        try:
            while not self.events.empty():  # drop anything said before the question
                self.events.get_nowait()
            self.print("")
            self.say(question)
            if self.tts is not None:
                self.tts.wait(timeout=15)
            hints = []
            if self.pipeline is not None:
                hints.append("say yes or no")
                self.state.set(S.LISTENING, "waiting for yes/no")
                self.pipeline.set_mode(ListenMode.CAPTURE, start_timeout=8.0, ignore_s=self._echo_tail())
            if self.ptt is not None:
                hints.append(f"hold {self.settings.hotkey.upper()} and answer")  # type: ignore[union-attr]
            self.print(f"({' or '.join(hints)})")
            answer = self._wait_for_answer(self.settings.confirm_timeout)
            self.state.set(S.THINKING, "answer received")
            if self.pipeline is not None:
                self.pipeline.set_mode(ListenMode.MONITOR)
            if answer is None:
                self.say("No answer, so I'll leave it.")
                return False
            self.print(f"You: {answer}")
            approved = parse_yes_no(answer)
            if not approved:
                self.say("Okay, cancelled.")
            return approved
        finally:
            self._awaiting_answer.clear()

    def _wait_for_answer(self, timeout: float) -> str | None:
        deadline = time.monotonic() + timeout
        while (remaining := deadline - time.monotonic()) > 0:
            try:
                event = self.events.get(timeout=remaining)
            except queue.Empty:
                break
            if event.kind == "utterance" and event.audio is not None:
                return self._transcribe(event.audio, quiet=True)
            if event.kind == "no_speech":
                return None
        return None

    def close(self) -> None:
        """Release the hotkey, audio pipeline, microphone and audio output."""
        try:
            if self.ptt is not None:
                self.ptt.stop()
        finally:
            if self.pipeline is not None:
                self.pipeline.stop()
            self.recorder.close()
            if self.tts is not None:
                self.tts.shutdown()


class WakeScoreMonitor:
    """--debug helper: prints wake word scores so the threshold can be tuned.

    Prints every score above ``show_above`` immediately, plus the peak score of
    each ``interval`` seconds as a heartbeat.
    """

    def __init__(self, threshold: float, print_fn: PrintFn = print, interval: float = 3.0, show_above: float = 0.1) -> None:
        self.threshold = threshold
        self.print = print_fn
        self.interval = interval
        self.show_above = show_above
        self._peak = 0.0
        self._last = time.monotonic()

    @staticmethod
    def bar(score: float, width: int = 20) -> str:
        filled = int(round(min(max(score, 0.0), 1.0) * width))
        return "#" * filled + "." * (width - filled)

    def __call__(self, score: float) -> None:
        self._peak = max(self._peak, score)
        if score >= self.show_above:
            mark = "  <- FIRE" if score >= self.threshold else ""
            self.print(f"[wake] {score:.2f} [{self.bar(score)}] threshold {self.threshold:.2f}{mark}")
        now = time.monotonic()
        if now - self._last >= self.interval:
            self.print(f"[wake] peak {self._peak:.3f} over last {self.interval:.0f}s (threshold {self.threshold:.2f})")
            self._peak = 0.0
            self._last = now


def debug_state_printer(print_fn: PrintFn = print) -> Callable[[Any, Any, str], None]:
    """--debug helper: a state listener that prints every transition."""

    def show(old: AssistantState, new: AssistantState, reason: str) -> None:
        print_fn(f"[state] {old.name} -> {new.name}{f' ({reason})' if reason else ''}")

    return show
