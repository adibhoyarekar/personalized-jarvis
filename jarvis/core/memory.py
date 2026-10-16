"""Long-term memory in SQLite: facts, preferences, command history, todos, reminders.

One database file (``data/jarvis.db``) holds everything that must survive a
restart. All methods are thread-safe: tools run on worker threads and the
reminder scheduler polls from its own thread.
"""

from __future__ import annotations

import datetime as dt
import difflib
import json
import logging
import re
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

log = logging.getLogger(__name__)

HISTORY_LIMIT = 200

_SCHEMA = """
CREATE TABLE IF NOT EXISTS facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT NOT NULL UNIQUE,
    value TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS preferences (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS command_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    text TEXT NOT NULL,
    reply TEXT NOT NULL DEFAULT '',
    tools TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS todos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    text TEXT NOT NULL,
    created_at TEXT NOT NULL,
    done_at TEXT
);
CREATE TABLE IF NOT EXISTS reminders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL DEFAULT 'reminder',
    text TEXT NOT NULL,
    due_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    fired_at TEXT,
    cancelled_at TEXT
);
CREATE TABLE IF NOT EXISTS app_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_reminders_due ON reminders(due_at) WHERE fired_at IS NULL AND cancelled_at IS NULL;
"""

_STOPWORDS = {
    "a", "an", "the", "is", "are", "was", "were", "be", "to", "of", "and", "or", "in", "on", "at", "for",
    "my", "me", "i", "you", "your", "it", "its", "this", "that", "what", "whats", "what's", "who", "where",
    "when", "how", "do", "does", "did", "can", "could", "would", "should", "please", "jarvis", "tell",
    "about", "with", "from", "by", "as", "so", "up", "remember", "recall", "know", "s",
}


def _now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def tokens(text: str) -> set[str]:
    """Lower-case content words, with a light plural strip ("keys" -> "key")."""
    words = re.findall(r"[a-z0-9]+", text.lower().replace("'", ""))
    out = set()
    for word in words:
        if word in _STOPWORDS or len(word) < 2:
            continue
        if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
            word = word[:-1]
        out.add(word)
    return out


def normalize_key(key: str) -> str:
    return re.sub(r"\s+", " ", key.strip().lower().rstrip(".?!"))


@dataclass
class Fact:
    key: str
    value: str
    updated_at: str
    score: float = 0.0

    def as_sentence(self) -> str:
        return f"{self.key}: {self.value}"


@dataclass
class Reminder:
    id: int
    kind: str
    text: str
    due_at: dt.datetime

    def describe(self) -> str:
        return f"{self.text} at {self.due_at.strftime('%I:%M %p').lstrip('0')}"


class Memory:
    """SQLite-backed store. Use ``Memory(":memory:")`` in tests."""

    def __init__(self, path: Path | str) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._conn.executescript(_SCHEMA)
        log.info("Memory database: %s", self.path)

    @classmethod
    def from_config(cls, config: Any) -> "Memory":
        return cls(config.path("paths.data_dir", "data") / "jarvis.db")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _query(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def _exec(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, params)

    # -- facts ------------------------------------------------------------------

    def remember(self, key: str, value: str) -> bool:
        """Store or update a fact. Returns True if it replaced an existing value."""
        key, value = normalize_key(key), value.strip()
        if not key or not value:
            raise ValueError("Both a key and a value are needed to remember something")
        now = _now()
        existed = bool(self._query("SELECT 1 FROM facts WHERE key = ?", (key,)))
        self._exec(
            "INSERT INTO facts (key, value, created_at, updated_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (key, value, now, now),
        )
        return existed

    def all_facts(self) -> list[Fact]:
        rows = self._query("SELECT key, value, updated_at FROM facts ORDER BY updated_at DESC")
        return [Fact(r["key"], r["value"], r["updated_at"]) for r in rows]

    def _score(self, query: str, fact: Fact) -> float:
        q = tokens(query)
        key_tokens, value_tokens = tokens(fact.key), tokens(fact.value)
        overlap = len(q & key_tokens) * 2.0 + len(q & value_tokens)
        # Fuzzy: "wi-fi pasword" still finds "wifi password".
        fuzzy = 0.0
        for word in q:
            for target in key_tokens | value_tokens:
                if word != target and len(word) > 3 and difflib.SequenceMatcher(None, word, target).ratio() >= 0.8:
                    fuzzy += 0.75
                    break
        whole = difflib.SequenceMatcher(None, normalize_key(query), fact.key).ratio()
        return overlap + fuzzy + (whole if whole >= 0.6 else 0.0)

    def recall(self, query: str, limit: int = 5) -> list[Fact]:
        """Facts matching ``query`` by keyword overlap plus fuzzy matching, best first."""
        scored = []
        for fact in self.all_facts():
            fact.score = self._score(query, fact)
            if fact.score > 0:
                scored.append(fact)
        scored.sort(key=lambda f: (f.score, f.updated_at), reverse=True)
        return scored[:limit]

    def relevant_facts(self, utterance: str, limit: int = 5) -> list[Fact]:
        """Top facts by keyword overlap with the utterance (for the system prompt)."""
        q = tokens(utterance)
        if not q:
            return []
        scored = []
        for fact in self.all_facts():
            overlap = len(q & tokens(fact.key)) * 2 + len(q & tokens(fact.value))
            if overlap:
                fact.score = float(overlap)
                scored.append(fact)
        scored.sort(key=lambda f: (f.score, f.updated_at), reverse=True)
        return scored[:limit]

    def forget(self, key: str) -> str | None:
        """Delete a fact by exact key, or the single best fuzzy match. Returns the key removed."""
        norm = normalize_key(key)
        if self._exec("DELETE FROM facts WHERE key = ?", (norm,)).rowcount:
            return norm
        matches = self.recall(key, limit=2)
        if matches and matches[0].score >= 2.0 and (len(matches) == 1 or matches[0].score > matches[1].score):
            self._exec("DELETE FROM facts WHERE key = ?", (matches[0].key,))
            return matches[0].key
        return None

    # -- preferences --------------------------------------------------------------

    def set_preference(self, key: str, value: str) -> None:
        self._exec(
            "INSERT INTO preferences (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (normalize_key(key), value.strip(), _now()),
        )

    def preferences(self) -> dict[str, str]:
        return {r["key"]: r["value"] for r in self._query("SELECT key, value FROM preferences ORDER BY key")}

    def delete_preference(self, key: str) -> bool:
        return bool(self._exec("DELETE FROM preferences WHERE key = ?", (normalize_key(key),)).rowcount)

    def prompt_context(self, utterance: str, limit: int = 5) -> list[str]:
        """Lines for the system prompt: all preferences + the most relevant facts."""
        lines = [f"preference - {k}: {v}" for k, v in self.preferences().items()]
        lines += [f.as_sentence() for f in self.relevant_facts(utterance, limit)]
        return lines

    # -- command history ----------------------------------------------------------

    def add_command(self, text: str, reply: str = "", tools: list[dict[str, Any]] | None = None) -> None:
        """Log a handled request and keep only the newest ``HISTORY_LIMIT``."""
        self._exec(
            "INSERT INTO command_history (text, reply, tools, created_at) VALUES (?, ?, ?, ?)",
            (text, reply[:1000], json.dumps(tools or [], default=str)[:4000], _now()),
        )
        self._exec(
            "DELETE FROM command_history WHERE id NOT IN "
            "(SELECT id FROM command_history ORDER BY id DESC LIMIT ?)",
            (HISTORY_LIMIT,),
        )

    def recent_commands(self, limit: int = 5) -> list[dict[str, Any]]:
        rows = self._query("SELECT * FROM command_history ORDER BY id DESC LIMIT ?", (limit,))
        return [
            {"text": r["text"], "reply": r["reply"], "tools": json.loads(r["tools"]), "at": r["created_at"]}
            for r in rows
        ]

    # -- todos --------------------------------------------------------------------

    def add_todo(self, text: str) -> int:
        cur = self._exec("INSERT INTO todos (text, created_at) VALUES (?, ?)", (text.strip(), _now()))
        return int(cur.lastrowid)

    def todos(self, include_done: bool = False) -> list[dict[str, Any]]:
        sql = "SELECT * FROM todos" + ("" if include_done else " WHERE done_at IS NULL") + " ORDER BY id"
        return [dict(r) for r in self._query(sql)]

    def complete_todo(self, item: str) -> dict[str, Any] | None:
        """Mark a todo done by its number in the open list, id, or best text match."""
        open_items = self.todos()
        target = None
        if item.strip().isdigit():
            n = int(item)
            target = next((t for t in open_items if t["id"] == n), None)
            if target is None and 1 <= n <= len(open_items):
                target = open_items[n - 1]
        if target is None:
            texts = [t["text"].lower() for t in open_items]
            hit = next((t for t in open_items if item.lower() in t["text"].lower()), None)
            if hit is None:
                close = difflib.get_close_matches(item.lower(), texts, n=1, cutoff=0.5)
                hit = open_items[texts.index(close[0])] if close else None
            target = hit
        if target is None:
            return None
        self._exec("UPDATE todos SET done_at = ? WHERE id = ?", (_now(), target["id"]))
        return target

    # -- reminders and timers -----------------------------------------------------

    def add_reminder(self, text: str, due_at: dt.datetime, kind: str = "reminder") -> int:
        cur = self._exec(
            "INSERT INTO reminders (kind, text, due_at, created_at) VALUES (?, ?, ?, ?)",
            (kind, text.strip(), due_at.isoformat(timespec="seconds"), _now()),
        )
        return int(cur.lastrowid)

    def _reminders(self, where: str, params: tuple[Any, ...] = ()) -> list[Reminder]:
        rows = self._query(
            f"SELECT id, kind, text, due_at FROM reminders WHERE fired_at IS NULL AND cancelled_at IS NULL {where} "
            "ORDER BY due_at",
            params,
        )
        return [Reminder(r["id"], r["kind"], r["text"], dt.datetime.fromisoformat(r["due_at"])) for r in rows]

    def pending_reminders(self) -> list[Reminder]:
        return self._reminders("")

    def due_reminders(self, now: dt.datetime | None = None) -> list[Reminder]:
        now = now or dt.datetime.now()
        return self._reminders("AND due_at <= ?", (now.isoformat(timespec="seconds"),))

    def reminders_between(self, start: dt.datetime, end: dt.datetime) -> list[Reminder]:
        return self._reminders(
            "AND due_at >= ? AND due_at < ?", (start.isoformat(timespec="seconds"), end.isoformat(timespec="seconds"))
        )

    def mark_fired(self, reminder_id: int) -> None:
        self._exec("UPDATE reminders SET fired_at = ? WHERE id = ?", (_now(), reminder_id))

    def cancel_reminder(self, reminder_id: int) -> bool:
        return bool(
            self._exec(
                "UPDATE reminders SET cancelled_at = ? WHERE id = ? AND fired_at IS NULL AND cancelled_at IS NULL",
                (_now(), reminder_id),
            ).rowcount
        )

    # -- internal app state (never shown to the model) ---------------------------

    def get_state(self, key: str, default: str | None = None) -> str | None:
        rows = self._query("SELECT value FROM app_state WHERE key = ?", (key,))
        return rows[0]["value"] if rows else default

    def set_state(self, key: str, value: str) -> None:
        self._exec(
            "INSERT INTO app_state (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    def iter_tables(self) -> Iterator[str]:  # pragma: no cover - debugging helper
        for row in self._query("SELECT name FROM sqlite_master WHERE type='table'"):
            yield row["name"]
