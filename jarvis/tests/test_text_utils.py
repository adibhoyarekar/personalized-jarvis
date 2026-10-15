"""Tests for sentence splitting of streamed replies and markdown stripping."""

from __future__ import annotations

import pytest

from core.text_utils import SentenceSplitter, split_sentences, strip_markdown


def stream(text: str, chunk: int = 3) -> list[str]:
    """Feed text in small chunks like the API stream does."""
    splitter = SentenceSplitter()
    out: list[str] = []
    for i in range(0, len(text), chunk):
        out.extend(splitter.feed(text[i : i + chunk]))
    tail = splitter.flush()
    return out + ([tail] if tail else [])


def test_sentences_released_as_soon_as_complete() -> None:
    splitter = SentenceSplitter()
    assert splitter.feed("Chrome is open. CPU is") == ["Chrome is open."]
    assert splitter.feed(" at twelve percent") == []
    assert splitter.feed("! Anything else?") == ["CPU is at twelve percent!"]
    assert splitter.flush() == "Anything else?"
    assert splitter.flush() == ""


def test_streaming_chunks_give_same_result_as_whole_text() -> None:
    text = "Done. The weather is mild today. It's 18 degrees, sir. Shall I open Spotify?"
    assert stream(text) == split_sentences(text)
    assert stream(text) == [
        "Done. The weather is mild today.",  # "Done." is short, merged with the next
        "It's 18 degrees, sir.",
        "Shall I open Spotify?",
    ]


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Dr. Smith called at 3 p.m. today. Then he left.", ["Dr. Smith called at 3 p.m. today.", "Then he left."]),
        ("Pi is about 3.14159 and e is 2.718. Neat.", ["Pi is about 3.14159 and e is 2.718.", "Neat."]),
        ("Use e.g. Chrome or Edge. Either works.", ["Use e.g. Chrome or Edge.", "Either works."]),
        ('He said "go now." Then silence.', ['He said "go now."', "Then silence."]),
        ("Wait... really? Yes!", ["Wait... really?", "Yes!"]),
        ("First line here\nSecond line here", ["First line here", "Second line here"]),
        ("No terminal punctuation", ["No terminal punctuation"]),
        ("", []),
    ],
)
def test_split_sentences(text: str, expected: list[str]) -> None:
    assert split_sentences(text) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("**Bold** and *italic* and _under_", "Bold and italic and under"),
        ("## Heading\nText", "Heading Text"),
        ("- one\n- two\n* three", "one two three"),
        ("1. first\n2. second", "first second"),
        ("Run `pip install x` now", "Run pip install x now"),
        ("See [the docs](https://example.com/x) please", "See the docs please"),
        ("Visit https://example.com today.", "Visit today."),
        ("Code:\n```python\nprint(1)\n```\nDone", "Code: Done"),
        ("> quoted text", "quoted text"),
        ("I like C# and snake_case_names", "I like C# and snake_case_names"),
        ("Price is 5 * 3 = 15", "Price is 5 3 = 15"),
        ("Plain sentence, nothing to strip.", "Plain sentence, nothing to strip."),
        ("~~old~~ new", "old new"),
    ],
)
def test_strip_markdown(raw: str, expected: str) -> None:
    assert strip_markdown(raw) == expected
