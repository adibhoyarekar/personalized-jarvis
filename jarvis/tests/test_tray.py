"""Tray icon: state -> colour mapping, icon drawing, tooltip, menu-driven refresh."""

from __future__ import annotations

from typing import Any

import pytest

from core.state import AssistantState as S
from core.state import StateManager
from ui.tray import COLORS, DisplayState, TrayIcon, display_state, make_icon, state_color, tooltip

GRAY, GREEN, AMBER, BLUE, RED, DARK_RED = (128, 128, 128), (46, 170, 80), (240, 170, 0), (40, 120, 230), (220, 40, 40), (120, 0, 0)


@pytest.mark.parametrize(
    "state,muted,error,color",
    [
        (S.IDLE, False, False, GRAY),
        (S.WAKE_DETECTED, False, False, GREEN),
        (S.LISTENING, False, False, GREEN),
        (S.FOLLOW_UP, False, False, GREEN),
        (S.THINKING, False, False, AMBER),
        (S.SPEAKING, False, False, BLUE),
        (S.IDLE, True, False, RED),
        (S.SPEAKING, True, False, RED),        # mute wins over the assistant state
        (S.THINKING, True, True, DARK_RED),     # an error wins over everything
        (S.IDLE, False, True, DARK_RED),
    ],
)
def test_state_to_color(state: S, muted: bool, error: bool, color: tuple[int, int, int]) -> None:
    assert state_color(state, muted, error) == color


def test_every_display_state_has_a_distinct_color() -> None:
    assert set(COLORS) == set(DisplayState)
    assert len(set(COLORS.values())) == len(COLORS)


def test_icons_are_drawn_in_the_state_color() -> None:
    for which, color in COLORS.items():
        img = make_icon(color, size=64, muted=which is DisplayState.MUTED)
        assert img.size == (64, 64) and img.mode == "RGBA"
        r, g, b, a = img.getpixel((14, 32))  # inside the circle, left of the letter
        assert a == 255 and max(abs(r - color[0]), abs(g - color[1]), abs(b - color[2])) <= 3
        assert img.getpixel((0, 0))[3] == 0  # transparent corners


def test_tooltip_truncates_to_windows_limit() -> None:
    assert tooltip(DisplayState.IDLE, "") == "Jarvis: Idle"
    assert tooltip(DisplayState.SPEAKING, "what's my CPU usage") == "Jarvis: Speaking\nLast: what's my CPU usage"
    long = tooltip(DisplayState.THINKING, "x" * 300)
    assert len(long) == 127 and long.endswith("...")


class FakeActions:
    def __init__(self) -> None:
        self.muted = False

    def is_muted(self) -> bool:
        return self.muted

    def toggle_mute(self) -> None:
        self.muted = not self.muted

    def current_mode(self) -> str:
        return "wake"


class FakePystrayIcon:
    def __init__(self) -> None:
        self.icon: Any = None
        self.title = ""
        self.menu_updates = 0

    def update_menu(self) -> None:
        self.menu_updates += 1


def test_tray_follows_state_mute_and_errors() -> None:
    sm = StateManager()
    actions = FakeActions()
    tray = TrayIcon(sm, actions)  # type: ignore[arg-type]
    tray._icon = FakePystrayIcon()
    sm.subscribe(lambda *_: tray.refresh())

    def shown() -> tuple[int, int, int]:
        return tray._icon.icon.getpixel((14, 32))[:3]

    sm.set(S.LISTENING)
    assert tray.current() is DisplayState.LISTENING and shown() == GREEN
    sm.set(S.THINKING)
    sm.set(S.SPEAKING)
    assert shown() == BLUE
    tray.set_last_command("open notepad")
    assert tray._icon.title == "Jarvis: Speaking\nLast: open notepad"
    actions.toggle_mute()
    tray.refresh()
    assert tray.current() is DisplayState.MUTED and shown() == RED
    tray.set_error(True)
    assert tray.current() is DisplayState.ERROR and shown() == DARK_RED
    tray.set_error(False)
    actions.toggle_mute()
    sm.set(S.IDLE)
    assert shown() == GRAY
