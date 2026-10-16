"""Memory tools: remember, recall, forget, list, and recent commands for "do that again"."""

from __future__ import annotations

from typing import Literal

from tools import get_memory, tool


@tool
def remember(key: str, value: str, kind: Literal["fact", "preference"] = "fact") -> str:
    """Save something the user wants remembered ("remember that my wifi password is ...").

    Use a short, reusable key and put the detail in value. Preferences (how the
    user likes things done) are always kept in mind; facts are recalled when relevant.

    Args:
        key: Short label, e.g. "wifi password", "mom's birthday", "favourite pizza".
        value: The thing to remember, e.g. "14 March".
        kind: "fact" or "preference" (e.g. "temperature units": "celsius").
    """
    memory = get_memory()
    if kind == "preference":
        memory.set_preference(key, value)
        return f"Noted as a preference: {key} is {value}."
    replaced = memory.remember(key, value)
    return f"{'Updated' if replaced else 'Remembered'}: {key} is {value}."


@tool
def recall(query: str) -> dict:
    """Look up saved memories matching a question ("what's my wifi password?").

    Args:
        query: What to look for, in the user's words.
    """
    facts = get_memory().recall(query)
    if not facts:
        return {"matches": "Nothing saved about that."}
    return {"matches": [{"key": f.key, "value": f.value, "saved": f.updated_at[:10]} for f in facts]}


@tool(dangerous=True, summary="forget what I know about {key}")
def forget(key: str) -> str:
    """Delete a saved memory or preference.

    Args:
        key: The memory's key (or close to it), e.g. "wifi password".
    """
    memory = get_memory()
    removed = memory.forget(key)
    if removed is None and memory.delete_preference(key):
        removed = key
    return f"Forgotten: {removed}." if removed else f"I had nothing saved under '{key}'."


@tool
def list_memories() -> dict:
    """List everything saved: facts and preferences."""
    memory = get_memory()
    facts = [{"key": f.key, "value": f.value} for f in memory.all_facts()]
    prefs = memory.preferences()
    if not facts and not prefs:
        return {"memories": "Nothing saved yet."}
    return {"facts": facts, "preferences": prefs}


@tool
def recent_commands(limit: int = 5) -> dict:
    """The user's most recent requests and which tools ran, newest first.

    Use for "do that again", "repeat the last thing", or "what did I ask earlier?".

    Args:
        limit: How many (1-20).
    """
    return {"recent": get_memory().recent_commands(max(1, min(int(limit), 20)))}
