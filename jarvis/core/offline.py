"""Degraded mode: understand a few common commands locally when Claude is unreachable.

Only simple, unambiguous phrasings are matched (timers, volume, media keys,
opening apps, the time). Everything still goes through the safety gate.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from typing import Any, Callable

_NUMBER_WORDS = {
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
    "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "fifteen": 15, "twenty": 20, "thirty": 30,
    "forty": 40, "forty five": 45, "fifty": 50, "sixty": 60, "ninety": 90, "half an": 0.5, "half a": 0.5,
}
_NUM = r"(\d+(?:\.\d+)?|" + "|".join(sorted((re.escape(w) for w in _NUMBER_WORDS), key=len, reverse=True)) + r")"


def _num(token: str) -> float:
    return float(token) if token[0].isdigit() else float(_NUMBER_WORDS[token])


@dataclass
class LocalCommand:
    """What to do for a matched utterance: run ``tool`` with ``args``, or just say ``answer``."""

    tool: str | None = None
    args: dict[str, Any] = field(default_factory=dict)
    answer: str | None = None


Rule = tuple[re.Pattern[str], Callable[[re.Match[str]], LocalCommand]]

_NOT_APPS = re.compile(r"\b(project|server|reminder|timer|briefing|music|song|track|video|the news)\b")


def _timer(m: re.Match[str]) -> LocalCommand:
    amount, unit = _num(m.group(1)), m.group(2)
    seconds = amount * {"second": 1, "minute": 60, "hour": 3600}[unit]
    return LocalCommand("set_timer", {"minutes": round(seconds / 60, 3), "seconds": 0})


def _open(m: re.Match[str]) -> LocalCommand | None:  # type: ignore[return]
    name = m.group(2).strip()
    if _NOT_APPS.search(name):
        return None  # type: ignore[return-value]
    return LocalCommand("open_app", {"name": name.removeprefix("the ").removesuffix(" app")})


RULES: list[Rule] = [
    (re.compile(r"\bwhat(?:'s| is)? (?:the )?time\b|\bwhat time is it\b"),
     lambda m: LocalCommand(answer=f"It's {dt.datetime.now().strftime('%I:%M %p').lstrip('0')}.")),
    (re.compile(r"\bwhat(?:'s| is) (?:the |today's )?(?:date|day)\b|\bwhat day is it\b"),
     lambda m: LocalCommand(answer=f"It's {dt.datetime.now().strftime('%A %d %B')}.")),
    (re.compile(rf"\btimer (?:for )?{_NUM} (second|minute|hour)s?\b"), _timer),
    (re.compile(rf"\b{_NUM} (second|minute|hour)s? timer\b"), _timer),
    (re.compile(r"\b(?:set )?(?:the )?volume (?:to |at )?(\d{1,3})(?: percent)?\b"),
     lambda m: LocalCommand("set_volume", {"level": int(m.group(1))})),
    (re.compile(r"^(?:un ?mute|turn (?:the )?sound (?:back )?on)\b"), lambda m: LocalCommand("unmute")),
    (re.compile(r"^mute\b(?! (?:the )?mic)"), lambda m: LocalCommand("mute")),
    (re.compile(r"^(?:pause|resume|play)(?: (?:the )?(?:music|song|track|video))?$"),
     lambda m: LocalCommand("play_pause")),
    (re.compile(r"^(?:next|skip)(?: (?:the )?(?:track|song))?$"), lambda m: LocalCommand("next_track")),
    (re.compile(r"^(?:previous|last)(?: (?:track|song))?$"), lambda m: LocalCommand("previous_track")),
    (re.compile(r"\block (?:my |the )?(?:pc|computer|screen|laptop)\b"), lambda m: LocalCommand("lock_pc")),
    (re.compile(r"\b(?:take a )?screenshot\b"), lambda m: LocalCommand("screenshot")),
    (re.compile(r"^(open|launch|start) (.+)$"), _open),  # type: ignore[list-item]
]


def normalize(text: str) -> str:
    text = text.lower().replace("’", "'")
    text = re.sub(r"^(?:hey |ok |okay )?jarvis[, ]*", "", text.strip())
    text = re.sub(r"\b(?:please|can you|could you|would you)\b", " ", text)
    text = re.sub(r"[^a-z0-9'. ]", " ", text)
    text = re.sub(r"\s+", " ", text).strip(" .")
    return text


class OfflineRouter:
    """Maps a handful of phrasings to tool calls without the LLM."""

    def __init__(self, rules: list[Rule] | None = None) -> None:
        self.rules = rules or RULES

    def match(self, text: str) -> LocalCommand | None:
        clean = normalize(text)
        for pattern, build in self.rules:
            m = pattern.search(clean)
            if m:
                command = build(m)
                if command is not None:
                    return command
        return None


OFFLINE_HELP = (
    "I can't reach my brain in the cloud right now, so I can only do simple things: "
    "the time, timers, volume, music controls, opening apps, screenshots and locking the PC."
)
