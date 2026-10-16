"""System tray icon (pystray + Pillow): shows Jarvis's state, offers a small menu.

Icons are drawn in code - one colour per state, no asset files. The tray runs
on its own thread and only talks to the voice loop through the thread-safe
:class:`TrayActions` callbacks, so it can never block audio.
"""

from __future__ import annotations

import enum
import logging
import threading
from typing import Any, Callable, Protocol

from PIL import Image, ImageDraw

from core.state import AssistantState, StateManager

log = logging.getLogger(__name__)


class DisplayState(enum.Enum):
    IDLE = "Idle"
    LISTENING = "Listening"
    THINKING = "Thinking"
    SPEAKING = "Speaking"
    MUTED = "Muted"
    ERROR = "Error"


COLORS: dict[DisplayState, tuple[int, int, int]] = {
    DisplayState.IDLE: (128, 128, 128),      # gray
    DisplayState.LISTENING: (46, 170, 80),   # green
    DisplayState.THINKING: (240, 170, 0),    # amber
    DisplayState.SPEAKING: (40, 120, 230),   # blue
    DisplayState.MUTED: (220, 40, 40),       # red
    DisplayState.ERROR: (120, 0, 0),         # dark red
}

_FROM_ASSISTANT = {
    AssistantState.IDLE: DisplayState.IDLE,
    AssistantState.WAKE_DETECTED: DisplayState.LISTENING,
    AssistantState.LISTENING: DisplayState.LISTENING,
    AssistantState.FOLLOW_UP: DisplayState.LISTENING,
    AssistantState.THINKING: DisplayState.THINKING,
    AssistantState.SPEAKING: DisplayState.SPEAKING,
}


def display_state(state: AssistantState, muted: bool = False, error: bool = False) -> DisplayState:
    """What the icon shows: an error wins over mute, mute wins over the assistant state."""
    if error:
        return DisplayState.ERROR
    if muted:
        return DisplayState.MUTED
    return _FROM_ASSISTANT[state]


def state_color(state: AssistantState, muted: bool = False, error: bool = False) -> tuple[int, int, int]:
    return COLORS[display_state(state, muted, error)]


def make_icon(color: tuple[int, int, int], size: int = 64, muted: bool = False) -> Image.Image:
    """A filled circle with a white "J"; a diagonal slash when muted."""
    scale = 4  # draw big, downsample = smooth edges
    big = size * scale
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    pad = big // 16
    draw.ellipse((pad, pad, big - pad, big - pad), fill=color + (255,), outline=(255, 255, 255, 230), width=big // 20)
    # A simple "J" from two strokes, so no font file is needed.
    w = big // 10
    x = big * 0.58
    draw.line([(x, big * 0.25), (x, big * 0.62)], fill="white", width=w)
    draw.arc((big * 0.30, big * 0.44, x + w / 2, big * 0.78), start=0, end=180, fill="white", width=w)
    if muted:
        draw.line([(big * 0.2, big * 0.8), (big * 0.8, big * 0.2)], fill="white", width=w)
    return img.resize((size, size), Image.LANCZOS)


class TrayActions(Protocol):
    """What the tray menu can ask the app to do (all must be thread-safe)."""

    def toggle_mute(self) -> None: ...
    def is_muted(self) -> bool: ...
    def set_mode(self, mode: str) -> None: ...  # "wake" | "push-to-talk"
    def current_mode(self) -> str: ...
    def open_logs(self) -> None: ...
    def open_config(self) -> None: ...
    def open_readme(self) -> None: ...
    def restart(self) -> None: ...
    def quit(self) -> None: ...


def tooltip(state: DisplayState, last_command: str, name: str = "Jarvis", limit: int = 127) -> str:
    """Tray tooltip; Windows truncates at 127 characters."""
    text = f"{name}: {state.value}"
    if last_command:
        text += f"\nLast: {last_command}"
    return text if len(text) <= limit else text[: limit - 3] + "..."


class TrayIcon:
    """The tray icon. Call :meth:`start` once; it runs on its own thread.

    Args:
        state: The assistant's state manager (subscribed to for icon updates).
        actions: Menu callbacks.
        name: Shown in the tooltip.
    """

    def __init__(self, state: StateManager, actions: TrayActions, name: str = "Jarvis") -> None:
        self.state = state
        self.actions = actions
        self.name = name
        self.error = False
        self.status_note = ""
        self.last_command = ""
        self._icon: Any = None
        self._thread: threading.Thread | None = None
        self._images: dict[DisplayState, Image.Image] = {}
        self._unsubscribe: Callable[[], None] | None = None

    # -- state ------------------------------------------------------------------

    def current(self) -> DisplayState:
        return display_state(self.state.state, self.actions.is_muted(), self.error)

    def image(self, which: DisplayState) -> Image.Image:
        if which not in self._images:
            self._images[which] = make_icon(COLORS[which], muted=which is DisplayState.MUTED)
        return self._images[which]

    def refresh(self) -> None:
        """Redraw icon and tooltip (safe from any thread)."""
        if self._icon is None:
            return
        which = self.current()
        try:
            self._icon.icon = self.image(which)
            note = f" ({self.status_note})" if self.status_note else ""
            self._icon.title = tooltip(which, self.last_command, self.name + note)
            self._icon.update_menu()
        except Exception:  # noqa: BLE001 - a tray hiccup must never reach the voice loop
            log.debug("Tray refresh failed", exc_info=True)

    def set_error(self, error: bool) -> None:
        self.error = error
        self.refresh()

    def set_status_note(self, note: str) -> None:
        """Short extra text in the tooltip, e.g. "offline" or "starting"."""
        self.status_note = note
        self.refresh()

    def set_last_command(self, text: str) -> None:
        self.last_command = text.strip()
        self.refresh()

    def notify(self, message: str, title: str = "Jarvis") -> None:
        """Balloon notification through the tray (fallback for toasts)."""
        if self._icon is not None:
            try:
                self._icon.notify(message, title)
            except Exception:  # noqa: BLE001
                log.debug("Tray notification failed", exc_info=True)

    # -- lifecycle --------------------------------------------------------------

    def _menu(self, pystray: Any) -> Any:
        item, menu = pystray.MenuItem, pystray.Menu

        def mode_item(label: str, mode: str) -> Any:
            return item(
                label,
                lambda _i, _it: self.actions.set_mode(mode),
                checked=lambda _it: self.actions.current_mode() == mode,
                radio=True,
            )

        return menu(
            item(
                lambda _it: "Unmute microphone" if self.actions.is_muted() else "Mute microphone",
                lambda _i, _it: (self.actions.toggle_mute(), self.refresh()),
                default=True,  # clicking the icon toggles mute
            ),
            item("Mode", menu(mode_item("Wake word (\"Hey Jarvis\")", "wake"), mode_item("Push-to-talk only", "push-to-talk"))),
            menu.SEPARATOR,
            item("Open logs folder", lambda _i, _it: self.actions.open_logs()),
            item("Open config.yaml", lambda _i, _it: self.actions.open_config()),
            item("Open README", lambda _i, _it: self.actions.open_readme()),
            menu.SEPARATOR,
            item("Restart Jarvis", lambda _i, _it: self.actions.restart()),
            item("Quit", lambda _i, _it: self.actions.quit()),
        )

    def start(self) -> None:
        """Create the icon and run it on a background thread."""
        import pystray

        which = self.current()
        self._icon = pystray.Icon(
            "jarvis", self.image(which), tooltip(which, "", self.name), menu=self._menu(pystray)
        )
        self._unsubscribe = self.state.subscribe(lambda _old, _new, _why: self.refresh())
        self._thread = threading.Thread(target=self._run, name="tray", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        try:
            self._icon.run()
        except Exception:  # noqa: BLE001
            log.exception("Tray icon stopped unexpectedly")

    def stop(self) -> None:
        if self._unsubscribe is not None:
            self._unsubscribe()
        if self._icon is not None:
            try:
                self._icon.stop()
            except Exception:  # noqa: BLE001
                pass
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=2)
