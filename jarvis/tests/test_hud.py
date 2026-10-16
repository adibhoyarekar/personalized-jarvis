"""The J.A.R.V.I.S. HUD server, typed messages, close-to-tray and the tray menu."""

from __future__ import annotations

import http.client
import json
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from core.app import VoiceApp
from core.config import Config
from core.single_instance import ShowRequests, request_show
from core.state import AssistantState as S
from tests.test_voice import FakeAgent, make_assistant
from ui.hud import ASSETS, ChatModel, HudWindow, StatsSampler, browser_command, mic_level
from ui.tray import TrayIcon


def test_chat_model_streams_and_trims() -> None:
    model = ChatModel(limit=3, clock=lambda: datetime(2026, 10, 16, 14, 5, 9))
    model.add_notice("Good afternoon. Jarvis online.")
    model.add_user("  what's the time  ")
    model.add_reply_chunk(" It's ")
    model.add_reply_chunk("five past two.")
    last = model.messages[-1]
    assert (last["role"], last["text"], last["streaming"], last["time"]) == ("assistant", "It's five past two.", True, "14:05:09")
    model.add_user("thanks")  # an interrupted reply is closed by the next request
    assert not model.messages[-2]["streaming"]
    assert [m["role"] for m in model.messages] == ["user", "assistant", "user"]
    model.add_reply_chunk("Anytime.")
    model.finish_reply()
    assert model.messages[-1]["text"] == "Anytime." and not model.messages[-1]["streaming"]
    assert model.add_user("   ") is None and model.add_reply_chunk("") is None


def test_mic_level_and_browser_flags(tmp_path: Path) -> None:
    assert mic_level(0) == 0 and mic_level(1e-4) == 0
    assert 0.3 < mic_level(0.03) < 0.8 and mic_level(1.0) == 1
    cmd = browser_command("msedge.exe", "http://127.0.0.1:5/?t=x", tmp_path, (1920, 1080))
    assert cmd[0] == "msedge.exe" and "--app=http://127.0.0.1:5/?t=x" in cmd
    assert f"--user-data-dir={tmp_path}" in cmd and "--no-first-run" in cmd
    size = next(c for c in cmd if c.startswith("--window-size="))
    w, h = map(int, size.split("=")[1].split(","))
    assert w <= 1920 and h <= 1080


def test_hud_assets_exist() -> None:
    html = (ASSETS / "index.html").read_text(encoding="utf-8")
    assert "/hud.js" in html and "/hud.css" in html and "<title>J.A.R.V.I.S.</title>" in html
    js = (ASSETS / "hud.js").read_text(encoding="utf-8")
    for kind in ("hello", "state", "user", "reply", "done", "notice", "note", "controls", "level", "stats", "tool", "log", "shutdown"):
        assert f"{kind}(" in js, kind
    for element in ("gear", "audio", "dev-in", "dev-out", "test-in", "test-out", "audio-apply", "audio-cancel"):
        assert f'id="{element}"' in html and f'"{element}"' in js, element


def test_stats_sampler_reports_numbers() -> None:
    stats = StatsSampler().sample()
    if not stats:
        pytest.skip("psutil unavailable")
    assert 0 <= stats["cpu"] <= 100 and 0 <= stats["mem"] <= 100 and stats["procs"] > 0


# -- the HUD server ----------------------------------------------------------------------


class HudActions:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.muted = False
        self.closed = threading.Event()

    def submit_text(self, text: str) -> None:
        self.sent.append(text)

    def toggle_mute(self) -> None:
        self.muted = not self.muted

    def is_muted(self) -> bool:
        return self.muted

    def current_mode(self) -> str:
        return "wake"

    def window_closed(self) -> None:
        self.closed.set()

    def audio_devices(self) -> dict[str, Any]:
        return {"input": {"options": [{"index": 3, "name": "Kreo Sonik", "api": "MME"}], "selected": 3, "default": 3},
                "output": {"options": [], "selected": None, "default": None}}

    def set_audio_devices(self, input_device: int | None, output_device: int | None) -> None:
        if input_device == 99:
            raise RuntimeError("That microphone can't be opened.")
        self.devices = (input_device, output_device)

    def test_speaker(self, device: int | None) -> None:
        self.tested = device

    def test_microphone(self, device: int | None) -> float:
        return 0.03


@pytest.fixture
def hud() -> Any:
    actions = HudActions()
    opened: list[str] = []
    window = HudWindow(actions, name="Jarvis", user_name="Aditya", launcher=opened.append, stats=False, close_grace_s=0.2)
    window.start()
    window.opened, window.fake = opened, actions  # type: ignore[attr-defined]
    yield window
    window.stop()


def request(hud: HudWindow, method: str, path: str, body: Any = None, headers: dict[str, str] | None = None) -> tuple[int, bytes]:
    conn = http.client.HTTPConnection("127.0.0.1", hud.port, timeout=5)
    data = json.dumps(body).encode() if body is not None else None
    conn.request(method, path, body=data, headers=headers or {})
    resp = conn.getresponse()
    out = resp.status, resp.read()
    conn.close()
    return out


def post_headers(hud: HudWindow) -> dict[str, str]:
    return {"Content-Type": "application/json", "X-Jarvis-Token": hud.token}


def read_events(resp: Any, count: int = 1000, until: str = "") -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    while len(events) < count:
        line = resp.fp.readline().decode().strip()
        if line.startswith("data: "):
            events.append(json.loads(line[6:]))
            if until and events[-1]["type"] == until:
                break
    return events


def test_hud_opens_window_and_guards_every_route(hud: HudWindow) -> None:
    assert hud.opened == [hud.url] and hud.token in hud.url  # type: ignore[attr-defined]
    assert request(hud, "GET", "/")[0] == 403
    assert request(hud, "GET", "/?t=wrong")[0] == 403
    status, body = request(hud, "GET", f"/?t={hud.token}")
    assert status == 200 and b"J.A.R.V.I.S." in body
    assert request(hud, "GET", "/hud.js")[0] == 200
    assert request(hud, "GET", "/../core/agent.py")[0] in (403, 404)
    # DNS-rebinding guard: a foreign Host header is refused even with the token
    assert request(hud, "GET", f"/?t={hud.token}", headers={"Host": "evil.example"})[0] == 403
    assert request(hud, "POST", "/api/send", {"text": "hi"}, {"Content-Type": "application/json"})[0] == 403
    assert request(hud, "POST", "/api/send", {"text": "hi"}, {"X-Jarvis-Token": hud.token})[0] == 415
    assert hud.fake.sent == []  # type: ignore[attr-defined]


def test_hud_streams_events_and_takes_commands(hud: HudWindow) -> None:
    hud.on_notice("Good afternoon, Aditya.")
    conn = http.client.HTTPConnection("127.0.0.1", hud.port, timeout=5)
    conn.request("GET", f"/events?t={hud.token}")
    resp = conn.getresponse()
    assert resp.status == 200 and resp.getheader("Content-Type").startswith("text/event-stream")
    hello = read_events(resp, 1)[0]
    assert hello["type"] == "hello" and hello["user"] == "Aditya" and hello["messages"][0]["text"] == "Good afternoon, Aditya."
    assert hud.client_count == 1

    hud.on_state(S.IDLE, S.LISTENING, "wake word")
    hud.on_transcript("open notepad")
    hud.on_reply("Opening ")
    hud.on_reply("Notepad.")
    hud.on_reply_done()
    hud.on_status("tool", "open_app")
    kinds = [e["type"] for e in read_events(resp, until="tool")]
    for kind in ("state", "user", "reply", "done", "tool"):
        assert kind in kinds, kinds

    assert request(hud, "POST", "/api/send", {"text": "  what time is it  "}, post_headers(hud))[0] == 204
    assert request(hud, "POST", "/api/mute", {}, post_headers(hud))[0] == 204
    assert hud.fake.sent == ["what time is it"] and hud.fake.muted  # type: ignore[attr-defined]
    controls = read_events(resp, until="controls")[-1]
    assert controls["muted"] is True
    conn.close()


def test_hud_audio_settings_routes(hud: HudWindow) -> None:
    assert request(hud, "GET", "/api/audio")[0] == 403
    status, body = request(hud, "GET", f"/api/audio?t={hud.token}")
    assert status == 200 and json.loads(body)["input"]["selected"] == 3
    h = post_headers(hud)
    assert request(hud, "POST", "/api/audio", {"input": 3}, h)[0] == 400  # both are required
    assert request(hud, "POST", "/api/audio", {"input": "3", "output": None}, h)[0] == 400
    assert request(hud, "POST", "/api/audio", {"input": True, "output": None}, h)[0] == 400
    status, body = request(hud, "POST", "/api/audio", {"input": 3, "output": None}, h)
    assert status == 200 and json.loads(body) == {"ok": True}
    assert hud.fake.devices == (3, None)  # type: ignore[attr-defined]
    status, body = request(hud, "POST", "/api/audio", {"input": 99, "output": 5}, h)
    assert json.loads(body) == {"ok": False, "error": "That microphone can't be opened."}
    assert hud.fake.devices == (3, None)  # type: ignore[attr-defined]
    status, body = request(hud, "POST", "/api/audio/test", {"kind": "output", "device": 5}, h)
    assert json.loads(body)["ok"] and hud.fake.tested == 5  # type: ignore[attr-defined]
    status, body = request(hud, "POST", "/api/audio/test", {"kind": "input", "device": None}, h)
    assert json.loads(body) == {"ok": True, "level": mic_level(0.03)}
    assert request(hud, "POST", "/api/audio/test", {"kind": "camera", "device": 1}, h)[0] == 400


def test_hud_reports_a_closed_window_but_not_a_reload(hud: HudWindow) -> None:
    def connect_once() -> None:
        conn = http.client.HTTPConnection("127.0.0.1", hud.port, timeout=5)
        conn.request("GET", f"/events?t={hud.token}")
        read_events(conn.getresponse(), 1)
        conn.close()

    connect_once()
    connect_once()  # reload: reconnects within the grace period
    assert not hud.fake.closed.wait(0.05)  # type: ignore[attr-defined]
    deadline = time.monotonic() + 3
    while hud.client_count and time.monotonic() < deadline:
        hud.on_state(S.IDLE, S.THINKING)  # a write makes the server notice the dropped socket
        time.sleep(0.05)
        hud.on_state(S.THINKING, S.IDLE)
    assert hud.fake.closed.wait(3)  # type: ignore[attr-defined]


def test_hud_start_hidden_waits_for_show() -> None:
    opened: list[str] = []
    window = HudWindow(HudActions(), launcher=opened.append, start_hidden=True, stats=False)
    window.start()
    try:
        assert opened == []
        window.show()
        assert opened == [window.url]
    finally:
        window.stop()
    window.show()  # after stop: ignored
    assert len(opened) == 1


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
