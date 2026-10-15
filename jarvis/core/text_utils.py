"""Text helpers for speech: sentence splitting of streamed replies, markdown stripping."""

from __future__ import annotations

import re

# Sentence end: punctuation (plus closing quotes/brackets) followed by whitespace,
# or one or more newlines.
_BOUNDARY = re.compile(r"([.!?…]+[\"'”’)\]]*)(\s+)|(\n+)")

ABBREVIATIONS = {
    "mr", "mrs", "ms", "dr", "prof", "sr", "jr", "st", "vs", "etc", "e.g", "i.e",
    "approx", "no", "fig", "inc", "ltd", "co", "a.m", "p.m", "u.s", "u.k", "mt", "dept",
}


def _ends_with_abbreviation(text: str) -> bool:
    match = re.search(r"(\S+)$", text)
    if not match:
        return False
    word = match.group(1).lstrip("(\"'").lower()
    if word in ABBREVIATIONS:
        return True
    # Initials like "J." in "J. R. R. Tolkien".
    return len(word) == 1 and text[-1:].isalpha() and text[-1:].isupper()


class SentenceSplitter:
    """Accumulates streamed text and yields complete sentences as soon as they end.

    A sentence counts as complete once its closing punctuation is followed by
    whitespace, so "Hello." is released when the next chunk starts. Very short
    sentences are merged with the next one to avoid choppy speech.

    Example:
        >>> s = SentenceSplitter()
        >>> s.feed("Chrome is open. CPU is ")
        ['Chrome is open.']
        >>> s.feed("at 12 percent.")
        []
        >>> s.flush()
        'CPU is at 12 percent.'
    """

    def __init__(self, min_chars: int = 8) -> None:
        self.min_chars = min_chars
        self._buf = ""

    def feed(self, text: str) -> list[str]:
        """Add streamed text; return any sentences that are now complete."""
        self._buf += text
        sentences: list[str] = []
        start = 0
        for match in _BOUNDARY.finditer(self._buf):
            if match.group(1):
                end = match.end(1)
                if _ends_with_abbreviation(self._buf[start : match.start(1)] + match.group(1).rstrip(".")):
                    continue
            else:
                end = match.start(3)
            candidate = self._buf[start:end].strip()
            if not candidate:
                start = match.end()
                continue
            if len(candidate) < self.min_chars and match.group(1):
                continue  # merge with the following sentence
            sentences.append(candidate)
            start = match.end()
        self._buf = self._buf[start:]
        return sentences

    def flush(self) -> str:
        """Return whatever is left (the final, possibly unterminated, sentence)."""
        rest, self._buf = self._buf.strip(), ""
        return rest


def split_sentences(text: str, min_chars: int = 8) -> list[str]:
    """Split a complete text into sentences (convenience wrapper)."""
    splitter = SentenceSplitter(min_chars)
    parts = splitter.feed(text)
    tail = splitter.flush()
    return parts + ([tail] if tail else [])


def strip_markdown(text: str) -> str:
    """Remove markdown so text-to-speech doesn't read out symbols.

    Code blocks are dropped, links keep their label, bare URLs are removed,
    and emphasis, headings, bullets and quote markers are stripped.
    """
    text = re.sub(r"```.*?```", " ", text, flags=re.S)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"<?https?://[^\s>)]+>?", "", text)
    text = re.sub(r"^\s{0,3}#{1,6}\s*", "", text, flags=re.M)
    text = re.sub(r"^\s*>\s?", "", text, flags=re.M)
    text = re.sub(r"^\s*(?:[-*+•]|\d+[.)])\s+", "", text, flags=re.M)
    text = re.sub(r"^\s*([-*_])(\s*\1){2,}\s*$", "", text, flags=re.M)  # horizontal rules
    text = re.sub(r"(\*\*|__)(.+?)\1", r"\2", text)
    text = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"\1", text)
    text = re.sub(r"(?<!\w)_(?!\s)(.+?)(?<!\s)_(?!\w)", r"\1", text)
    text = re.sub(r"~~(.+?)~~", r"\1", text)
    text = text.replace("`", "").replace("*", "")
    text = re.sub(r"(?<!\w)#+", "", text)  # stray hashes, but keep "C#"
    text = re.sub(r"\|", ", ", text)  # table pipes
    text = re.sub(r"[ \t]*\n[ \t]*", " ", text)
    text = re.sub(r"\s{2,}", " ", text)
    text = re.sub(r"\s+([,.!?;:])", r"\1", text)
    return text.strip(" ,")


# ---------------------------------------------------------------------------
# Voice conversation helpers
# ---------------------------------------------------------------------------

EXIT_PHRASES = (
    "thanks", "thank you", "thanks a lot", "thank you very much", "cheers",
    "that's all", "that is all", "that's it", "that'll be all", "that will be all",
    "stop listening", "never mind", "nevermind", "nothing", "no thanks", "no thank you",
    "goodbye", "bye", "bye bye", "good night", "i'm done", "i'm good", "we're done",
    "all good", "that's everything", "no", "nope", "not now",
)
_FILLER_WORDS = {"ok", "okay", "great", "perfect", "cool", "alright", "right", "jarvis", "hey", "so", "oh"}


def _normalize(text: str) -> str:
    text = text.lower().replace("’", "'")
    text = re.sub(r"[^a-z' ]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def is_exit_phrase(text: str) -> bool:
    """True if the utterance just ends the conversation ("thanks", "that's all", "stop listening").

    Only short utterances count, so "thanks, now open Spotify" is still a request.
    """
    words = [w for w in _normalize(text).split() if w not in _FILLER_WORDS]
    if not words or len(words) > 6:
        return False
    phrase = " ".join(words)
    for exit_phrase in EXIT_PHRASES:
        if phrase == exit_phrase:
            return True
        if phrase.startswith(exit_phrase + " "):
            rest = phrase[len(exit_phrase) + 1 :].split()
            if len(rest) <= 2 and not set(rest) & {"open", "play", "search", "set", "what", "how", "tell"}:
                return True  # "thanks a bunch", "that's all for now"
    return False


_WAKE_PREFIX = re.compile(r"^\W*(?:(?:hey|hi|okay|ok)[\s,]+)?jarvis\b[\s,.!?]*", re.IGNORECASE)


def strip_wake_phrase(text: str) -> str:
    """Remove a leading "Hey Jarvis," that slipped into the recording."""
    stripped = _WAKE_PREFIX.sub("", text, count=1).strip()
    return stripped[:1].upper() + stripped[1:] if stripped else ""
