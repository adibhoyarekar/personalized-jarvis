"""Desktop window logic (no display needed), typed messages, close-to-tray and the tray menu."""

from __future__ import annotations

import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from core.app import VoiceApp
from core.config import Config
from core.single_instance import ShowRequests, request_show
from core.state import AssistantState as S
from tests.test_voice import FakeAgent, make_assistant
from ui.tray import TrayIcon
from ui.window import (
    LOOKS,
    ChatModel,
    JarvisWindow,
    blend,
    hint_text,
    look_key,
    mic_level,
    rounded_rect,
    spectrum,
    status_badge,
)


def test_blend_and_looks() -> None:
    assert blend("#000000", "#ffffff", 0) == "#000000"
    assert blend("#000000", "#ffffff", 1) == "#ffffff"
    assert blend("#000000", "#ffffff", 0.5) == "#808080"
    assert blend("#102030", "#102030", 0.7) == "#102030"
    assert blend("#000000", "#ffffff", 7) == "#ffffff"  # clamped
    assert look_key(S.IDLE) == "idle"
    assert look_key(S.FOLLOW_UP) == look_key(S.WAKE_DETECTED) == "listening"
    assert look_key(S.THINKING) == "thinking" and look_key(S.SPEAKING) == "speaking"
    assert look_key(S.SPEAKING, muted=True) == "muted"
    assert look_key(S.SPEAKING, muted=True, starting=True) == "starting"
    assert look_key(S.SPEAKING, muted=True, starting=True, error=True) == "error"
    assert all(len(look.accent) == 7 for look in LOOKS.values())


def test_hints_and_status_badge() -> None:
    assert hint_text("idle", "Jarvis", "f9", wake=True) == 'Say "Hey Jarvis" or hold F9 to talk.'
    assert hint_text("idle", "Jarvis", "f9", wake=False) == "Hold F9 and speak, or type below."
    assert hint_text("idle", "Jarvis", "", wake=False) == "Type below to talk."
    assert "mic button" in hint_text("muted", "Jarvis", "f9", wake=True)
    assert status_badge("") == ("ONLINE", status_badge("")[1])
    assert status_badge("offline")[0] == "OFFLINE"
    assert status_badge("loading")[0] == status_badge("starting")[0] == "STARTING"
    assert status_badge("offline", error=True)[0] == "ERROR"


def test_geometry_helpers() -> None:
    points = rounded_rect(0, 0, 100, 40, 10)
    xs, ys = points[0::2], points[1::2]
    assert (min(xs), max(xs), min(ys), max(ys)) == (0, 100, 0, 40)
    assert rounded_rect(0, 0, 10, 10, 50)[0] == 5  # radius clamped to half the size
    bars = spectrum(1.3, 0.8, 72)
    assert len(bars) == 72 and all(0 <= b <= 1 for b in bars) and len(set(bars)) > 10
    assert max(spectrum(1.3, 0.0, 16)) == 0
    assert mic_level(0) == 0 and mic_level(1e-4) == 0
    assert 0.3 < mic_level(0.03) < 0.8 and mic_level(1.0) == 1


def test_chat_model_streams_and_trims() -> None:
    model = ChatModel(limit=3, clock=lambda: datetime(2026, 10, 16, 14, 5))
    model.add_notice("Good afternoon. Jarvis online.")
    model.add_user("  what's the time  ")
    model.add_reply_chunk(" It's ")
    model.add_reply_chunk("five past two.")
    last = model.messages[-1]
    assert (last.role, last.text, last.streaming, last.time) == ("assistant", "It's five past two.", True, "14:05")
    model.add_user("thanks")  # an interrupted reply is closed by the next request
    assert not model.messages[-2].streaming
    assert [m.role for m in model.messages] == ["user", "assistant", "user"] and model.trimmed == 1
    model.add_reply_chunk("Anytime.")
    model.finish_reply()
    assert model.messages[-1].text == "Anytime." and not model.messages[-1].streaming
    model.add_user("   ")
    model.add_reply_chunk("")
    assert len(model.messages) == 3 and model.trimmed == 2


def test_window_without_display_stops_cleanly(monkeypatch: Any) -> None:
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    if sys.platform == "win32":
        return  # Windows always has a display
    window = JarvisWindow(actions=None)  # type: ignore[arg-type]
    window.start()
    window.on_state(S.IDLE, S.LISTENING)
    window.stop()
    assert not window._thread.is_alive()  # type: ignore[union-attr]


# -- typed messages through the voice loop ----------------------------------------------


def test_typed_message_is_answered_and_shown() -> None:
    assistant, agent, tts, _rec = make_assistant([])
    heard: list[str] = []
    done: list[bool] = []
    assistant.transcript_listeners.append(heard.append)
    assistant.reply_done_listeners.append(lambda: done.append(True))
    assistant.submit_text("  open notepad ")
    assistant.submit_text("   ")  # ignored
    event = assistant.events.get_nowait()
    assert assistant.events.empty() and (event.kind, event.detail) == ("text", "open notepad")
    assistant.dispatch(event)
    assert agent.asked == ["open notepad"] and heard == ["open notepad"] and done == [True]
    assert tts.spoken and assistant.state.state is S.IDLE


def test_typed_message_interrupts_a_reply() -> None:
    assistant, agent, tts, _rec = make_assistant([])
    assistant._busy.set()
    assistant.submit_text("stop, do something else")
    assert agent.cancelled.is_set() and tts.stops == 1


def test_typed_answer_to_a_confirmation_does_not_cancel() -> None:
    assistant, agent, _tts, _rec = make_assistant([], agent=FakeAgent())
    assistant._busy.set()
    heard: list[str] = []
    notices: list[str] = []
    assistant.transcript_listeners.append(heard.append)
    assistant.notice_listeners.append(notices.append)
    threading.Timer(0.05, lambda: assistant.submit_text("yes")).start()
    assert assistant.confirm("Should I close Chrome?")
    assert not agent.cancelled.is_set() and heard == ["yes"] and notices == ["Should I close Chrome?"]


# -- app: close to tray, typed text before start-up, restart --------------------------------


class FakeTray:
    def __init__(self) -> None:
        self.notes: list[str] = []
        self.notes_set: list[str] = []

    def notify(self, message: str, title: str = "Jarvis") -> None:
        self.notes.append(message)

    def set_status_note(self, note: str) -> None:
        self.notes_set.append(note)

    def set_error(self, error: bool) -> None: ...


class FakeWindow:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    def __getattr__(self, name: str) -> Any:
        return lambda *args: self.calls.append((name, args[0] if args else None))


def make_app(**kwargs: Any) -> VoiceApp:
    return VoiceApp(Config({"assistant": {"name": "Jarvis"}}, Path(".")), print_fn=lambda *a, **k: None, **kwargs)


def test_closing_the_window_hides_to_tray_and_notifies_once() -> None:
    app = make_app(window=True, tray=True)
    app.tray, app.window = FakeTray(), FakeWindow()
    app.window_closed()
    app.window_closed()
    assert [c for c in app.window.calls if c[0] == "hide"] == [("hide", None)] * 2
    assert len(app.tray.notes) == 1 and "Quit" in app.tray.notes[0]
    assert not app._quit.is_set()


def test_closing_the_window_without_a_tray_quits() -> None:
    app = make_app(window=True)
    app.window = FakeWindow()
    app.window_closed()
    assert app._quit.is_set()


def test_typing_before_startup_finishes_explains() -> None:
    app = make_app()
    app.window = FakeWindow()
    app.submit_text("hello")
    assert app.window.calls[0] == ("on_transcript", "hello")
    assert app.window.calls[1][0] == "on_notice" and "starting up" in app.window.calls[1][1]


def test_status_notes_reach_tray_and_window_and_restart_comes_back_on_screen() -> None:
    app = make_app(argv=["--wake", "--tray", "--minimized"])
    app.tray, app.window = FakeTray(), FakeWindow()
    app._set_status_note("offline")
    assert app.tray.notes_set == ["offline"] and ("set_status_note", "offline") in app.window.calls
    app.restart()
    assert app.argv == ["--wake", "--tray"] and app._quit.is_set()
    assert make_app().use_window  # on by default
    assert not VoiceApp(Config({"ui": {"window": False}}, Path("."))).use_window


# -- tray menu and second launches --------------------------------------------------------


class FakeItem:
    def __init__(self, text: Any, action: Any = None, default: bool = False, **_: Any) -> None:
        self.text, self.action, self.default = text, action, default


class FakeMenu:
    SEPARATOR = object()

    def __init__(self, *items: Any) -> None:
        self.items = list(items)


class FakePystray:
    MenuItem = FakeItem
    Menu = FakeMenu


class MenuActions:
    def __init__(self, window: bool) -> None:
        self.window, self.shown = window, 0

    def has_window(self) -> bool:
        return self.window

    def show_window(self) -> None:
        self.shown += 1

    def is_muted(self) -> bool:
        return False

    def current_mode(self) -> str:
        return "wake"


def test_tray_click_opens_the_window_when_there_is_one() -> None:
    from core.state import StateManager

    actions = MenuActions(window=True)
    menu = TrayIcon(StateManager(), actions)._menu(FakePystray)  # type: ignore[arg-type]
    defaults = [i for i in menu.items if isinstance(i, FakeItem) and i.default]
    assert len(defaults) == 1 and defaults[0].text == "Open Jarvis"
    defaults[0].action(None, None)
    assert actions.shown == 1

    menu = TrayIcon(StateManager(), MenuActions(window=False))._menu(FakePystray)  # type: ignore[arg-type]
    defaults = [i for i in menu.items if isinstance(i, FakeItem) and i.default]
    assert len(defaults) == 1 and callable(defaults[0].text)  # the mute toggle


def test_show_requests_are_windows_only() -> None:
    if sys.platform == "win32":
        return
    assert request_show() is False
    listener = ShowRequests(lambda: None)
    listener.start()
    listener.stop()
