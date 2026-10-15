"""Safety gate: confirmation for dangerous tools and an audit log of every tool call."""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger(__name__)

ConfirmFn = Callable[[str], bool]

_YES = {"y", "yes", "yeah", "yep", "sure", "ok", "okay", "do it", "go ahead", "confirm", "affirmative"}


def parse_yes_no(answer: str | None) -> bool:
    """Interpret a typed or spoken answer. Anything not clearly "yes" counts as no."""
    if not answer:
        return False
    text = answer.strip().lower().rstrip(".!?")
    return text in _YES or text.startswith(("yes ", "yeah ", "sure ", "go ahead"))


def describe_call(name: str, args: dict[str, Any]) -> str:
    """Human-friendly one-line description of a tool call, used in confirmations."""
    if not args:
        return name.replace("_", " ")
    shown = ", ".join(f"{k}={v!r}" for k, v in args.items())
    if len(shown) > 200:
        shown = shown[:200] + "..."
    return f"{name.replace('_', ' ')} ({shown})"


class SafetyGate:
    """Decides whether a tool call may run and records every call.

    Args:
        confirm: Asks the user a yes/no question and returns their answer.
            Text mode types it; voice mode speaks it and listens.
        audit_log: JSONL file receiving one line per tool call.
        confirm_dangerous: When False, dangerous tools run without asking.
    """

    def __init__(
        self,
        confirm: ConfirmFn,
        audit_log: Path,
        confirm_dangerous: bool = True,
    ) -> None:
        self.confirm = confirm
        self.audit_log = audit_log
        self.confirm_dangerous = confirm_dangerous
        self._lock = threading.Lock()
        audit_log.parent.mkdir(parents=True, exist_ok=True)

    def approve(self, name: str, args: dict[str, Any], dangerous: bool) -> bool:
        """Return True if the call may proceed, asking the user when required."""
        if not dangerous or not self.confirm_dangerous:
            return True
        question = f"Should I {describe_call(name, args)}?"
        try:
            approved = bool(self.confirm(question))
        except Exception:  # noqa: BLE001 - a broken prompt must fail closed
            log.exception("Confirmation prompt failed; denying %s", name)
            approved = False
        log.info("Confirmation for %s: %s", name, "approved" if approved else "denied")
        if not approved:
            self.record(name, args, approved=False, result="denied by user", is_error=True)
        return approved

    def record(
        self,
        name: str,
        args: dict[str, Any],
        *,
        approved: bool,
        result: str,
        is_error: bool,
        duration_s: float = 0.0,
    ) -> None:
        """Append one tool call to the audit log (never raises)."""
        entry = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "tool": name,
            "args": args,
            "approved": approved,
            "is_error": is_error,
            "duration_s": round(duration_s, 3),
            "result": result[:500],
        }
        try:
            with self._lock, self.audit_log.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
        except OSError:
            log.exception("Could not write tool audit log %s", self.audit_log)
