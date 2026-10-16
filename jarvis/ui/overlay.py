"""Optional on-screen overlay: a small, borderless, semi-transparent, always-on-top box.

It shows the state, what you said, and the reply as it streams, then fades
out a few seconds after Jarvis goes idle. On Windows the window is
click-through. tkinter runs on its own thread; other threads only put
messages on a queue, so it never touches the audio loop.

Enable with ``ui.overlay: true`` in config.yaml.
"""

from __future__ import annotations

import logging
import queue
import sys
import threading
import time
from dataclasses import dataclass
from typing import Any

from core.state import AssistantState

log = logging.getLogger(__name__)

LABELS = {
    AssistantState.IDLE: "Idle",
    AssistantState.WAKE_DETECTED: "Listening...",
    AssistantState.LISTENING: "Listening...",
    AssistantState.THINKING: "Thinking...",
    AssistantState.SPEAKING: "Speaking",
    AssistantState.FOLLOW_UP: "Listening for a follow-up...",
}

BASE_ALPHA = 0.88
FADE_OUT_S = 0.6
MAX_REPLY_CHARS = 280


@dataclass
class OverlayModel:
    """What the overlay shows and how opaque it is - pure logic, no tkinter.

    Visible while Jarvis is active; once idle it stays for ``fade_s`` seconds,
    then fades out over ``FADE_OUT_S``.
    """

    fade_s: float = 4.0
    status: str = ""
    heard: str = ""
    reply: str = ""
    active: bool = False
    idle_since: float | None = None
    shown: bool = False

    def on_state(self, state: AssistantState, now: float) -> None:
        self.status = LABELS[state]
        if state is AssistantState.IDLE:
            self.active = False
            self.idle_since = now
        else:
            if not self.active and state in (AssistantState.WAKE_DETECTED, AssistantState.LISTENING):
                self.heard, self.reply = "", ""  # a new request starts with a clean box
            self.active = True
            self.idle_since = None
            self.shown = True

    def on_transcript(self, text: str) -> None:
        self.heard, self.reply = text, ""
        self.shown = True

    def on_reply(self, chunk: str) -> None:
        self.reply = (self.reply + chunk)[-MAX_REPLY_CHARS:]

    def alpha(self, now: float) -> float:
        """Window opacity right now (0 = hidden)."""
        if not self.shown:
            return 0.0
        if self.active or self.idle_since is None:
            return BASE_ALPHA
        idle = now - self.idle_since
        if idle <= self.fade_s:
            return BASE_ALPHA
        fading = (idle - self.fade_s) / FADE_OUT_S
        return 0.0 if fading >= 1 else round(BASE_ALPHA * (1 - fading), 3)

    def lines(self) -> tuple[str, str, str]:
        return (
            self.status,
            f"You: {self.heard}" if self.heard else "",
            f"Jarvis: {self.reply.strip()}" if self.reply.strip() else "",
        )


class Overlay:
    """The tkinter window, owned by its own thread. Feed it with the ``on_*`` methods."""

    def __init__(self, corner: str = "bottom-right", fade_s: float = 4.0, width: int = 380) -> None:
        self.corner = corner
        self.width = width
        self.model = OverlayModel(fade_s=fade_s)
        self._q: queue.Queue[tuple[str, Any]] = queue.Queue()
        self._thread: threading.Thread | None = None

    # -- thread-safe inputs --------------------------------------------------------

    def on_state(self, _old: AssistantState, new: AssistantState, _reason: str = "") -> None:
        self._q.put(("state", new))

    def on_transcript(self, text: str) -> None:
        self._q.put(("heard", text))

    def on_reply(self, chunk: str) -> None:
        self._q.put(("reply", chunk))

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="overlay", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._q.put(("quit", None))
        if self._thread is not None:
            self._thread.join(timeout=2)

    # -- tkinter (overlay thread only) -------------------------------------------------

    def _run(self) -> None:
        try:
            import tkinter as tk
        except ImportError:
            log.warning("Overlay disabled: tkinter is not available")
            return
        try:
            root = tk.Tk()
        except Exception as exc:  # noqa: BLE001 - no display, etc.
            log.warning("Overlay disabled: %s", exc)
            return
        root.withdraw()
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        root.attributes("-alpha", 0.0)
        bg, fg, dim = "#1e1f24", "#f2f2f2", "#a0a4ad"
        frame = tk.Frame(root, bg=bg, padx=14, pady=10)
        frame.pack(fill="both", expand=True)
        status = tk.Label(frame, bg=bg, fg="#7fb2ff", font=("Segoe UI", 10, "bold"), anchor="w")
        heard = tk.Label(frame, bg=bg, fg=dim, font=("Segoe UI", 10), anchor="w", justify="left", wraplength=self.width - 28)
        reply = tk.Label(frame, bg=bg, fg=fg, font=("Segoe UI", 10), anchor="w", justify="left", wraplength=self.width - 28)
        for label in (status, heard, reply):
            label.pack(fill="x", anchor="w")
        root.update_idletasks()
        self._make_click_through(root)
        visible = False

        def place() -> None:
            root.update_idletasks()
            w, h = self.width, max(60, frame.winfo_reqheight())
            sw, sh, margin = root.winfo_screenwidth(), root.winfo_screenheight(), 24
            x = margin if "left" in self.corner else sw - w - margin
            y = margin if "top" in self.corner else sh - h - margin - 40  # clear the taskbar
            root.geometry(f"{w}x{h}+{x}+{y}")

        def poll() -> None:
            nonlocal visible
            try:
                while True:
                    kind, value = self._q.get_nowait()
                    if kind == "quit":
                        root.destroy()
                        return
                    if kind == "state":
                        self.model.on_state(value, time.monotonic())
                    elif kind == "heard":
                        self.model.on_transcript(value)
                    elif kind == "reply":
                        self.model.on_reply(value)
            except queue.Empty:
                pass
            a, b, c = self.model.lines()
            status.config(text=a)
            heard.config(text=b)
            reply.config(text=c)
            alpha = self.model.alpha(time.monotonic())
            if alpha <= 0:
                if visible:
                    root.withdraw()
                    visible = False
            else:
                if not visible:
                    root.deiconify()
                    visible = True
                place()
                root.attributes("-alpha", alpha)
            root.after(50, poll)

        root.after(50, poll)
        try:
            root.mainloop()
        except Exception:  # noqa: BLE001
            log.exception("Overlay crashed")

    @staticmethod
    def _make_click_through(root: Any) -> None:
        """Windows: let mouse clicks pass through the overlay to the window below."""
        if sys.platform != "win32":
            return
        try:
            import ctypes

            user32 = ctypes.windll.user32  # type: ignore[attr-defined]
            hwnd = user32.GetParent(root.winfo_id()) or root.winfo_id()
            GWL_EXSTYLE, WS_EX_LAYERED, WS_EX_TRANSPARENT, WS_EX_TOOLWINDOW = -20, 0x80000, 0x20, 0x80
            style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style | WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW)
        except Exception:  # noqa: BLE001
            log.debug("Click-through not available", exc_info=True)
