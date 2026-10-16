"""The Jarvis desktop window: an animated orb, the live conversation and a message box.

tkinter runs on its own thread (like the overlay); other threads only post
messages to a queue, so the window can never block the voice loop. Closing the
window hides it to the tray; Quit in the tray menu ends Jarvis.

The colour, layout and conversation helpers at the top are plain Python so
they can be tested without a display.
"""

from __future__ import annotations

import gc
import logging
import math
import queue
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Protocol

from core.state import AssistantState

log = logging.getLogger(__name__)

S = AssistantState

BG = "#060912"
SURFACE = "#0C1222"
SURFACE_2 = "#101A2F"
BORDER = "#1C2740"
TEXT = "#E6EDF7"
TEXT_DIM = "#7C8BA6"
TEXT_FAINT = "#4A5873"
USER_BUBBLE = "#15305A"
ONLINE = "#34D399"
WARNING = "#FBBF24"
DANGER = "#F43F5E"
WHITE = "#FFFFFF"

ACTIVE_FRAME_MS = 33   # ~30 fps while something is happening
IDLE_FRAME_MS = 50
HIDDEN_POLL_MS = 120   # only drain the queue while hidden
CHAT_LIMIT = 150
FONT_FAMILIES = ("Segoe UI Variable Display", "Segoe UI", "Helvetica Neue", "Helvetica", "Arial")


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def hex_rgb(color: str) -> tuple[int, int, int]:
    color = color.lstrip("#")
    return int(color[0:2], 16), int(color[2:4], 16), int(color[4:6], 16)


def blend(a: str, b: str, t: float) -> str:
    """Mix two #rrggbb colours (t=0 gives a, t=1 gives b). tkinter has no alpha, so this fakes it."""
    t = min(max(t, 0.0), 1.0)
    (ra, ga, ba), (rb, gb, bb) = hex_rgb(a), hex_rgb(b)
    return "#%02x%02x%02x" % (round(ra + (rb - ra) * t), round(ga + (gb - ga) * t), round(ba + (bb - ba) * t))


@dataclass(frozen=True)
class Look:
    """How the orb looks in one state."""

    label: str
    accent: str
    accent2: str
    spin: float     # ring rotation, degrees per second
    breathe: float  # core pulse, as a fraction of the radius


LOOKS: dict[str, Look] = {
    "starting": Look("STARTING", "#94A3B8", "#22D3EE", 40, 0.03),
    "idle": Look("STANDBY", "#22D3EE", "#3B82F6", 16, 0.035),
    "listening": Look("LISTENING", "#2DF5C4", "#22D3EE", 70, 0.06),
    "thinking": Look("THINKING", "#A78BFA", "#F472B6", 240, 0.05),
    "speaking": Look("SPEAKING", "#60A5FA", "#22D3EE", 95, 0.07),
    "muted": Look("MIC OFF", "#F43F5E", "#9F1239", 6, 0.015),
    "error": Look("ERROR", "#EF4444", "#F59E0B", 0, 0.015),
}


def look_key(state: AssistantState, muted: bool = False, error: bool = False, starting: bool = False) -> str:
    """Which :data:`LOOKS` entry to show. Errors win, then start-up, then mute."""
    if error:
        return "error"
    if starting:
        return "starting"
    if muted:
        return "muted"
    if state in (S.WAKE_DETECTED, S.LISTENING, S.FOLLOW_UP):
        return "listening"
    if state is S.THINKING:
        return "thinking"
    if state is S.SPEAKING:
        return "speaking"
    return "idle"


def hint_text(key: str, name: str, hotkey: str, wake: bool) -> str:
    """The line under the orb telling the user what they can do right now."""
    key_name = hotkey.upper() if hotkey else ""
    hints = {
        "starting": "Warming up speech recognition...",
        "muted": "Microphone is off. Click the mic button to turn it on.",
        "listening": "Go ahead, I'm listening.",
        "thinking": "Working on it...",
        "speaking": f"Type, or hold {key_name}, to interrupt." if key_name else "Type to interrupt.",
        "error": "Something went wrong. Details are in logs/jarvis.log.",
    }
    if key in hints:
        return hints[key]
    if wake:
        return f'Say "Hey {name}" or hold {key_name} to talk.' if key_name else f'Say "Hey {name}" to talk.'
    return f"Hold {key_name} and speak, or type below." if key_name else "Type below to talk."


def status_badge(note: str, error: bool = False) -> tuple[str, str]:
    """Header pill text and colour, from the same status note the tray tooltip uses."""
    if error:
        return "ERROR", DANGER
    note = (note or "").lower()
    if note in ("starting", "loading"):
        return "STARTING", "#94A3B8"
    if note == "offline":
        return "OFFLINE", WARNING
    return "ONLINE", ONLINE


def spaced(text: str) -> str:
    """Letter-spaced label text (tkinter has no letter-spacing)."""
    return " ".join(text)


def rounded_rect(x1: float, y1: float, x2: float, y2: float, r: float) -> list[float]:
    """Polygon points for a rounded rectangle; draw with ``smooth=True``."""
    r = max(0.0, min(r, (x2 - x1) / 2, (y2 - y1) / 2))
    # Doubled points keep the edges straight; the corner points are the curve's control points.
    return [
        x1 + r, y1, x1 + r, y1, x2 - r, y1, x2 - r, y1, x2, y1,
        x2, y1 + r, x2, y1 + r, x2, y2 - r, x2, y2 - r, x2, y2,
        x2 - r, y2, x2 - r, y2, x1 + r, y2, x1 + r, y2, x1, y2,
        x1, y2 - r, x1, y2 - r, x1, y1 + r, x1, y1 + r, x1, y1,
    ]


def spectrum(t: float, level: float, bars: int) -> list[float]:
    """0..1 heights for the equaliser ring: a few travelling waves scaled by ``level``."""
    out = []
    for i in range(bars):
        a = 2 * math.pi * i / bars
        wave = 0.55 + 0.22 * math.sin(3 * a + t * 5.1) + 0.14 * math.sin(7 * a - t * 8.3) + 0.09 * math.sin(11 * a + t * 13.7)
        out.append(min(1.0, max(0.0, wave * level)))
    return out


def mic_level(rms: float) -> float:
    """Map microphone RMS to 0..1 on a dB scale (-55 dB is silence, -15 dB is loud speech)."""
    if rms <= 0:
        return 0.0
    return min(1.0, max(0.0, (20 * math.log10(rms) + 55) / 40))


@dataclass
class ChatMessage:
    role: str          # "user" or "assistant"
    text: str
    time: str
    streaming: bool = False


class ChatModel:
    """The conversation shown in the window (no tkinter)."""

    def __init__(self, limit: int = CHAT_LIMIT, clock: Callable[[], datetime] = datetime.now) -> None:
        self.limit = limit
        self.clock = clock
        self.messages: list[ChatMessage] = []
        #: Messages dropped from the top so far; the view rebuilds when this changes.
        self.trimmed = 0

    def add_user(self, text: str) -> None:
        text = text.strip()
        if text:
            self.finish_reply()
            self._append(ChatMessage("user", text, self._stamp()))

    def add_reply_chunk(self, chunk: str) -> None:
        if not chunk:
            return
        last = self.messages[-1] if self.messages else None
        if last is not None and last.role == "assistant" and last.streaming:
            last.text += chunk
        else:
            self._append(ChatMessage("assistant", chunk.lstrip(), self._stamp(), streaming=True))

    def finish_reply(self) -> None:
        if self.messages and self.messages[-1].streaming:
            last = self.messages[-1]
            last.streaming = False
            last.text = last.text.strip()

    def add_notice(self, text: str) -> None:
        """Something Jarvis said outside a reply: a reminder, a question, the greeting."""
        text = text.strip()
        if text:
            self.finish_reply()
            self._append(ChatMessage("assistant", text, self._stamp()))

    def _stamp(self) -> str:
        return self.clock().strftime("%H:%M")

    def _append(self, message: ChatMessage) -> None:
        self.messages.append(message)
        extra = len(self.messages) - self.limit
        if extra > 0:
            del self.messages[:extra]
            self.trimmed += extra


class WindowActions(Protocol):
    """What the window asks the app to do (called from the window thread)."""

    def submit_text(self, text: str) -> None: ...
    def toggle_mute(self) -> None: ...
    def is_muted(self) -> bool: ...
    def current_mode(self) -> str: ...  # "wake" | "push-to-talk"
    def window_closed(self) -> None: ...


def prepare_windows_process(app_id: str = "Jarvis.Assistant") -> None:
    """Windows: sharp text on high-DPI screens and Jarvis's own taskbar button.

    Call before any window (tray included) is created.
    """
    if sys.platform != "win32":
        return
    import ctypes

    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # type: ignore[attr-defined]  # system DPI aware
    except Exception:  # noqa: BLE001 - older Windows or already set
        log.debug("DPI awareness not set", exc_info=True)
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(app_id)  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        log.debug("AppUserModelID not set", exc_info=True)


# ---------------------------------------------------------------------------
# The window
# ---------------------------------------------------------------------------


class JarvisWindow:
    """The desktop window, owned by its own thread. Feed it with the thread-safe methods.

    Args:
        actions: App callbacks (send a message, mute, close).
        name: Assistant name for the title and labels.
        hotkey: Push-to-talk key, shown in hints.
        start_hidden: Start in the tray instead of on screen.
        level: Returns the latest microphone RMS (for the listening animation).
    """

    def __init__(
        self,
        actions: WindowActions,
        name: str = "Jarvis",
        hotkey: str = "f9",
        start_hidden: bool = False,
        level: Callable[[], float] | None = None,
    ) -> None:
        self.actions = actions
        self.name = name
        self.hotkey = hotkey
        self.start_hidden = start_hidden
        self.level = level or (lambda: 0.0)
        self._q: queue.Queue[tuple[str, Any]] = queue.Queue()
        self._thread: threading.Thread | None = None

    # -- thread-safe inputs ----------------------------------------------------------

    def on_state(self, _old: AssistantState, new: AssistantState, _reason: str = "") -> None:
        self._q.put(("state", new))

    def on_transcript(self, text: str) -> None:
        self._q.put(("user", text))

    def on_reply(self, chunk: str) -> None:
        self._q.put(("reply", chunk))

    def on_reply_done(self) -> None:
        self._q.put(("done", None))

    def on_notice(self, text: str) -> None:
        self._q.put(("notice", text))

    def set_status_note(self, note: str) -> None:
        self._q.put(("note", note))

    def set_error(self, error: bool) -> None:
        self._q.put(("error", error))

    def show(self) -> None:
        self._q.put(("show", None))

    def hide(self) -> None:
        self._q.put(("hide", None))

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="window", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._q.put(("quit", None))
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=3)

    # -- window thread ---------------------------------------------------------------------

    def _run(self) -> None:
        try:
            import tkinter as tk
        except ImportError:
            log.warning("Window disabled: tkinter is not available")
            return
        try:
            root = tk.Tk()
        except Exception as exc:  # noqa: BLE001 - no display
            log.warning("Window disabled: %s", exc)
            return
        view: _View | None = None
        try:
            view = _View(root, self)
            root.mainloop()
        except Exception:  # noqa: BLE001
            log.exception("Window crashed")
        finally:
            # Tk objects must be freed on the thread that made them, or Tcl aborts the process.
            if view is not None:
                view.close()
            view = None
            try:
                root.destroy()
            except Exception:  # noqa: BLE001 - already destroyed
                pass
            root = None  # type: ignore[assignment]
            gc.collect()


class _View:
    """Everything tkinter. Lives on the window thread only."""

    def __init__(self, root: Any, owner: JarvisWindow) -> None:
        import tkinter as tk
        import tkinter.font as tkfont

        self.tk = tk
        self.root = root
        self.owner = owner
        self.actions = owner.actions
        self.model = ChatModel()
        self.state = AssistantState.IDLE
        self.note = "starting"
        self.error = False
        self.visible = False
        self.closed = False
        self.t0 = time.monotonic()
        self.level = 0.0
        self.accent = LOOKS["starting"].accent
        self.accent2 = LOOKS["starting"].accent2
        self.placeholder = True
        self.button_keys: dict[str, tuple[Any, ...]] = {}
        self.hover: set[str] = set()

        root.withdraw()
        root.title(owner.name)
        root.configure(bg=BG)
        self.s = s = max(1.0, root.winfo_fpixels("1i") / 96.0)
        families = set(tkfont.families(root))
        family = next((f for f in FONT_FAMILIES if f in families), "TkDefaultFont")
        self.f_title = tkfont.Font(root, family=family, size=11, weight="bold")
        self.f_label = tkfont.Font(root, family=family, size=10, weight="bold")
        self.f_small = tkfont.Font(root, family=family, size=9)
        self.f_body = tkfont.Font(root, family=family, size=11)
        self.icon = self._make_icon()

        root.grid_columnconfigure(0, weight=1)
        root.grid_rowconfigure(0, weight=5)
        root.grid_rowconfigure(2, weight=6)
        self.orb = tk.Canvas(root, bg=BG, highlightthickness=0, bd=0)
        self.orb.grid(row=0, column=0, sticky="nsew")
        self.divider = tk.Canvas(root, bg=BG, height=max(1, int(s)), highlightthickness=0, bd=0)
        self.divider.grid(row=1, column=0, sticky="ew")
        self.chat = tk.Canvas(root, bg=BG, highlightthickness=0, bd=0, yscrollincrement=int(18 * s))
        self.chat.grid(row=2, column=0, sticky="nsew")
        self.chat_view = _ChatView(self)
        self._build_input_bar()

        self.orb.bind("<Configure>", lambda _e: self._draw_static())
        self.divider.bind("<Configure>", lambda _e: self._draw_divider())
        self.chat.bind("<Configure>", lambda _e: self.chat_view.render(self.model, force=True))
        # Bound on the toplevel only: every child's events pass through it once.
        root.bind("<MouseWheel>", self._on_wheel)
        root.bind("<Button-4>", lambda _e: self.chat.yview_scroll(-3, "units"))
        root.bind("<Button-5>", lambda _e: self.chat.yview_scroll(3, "units"))
        root.protocol("WM_DELETE_WINDOW", self._on_close)

        w = int(470 * s)
        h = int(min(800 * s, root.winfo_screenheight() * 0.86))
        x = (root.winfo_screenwidth() - w) // 2
        y = max(0, (root.winfo_screenheight() - h) // 2 - int(20 * s))
        root.geometry(f"{w}x{h}+{x}+{y}")
        root.minsize(int(380 * s), int(560 * s))
        root.update_idletasks()
        self._style_title_bar()
        if not owner.start_hidden:
            self._show()
        root.after(30, self._tick)

    # -- setup -----------------------------------------------------------------------

    def _make_icon(self) -> Any:
        try:
            from PIL import ImageTk

            from ui.tray import make_icon

            icon = ImageTk.PhotoImage(make_icon((34, 211, 238), size=64), master=self.root)
            self.root.iconphoto(True, icon)
            return icon
        except Exception:  # noqa: BLE001 - a missing icon is cosmetic
            log.debug("Window icon unavailable", exc_info=True)
            return None

    def _style_title_bar(self) -> None:
        """Windows 10/11: dark title bar coloured like the window (silently skipped elsewhere)."""
        if sys.platform != "win32":
            return
        try:
            import ctypes

            hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id()) or self.root.winfo_id()  # type: ignore[attr-defined]
            dwm = ctypes.windll.dwmapi  # type: ignore[attr-defined]

            def set_attr(attr: int, value: int) -> None:
                v = ctypes.c_int(value)
                dwm.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(v), ctypes.sizeof(v))

            def colorref(color: str) -> int:
                r, g, b = hex_rgb(color)
                return (b << 16) | (g << 8) | r

            set_attr(20, 1)                  # DWMWA_USE_IMMERSIVE_DARK_MODE
            set_attr(35, colorref(BG))       # DWMWA_CAPTION_COLOR (Windows 11)
            set_attr(34, colorref(BORDER))   # DWMWA_BORDER_COLOR (Windows 11)
            set_attr(36, colorref(TEXT_DIM))  # DWMWA_TEXT_COLOR (Windows 11)
        except Exception:  # noqa: BLE001
            log.debug("Title bar styling unavailable", exc_info=True)

    def _build_input_bar(self) -> None:
        tk, s = self.tk, self.s
        bar = tk.Frame(self.root, bg=BG, padx=int(16 * s), pady=int(14 * s))
        bar.grid(row=3, column=0, sticky="ew")
        bar.grid_columnconfigure(1, weight=1)
        size = int(44 * s)
        self.mic_btn = tk.Canvas(bar, width=size, height=size, bg=BG, highlightthickness=0, bd=0, cursor="hand2")
        self.mic_btn.grid(row=0, column=0, padx=(0, int(10 * s)))
        self.box = tk.Canvas(bar, height=size, bg=BG, highlightthickness=0, bd=0)
        self.box.grid(row=0, column=1, sticky="ew")
        self.send_btn = tk.Canvas(bar, width=size, height=size, bg=BG, highlightthickness=0, bd=0, cursor="hand2")
        self.send_btn.grid(row=0, column=2, padx=(int(10 * s), 0))
        self.entry = tk.Entry(
            self.box, bg=SURFACE, fg=TEXT_FAINT, relief="flat", bd=0, highlightthickness=0,
            insertbackground=TEXT, font=self.f_body, insertwidth=max(1, int(2 * s)),
        )
        self.entry_window = self.box.create_window(0, 0, window=self.entry, anchor="w")
        self._set_placeholder()

        self.mic_btn.bind("<Button-1>", lambda _e: self._toggle_mute())
        self.send_btn.bind("<Button-1>", lambda _e: self._submit())
        for name, widget in (("mic", self.mic_btn), ("send", self.send_btn)):
            widget.bind("<Enter>", lambda _e, n=name: self._hover(n, True))
            widget.bind("<Leave>", lambda _e, n=name: self._hover(n, False))
        self.entry.bind("<Return>", lambda _e: self._submit())
        self.entry.bind("<KP_Enter>", lambda _e: self._submit())
        self.entry.bind("<Escape>", lambda _e: self._clear_entry())
        self.entry.bind("<FocusIn>", lambda _e: self._focus(True))
        self.entry.bind("<FocusOut>", lambda _e: self._focus(False))
        self.box.bind("<Configure>", lambda _e: self._draw_box())
        self.box.bind("<Button-1>", lambda _e: self.entry.focus_set())

    # -- input -----------------------------------------------------------------------

    def _placeholder_text(self) -> str:
        return f"Message {self.owner.name}..."

    def _set_placeholder(self) -> None:
        self.entry.delete(0, "end")
        self.entry.insert(0, self._placeholder_text())
        self.entry.configure(fg=TEXT_FAINT)
        self.placeholder = True

    def _clear_entry(self) -> None:
        self.entry.delete(0, "end")
        if self.root.focus_get() is not self.entry:
            self._set_placeholder()

    def _focus(self, focused: bool) -> None:
        if focused and self.placeholder:
            self.entry.delete(0, "end")
            self.entry.configure(fg=TEXT)
            self.placeholder = False
        elif not focused and not self.entry.get():
            self._set_placeholder()
        self._draw_box()

    def _submit(self) -> None:
        text = "" if self.placeholder else self.entry.get().strip()
        if not text:
            self.entry.focus_set()
            return
        self.entry.delete(0, "end")
        try:
            self.actions.submit_text(text)
        except Exception:  # noqa: BLE001
            log.exception("Sending a typed message failed")

    def _toggle_mute(self) -> None:
        try:
            self.actions.toggle_mute()
        except Exception:  # noqa: BLE001
            log.exception("Mute toggle failed")

    def _hover(self, name: str, on: bool) -> None:
        if on:
            self.hover.add(name)
        else:
            self.hover.discard(name)

    def _on_wheel(self, event: Any) -> None:
        step = -1 if event.delta > 0 else 1
        self.chat.yview_scroll(step * max(1, abs(int(event.delta)) // 40), "units")

    def _on_close(self) -> None:
        try:
            self.actions.window_closed()
        except Exception:  # noqa: BLE001
            log.exception("Window close handler failed")

    # -- show / hide -------------------------------------------------------------------

    def _show(self) -> None:
        root = self.root
        root.deiconify()
        if root.state() == "iconic":
            root.state("normal")
        self._style_title_bar()
        root.lift()
        root.attributes("-topmost", True)
        root.after(250, lambda: root.attributes("-topmost", False))
        root.focus_force()
        self.visible = True

    def _hide(self) -> None:
        self.root.withdraw()
        self.visible = False

    # -- the loop ----------------------------------------------------------------------

    def _drain(self) -> bool:
        """Apply queued messages. Returns False once the window should close."""
        chat_changed = False
        try:
            while True:
                kind, value = self.owner._q.get_nowait()
                if kind == "quit":
                    return False
                if kind == "state":
                    self.state = value
                elif kind == "user":
                    self.model.add_user(value)
                    chat_changed = True
                elif kind == "reply":
                    self.model.add_reply_chunk(value)
                    chat_changed = True
                elif kind == "done":
                    self.model.finish_reply()
                    chat_changed = True
                elif kind == "notice":
                    self.model.add_notice(value)
                    chat_changed = True
                elif kind == "note":
                    self.note = value
                elif kind == "error":
                    self.error = bool(value)
                elif kind == "show":
                    self._show()
                elif kind == "hide":
                    self._hide()
        except queue.Empty:
            pass
        if chat_changed:
            try:
                self.chat_view.render(self.model)
            except Exception:  # noqa: BLE001 - keep the window alive; the next change redraws
                log.debug("Chat render failed", exc_info=True)
        return True

    def _tick(self) -> None:
        if self.closed:
            return
        if not self._drain():
            self.closed = True
            self.root.quit()
            return
        if not self.visible or self.root.state() == "iconic":
            self.root.after(HIDDEN_POLL_MS, self._tick)
            return
        key = self._key()
        try:
            self._draw_frame(key)
        except Exception:  # noqa: BLE001 - never let one bad frame kill the window
            log.debug("Frame failed", exc_info=True)
        delay = IDLE_FRAME_MS if key in ("idle", "muted", "error") else ACTIVE_FRAME_MS
        self.root.after(delay, self._tick)

    def _key(self) -> str:
        try:
            muted = bool(self.actions.is_muted())
        except Exception:  # noqa: BLE001
            muted = False
        starting = self.note in ("starting", "loading")
        return look_key(self.state, muted=muted, error=self.error, starting=starting)

    def close(self) -> None:
        """Drop Tk images and fonts on this thread."""
        self.icon = None
        self.f_title = self.f_label = self.f_small = self.f_body = None

    # -- drawing: orb ----------------------------------------------------------------

    def _geometry(self) -> tuple[float, float, float, float, float]:
        w, h, s = self.orb.winfo_width(), self.orb.winfo_height(), self.s
        header, labels = 56 * s, 62 * s
        avail = max(40.0, h - header - labels)
        radius = max(18.0, min(w * 0.17, avail / 4.3))
        return w, h, w / 2, header + avail / 2, radius

    def _draw_static(self) -> None:
        """Background gradient, dot grid and tick ring (redrawn only on resize)."""
        c, s = self.orb, self.s
        c.delete("static")
        w, h, cx, cy, r = self._geometry()
        bands = 28
        for i in range(bands):
            y1, y2 = h * i / bands, h * (i + 1) / bands + 1
            c.create_rectangle(0, y1, w, y2, fill=blend("#0A1020", BG, i / (bands - 1)), outline="", tags="static")
        step = 26 * s
        dot = blend(BG, "#25324D", 0.55)
        y = step / 2
        while y < h:
            x = step / 2
            while x < w:
                c.create_rectangle(x, y, x + max(1, s), y + max(1, s), fill=dot, outline="", tags="static")
                x += step
            y += step
        for i in range(72):
            a = 2 * math.pi * i / 72
            inner = r * (1.98 if i % 6 else 1.94)
            outer = r * 2.04
            c.create_line(
                cx + math.cos(a) * inner, cy + math.sin(a) * inner, cx + math.cos(a) * outer, cy + math.sin(a) * outer,
                fill=blend(BG, "#3A4A6B", 0.8 if i % 6 == 0 else 0.45), width=max(1, s), tags="static",
            )
        c.tag_lower("static")

    def _draw_divider(self) -> None:
        d = self.divider
        d.delete("all")
        w, segments = d.winfo_width(), 40
        for i in range(segments):
            k = 1 - abs(i / (segments - 1) * 2 - 1)  # bright in the middle, fading to the edges
            d.create_rectangle(w * i / segments, 0, w * (i + 1) / segments + 1, 4, fill=blend(BG, BORDER, k), outline="")

    def _draw_frame(self, key: str) -> None:
        c, s = self.orb, self.s
        look = LOOKS[key]
        t = time.monotonic() - self.t0
        self.accent = blend(self.accent, look.accent, 0.14)
        self.accent2 = blend(self.accent2, look.accent2, 0.14)
        acc, acc2 = self.accent, self.accent2
        raw = mic_level(float(self.owner.level() or 0.0)) if key == "listening" else 0.0
        self.level = max(raw, self.level * 0.86)
        lvl = self.level

        c.delete("dyn")
        w, h, cx, cy, R = self._geometry()
        b = 1 + look.breathe * math.sin(t * 2.1) + 0.16 * lvl
        r = R * b

        def oval(x: float, y: float, rad: float, **kw: Any) -> None:
            c.create_oval(x - rad, y - rad, x + rad, y + rad, tags="dyn", **kw)

        for i in range(16):  # aura
            k = i / 15
            oval(cx, cy, R * (2.2 - 1.15 * k) * (1 + 0.05 * lvl), fill=blend(BG, acc, 0.012 + 0.11 * k * k), outline="")

        rings = ((1.42, 2.0, 3, 1.0, 0.8, acc), (1.62, 1.0, 6, -0.55, 0.5, acc2), (1.84, 3.0, 2, 0.32, 0.32, acc))
        for j, (radius, width, segments, speed, alpha, color) in enumerate(rings):
            rr = R * radius
            base = t * look.spin * speed + j * 47
            span = 360 / segments * (0.32 if j == 1 else 0.58)
            for n in range(segments):
                c.create_arc(
                    cx - rr, cy - rr, cx + rr, cy + rr, start=(base + n * 360 / segments) % 360, extent=span,
                    style="arc", outline=blend(BG, color, alpha), width=width * s, tags="dyn",
                )

        if key in ("listening", "speaking"):
            amp = max(0.18, lvl) if key == "listening" else 0.6 + 0.25 * math.sin(t * 3.3)
            bars = 72
            for i, height in enumerate(spectrum(t, amp, bars)):
                a = 2 * math.pi * i / bars
                r1 = r * 1.07
                r2 = r1 + R * (0.03 + 0.26 * height)
                c.create_line(
                    cx + math.cos(a) * r1, cy + math.sin(a) * r1, cx + math.cos(a) * r2, cy + math.sin(a) * r2,
                    fill=blend(BG, acc, 0.3 + 0.65 * height), width=max(1, 2 * s), capstyle="round", tags="dyn",
                )
        if key == "listening":
            for k in range(3):
                p = (t * 0.65 + k / 3) % 1
                oval(cx, cy, r * (1.05 + 0.8 * p), outline=blend(BG, acc, 0.6 * (1 - p)), width=max(1, 2 * s))
        elif key == "thinking":
            for head, color in ((0.0, acc2), (math.pi, acc)):
                for k in range(14):
                    a = t * 3.4 + head - k * 0.09
                    size = 4.2 * s * (1 - k / 15)
                    oval(cx + math.cos(a) * R * 1.25, cy + math.sin(a) * R * 1.25, size,
                         fill=blend(BG, color, 1 - k / 14), outline="")
        elif key == "idle":
            for k in range(3):
                a = t * 0.35 + k * 2 * math.pi / 3
                oval(cx + math.cos(a) * R * 1.62, cy + math.sin(a) * R * 1.62, 2.6 * s, fill=blend(BG, acc2, 0.85), outline="")

        for i in range(13):  # the core: a lit sphere
            k = i / 12
            oval(cx, cy, r * (1 - 0.9 * k), fill=blend(blend(BG, acc, 0.5), blend(acc, WHITE, 0.7), k ** 1.5), outline="")
        ring = r * 0.74
        c.create_arc(cx - ring, cy - ring, cx + ring, cy + ring, start=(-t * look.spin * 1.6) % 360, extent=290,
                     style="arc", outline=blend(acc, WHITE, 0.85), width=max(1, s), tags="dyn")

        label_y = cy + R * 2.04 + 22 * s
        c.create_text(cx, label_y, text=spaced(look.label), font=self.f_label, fill=acc, tags="dyn")
        mode = "wake"
        try:
            mode = self.actions.current_mode()
        except Exception:  # noqa: BLE001
            pass
        hint = hint_text(key, self.owner.name, self.owner.hotkey, mode == "wake")
        c.create_text(cx, label_y + 22 * s, text=hint, font=self.f_small, fill=TEXT_DIM, tags="dyn")

        # header: glowing dot, name, status pill
        oval(24 * s, 28 * s, 7 * s, fill=blend(BG, acc, 0.25), outline="")
        oval(24 * s, 28 * s, 3.5 * s, fill=acc, outline="")
        c.create_text(40 * s, 28 * s, text=spaced(self.owner.name.upper()), anchor="w", font=self.f_title, fill=TEXT, tags="dyn")
        badge, color = status_badge(self.note, self.error)
        text_id = c.create_text(w - 22 * s, 28 * s, text=badge, anchor="e", font=self.f_small, fill=color, tags="dyn")
        x1, y1, x2, y2 = c.bbox(text_id)
        pill = c.create_polygon(rounded_rect(x1 - 22 * s, y1 - 5 * s, x2 + 11 * s, y2 + 5 * s, 12 * s), smooth=True,
                                fill=blend(BG, color, 0.1), outline=blend(BG, color, 0.45), tags="dyn")
        c.tag_lower(pill, text_id)
        pulse = 0.55 + 0.45 * math.sin(t * 3) if badge == "ONLINE" else 1.0
        oval(x1 - 11 * s, (y1 + y2) / 2, 3.2 * s, fill=blend(BG, color, pulse), outline="")

        self._draw_buttons()

    # -- drawing: input bar ----------------------------------------------------------

    def _draw_box(self) -> None:
        b, s = self.box, self.s
        w, h = b.winfo_width(), b.winfo_height()
        if w < 10:
            return
        b.delete("box")
        focused = self.root.focus_get() is self.entry
        outline = blend(BORDER, self.accent, 0.6) if focused else BORDER
        b.create_polygon(rounded_rect(1, 1, w - 1, h - 1, h / 2), smooth=True, fill=SURFACE, outline=outline,
                         width=max(1, s), tags="box")
        b.tag_lower("box")
        b.coords(self.entry_window, 18 * s, h / 2)
        b.itemconfigure(self.entry_window, width=max(10, w - 36 * s))

    def _draw_buttons(self) -> None:
        muted = False
        try:
            muted = bool(self.actions.is_muted())
        except Exception:  # noqa: BLE001
            pass
        acc = self.accent
        mic_key = (muted, acc, "mic" in self.hover)
        if self.button_keys.get("mic") != mic_key:
            self.button_keys["mic"] = mic_key
            self._draw_mic(muted, "mic" in self.hover)
        send_key = (acc, "send" in self.hover)
        if self.button_keys.get("send") != send_key:
            self.button_keys["send"] = send_key
            self._draw_send("send" in self.hover)
            if self.root.focus_get() is self.entry:
                self._draw_box()

    def _draw_mic(self, muted: bool, hover: bool) -> None:
        c, s = self.mic_btn, self.s
        c.delete("all")
        size = int(c.cget("width"))
        x = y = size / 2
        rad = size / 2 - 2 * s
        if muted:
            fill, outline, ink = blend(BG, DANGER, 0.35 if hover else 0.22), DANGER, WHITE
        else:
            fill, outline, ink = blend(SURFACE, self.accent, 0.18 if hover else 0.08), blend(BG, self.accent, 0.7), self.accent
        c.create_oval(x - rad, y - rad, x + rad, y + rad, fill=fill, outline=outline, width=max(1, 1.5 * s))
        u = s
        c.create_polygon(rounded_rect(x - 4.5 * u, y - 10 * u, x + 4.5 * u, y + 3 * u, 4.5 * u), smooth=True, fill=ink, outline=ink)
        c.create_arc(x - 8 * u, y - 8.5 * u, x + 8 * u, y + 6.5 * u, start=200, extent=140, style="arc", outline=ink, width=max(1, 2 * u))
        c.create_line(x, y + 6.5 * u, x, y + 10.5 * u, fill=ink, width=max(1, 2 * u))
        c.create_line(x - 4.5 * u, y + 10.5 * u, x + 4.5 * u, y + 10.5 * u, fill=ink, width=max(1, 2 * u), capstyle="round")
        if muted:
            c.create_line(x - 10 * u, y + 10 * u, x + 10 * u, y - 10 * u, fill=ink, width=max(1, 2.2 * u), capstyle="round")

    def _draw_send(self, hover: bool) -> None:
        c, s = self.send_btn, self.s
        c.delete("all")
        size = int(c.cget("width"))
        x = y = size / 2
        rad = size / 2 - 2 * s
        top = blend(self.accent, WHITE, 0.25 if hover else 0.1)
        c.create_oval(x - rad, y - rad, x + rad, y + rad, fill=blend(self.accent, BG, 0.25), outline="")
        c.create_oval(x - rad * 0.9, y - rad * 0.95, x + rad * 0.9, y + rad * 0.8, fill=top, outline="")
        u = s
        c.create_polygon(x - 7 * u, y - 8 * u, x + 9 * u, y, x - 7 * u, y + 8 * u, x - 3.5 * u, y, fill=BG, outline=BG)


class _ChatView:
    """Draws the conversation as rounded bubbles on a canvas, redrawing only what changed."""

    def __init__(self, view: _View) -> None:
        self.view = view
        self.c = view.chat
        self.drawn: list[tuple[str, bool, float]] = []  # (text, streaming, bottom y) per message
        self.width = 0
        self.trimmed = 0

    def render(self, model: ChatModel, force: bool = False) -> None:
        c, s = self.c, self.view.s
        width = c.winfo_width()
        if width < 60:
            return
        if force or width != self.width or model.trimmed != self.trimmed:
            c.delete("msg")
            self.drawn, self.width, self.trimmed = [], width, model.trimmed
        start = len(self.drawn)
        for i, (text, streaming, _bottom) in enumerate(self.drawn):
            if i >= len(model.messages) or model.messages[i].text != text or model.messages[i].streaming != streaming:
                start = i
                break
        if start == len(self.drawn) == len(model.messages) and not force:
            return
        stick = c.yview()[1] >= 0.995 or (model.messages and model.messages[-1].role == "user")
        for i in range(start, len(self.drawn)):
            c.delete(f"m{i}")
        del self.drawn[start:]
        y = self.drawn[-1][2] if self.drawn else 18 * s
        for i in range(start, len(model.messages)):
            message = model.messages[i]
            y = self._bubble(i, message, y)
            self.drawn.append((message.text, message.streaming, y))
        c.delete("empty")
        if not model.messages:
            c.create_text(width / 2, max(40 * s, c.winfo_height() / 2), text="Your conversation will appear here.",
                          font=self.view.f_small, fill=TEXT_FAINT, tags="empty")
        c.configure(scrollregion=(0, 0, width, max(y + 6 * s, c.winfo_height())))
        if stick:
            c.yview_moveto(1.0)

    def _bubble(self, index: int, message: ChatMessage, y: float) -> float:
        c, s, view = self.c, self.view.s, self.view
        margin, pad_x, pad_y = 18 * s, 14 * s, 10 * s
        max_w = min(self.width * 0.8, 560 * s)
        user = message.role == "user"
        tags = ("msg", f"m{index}")
        who = "YOU" if user else view.owner.name.upper()
        anchor = "ne" if user else "nw"
        lx = self.width - margin if user else margin + 2 * s
        label = c.create_text(lx, y, text=f"{who}  ·  {message.time}", anchor=anchor, font=view.f_small,
                              fill=TEXT_FAINT, tags=tags)
        top = c.bbox(label)[3] + 6 * s
        text = message.text or "..."
        if message.streaming:
            text += " ▍"
        tx = self.width - margin - pad_x if user else margin + pad_x + 3 * s
        body = c.create_text(tx, top + pad_y, text=text, anchor=anchor, width=max_w - 2 * pad_x, font=view.f_body,
                             fill=TEXT, justify="left", tags=tags)
        x1, y1, x2, y2 = c.bbox(body)
        bx1, by1, bx2, by2 = x1 - pad_x, y1 - pad_y, x2 + pad_x, y2 + pad_y
        fill = USER_BUBBLE if user else SURFACE_2
        bubble = c.create_polygon(rounded_rect(bx1, by1, bx2, by2, 14 * s), smooth=True, fill=fill,
                                  outline=blend(fill, WHITE, 0.07), tags=tags)
        c.tag_lower(bubble, body)
        if not user:
            c.create_line(bx1 + 1.5 * s, by1 + 11 * s, bx1 + 1.5 * s, by2 - 11 * s, fill=LOOKS["idle"].accent,
                          width=max(1, 2 * s), capstyle="round", tags=tags)
        return by2 + 16 * s
