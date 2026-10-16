"""Offline router, connectivity monitor, overlay fading, greeting, startup task XML, mute/mode."""

from __future__ import annotations

import datetime as dt
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from core.app import greeting_text
from core.config import Config
from core.connectivity import ConnectivityMonitor
from core.offline import OfflineRouter
from core.startup import command_line, task_xml
from core.state import AssistantState as S
from ui.overlay import BASE_ALPHA, OverlayModel


@pytest.mark.parametrize(
    "text,tool,args",
    [
        ("Set a timer for ten minutes", "set_timer", {"minutes": 10.0, "seconds": 0}),
        ("timer for 90 seconds", "set_timer", {"minutes": 1.5, "seconds": 0}),
        ("Hey Jarvis, set the volume to 40 percent", "set_volume", {"level": 40}),
        ("mute", "mute", {}),
        ("unmute", "unmute", {}),
        ("Pause the music.", "play_pause", {}),
        ("next track", "next_track", {}),
        ("Lock my computer", "lock_pc", {}),
        ("Take a screenshot please", "screenshot", {}),
        ("Open Chrome", "open_app", {"name": "chrome"}),
    ],
)
def test_offline_router_matches(text: str, tool: str, args: dict) -> None:
    cmd = OfflineRouter().match(text)
    assert cmd is not None and cmd.tool == tool and cmd.args == args


@pytest.mark.parametrize("text", ["write a poem", "what's the weather", "start the cs-events dev server",
                                  "mute the mic", "open the news", "remind me to call mom"])
def test_offline_router_ignores_complex_requests(text: str) -> None:
    assert OfflineRouter().match(text) is None


def test_offline_router_answers_time_locally() -> None:
    cmd = OfflineRouter().match("what time is it?")
    assert cmd is not None and cmd.tool is None and cmd.answer.startswith("It's ")


def test_connectivity_monitor_transitions_once() -> None:
    status = {"value": "offline"}
    events: list[str] = []
    mon = ConnectivityMonitor(lambda: status["value"], on_offline=lambda: events.append("off"),
                              on_online=lambda: events.append("on"))
    assert mon.check_now() is False and not mon.online
    mon.check_now()  # still offline: no second notice
    status["value"] = "ok"
    assert mon.check_now() is True
    status["value"] = "auth"  # a bad key is not "offline"
    assert mon.check_now() is True
    assert events == ["off", "on"]


def test_overlay_fades_after_idle() -> None:
    model = OverlayModel(fade_s=4.0)
    assert model.alpha(0) == 0.0  # hidden until something happens
    model.on_state(S.LISTENING, 0)
    model.on_transcript("what's my CPU usage")
    model.on_state(S.SPEAKING, 1)
    model.on_reply("CPU is at ")
    model.on_reply("12 percent.")
    assert model.lines() == ("Speaking", "You: what's my CPU usage", "Jarvis: CPU is at 12 percent.")
    assert model.alpha(100) == BASE_ALPHA  # stays while active
    model.on_state(S.IDLE, 10)
    assert model.alpha(13.9) == BASE_ALPHA
    assert 0 < model.alpha(14.3) < BASE_ALPHA
    assert model.alpha(15.0) == 0.0
    model.on_state(S.LISTENING, 20)  # next request clears the old text
    assert model.lines()[1:] == ("", "")


def test_greeting_text() -> None:
    cfg = Config({"assistant": {"user_name": "Aditya"}, "startup": {"greeting": "Jarvis online."}}, Path("."))
    assert greeting_text(cfg, dt.datetime(2026, 10, 16, 8)) == "Good morning, Aditya. Jarvis online."
    assert greeting_text(cfg, dt.datetime(2026, 10, 16, 14)).startswith("Good afternoon")
    assert greeting_text(cfg, dt.datetime(2026, 10, 16, 21)).startswith("Good evening")
    plain = Config({"startup": {"greeting": "Ready.", "time_greeting": False}}, Path("."))
    assert greeting_text(plain) == "Ready."
    silent = Config({"startup": {"greeting": "", "time_greeting": False}}, Path("."))
    assert greeting_text(silent) == ""


def test_startup_task_xml_and_run_key() -> None:
    pyw = Path("C:/Users/Me/jarvis/.venv/Scripts/pythonw.exe")
    main = Path("C:/Users/Me/jarvis/main.py")
    xml = task_xml(pyw, main, main.parent, "DESKTOP\\Me & Co")
    root = ET.fromstring(xml.split("\n", 1)[1])  # skip the UTF-16 declaration line
    ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
    assert root.find(".//t:LogonTrigger/t:UserId", ns).text == "DESKTOP\\Me & Co"  # escaped & round-trips
    assert root.find(".//t:Exec/t:Command", ns).text == str(pyw)
    assert root.find(".//t:Exec/t:Arguments", ns).text == f'"{main}" --wake --tray'
    assert root.find(".//t:Exec/t:WorkingDirectory", ns).text == str(main.parent)
    assert root.find(".//t:Settings/t:Hidden", ns).text == "true"
    assert root.find(".//t:Settings/t:DisallowStartIfOnBatteries", ns).text == "false"
    assert command_line(pyw, main) == f'"{pyw}" "{main}" --wake --tray'


def test_voice_mute_and_mode_switch() -> None:
    from tests.test_voice import FakePipeline, make_assistant
    from core.audio_in import AudioEvent, ListenMode

    pipeline = FakePipeline()
    assistant, agent, _tts, rec = make_assistant(["What time is it?"], pipeline=pipeline)
    assistant.set_muted(True)
    assert assistant.muted and pipeline.modes[-1] is ListenMode.OFF
    assistant._on_press()  # push-to-talk ignored while muted
    assert not rec.recording
    assistant.dispatch(AudioEvent("wake"))  # a stray wake event too
    assert assistant.state.state is S.IDLE and pipeline.modes[-1] is ListenMode.OFF
    assistant.set_muted(False)
    assistant.dispatch(assistant.events.get_nowait())  # mute_changed -> back to wake listening
    assistant.dispatch(assistant.events.get_nowait())
    assert pipeline.modes[-1] is ListenMode.WAKE

    assert assistant.set_wake_active(False) and not assistant.wake_enabled
    while not assistant.events.empty():
        assistant.dispatch(assistant.events.get_nowait())
    assert pipeline.modes[-1] is ListenMode.OFF  # push-to-talk only: wake word off
    assistant.dispatch(AudioEvent("utterance", __import__("numpy").ones(16000, dtype="float32"), "push-to-talk"))
    assert agent.asked == ["What time is it?"]
    assert assistant.state.state is S.IDLE  # no follow-up window in push-to-talk mode

    no_wake, *_ = make_assistant([], pipeline=FakePipeline(wake=False))
    assert not no_wake.set_wake_active(True)  # can't switch on without a wake model


def test_voice_stop_ends_run() -> None:
    import threading

    from tests.test_voice import FakePipeline, make_assistant

    assistant, *_ = make_assistant([], pipeline=FakePipeline(), hotkey=None)
    ready: list[bool] = []
    thread = threading.Thread(target=assistant.run, kwargs={"on_ready": lambda: ready.append(True)})
    thread.start()
    assistant.stop()
    thread.join(timeout=3)
    assert not thread.is_alive() and ready == [True]
