"""Safety gate: shell-command policy, confirmation of dangerous tools, audit log.

Every tool call passes through :meth:`SafetyGate.review` before it runs:

1. Tools with a ``shell_arg`` have their command checked by :class:`ShellPolicy`.
   Denylisted commands (``format``, ``rm -rf``, system folders, ...) are
   always blocked. Commands whose program isn't on the allowlist are blocked.
   Allowlisted commands still need a yes unless ``auto_approve_allowlist``.
2. Tools marked ``dangerous=True`` need a spoken or typed yes.
3. Every call, run or not, is written to ``logs/tool_calls.log`` (rotating).
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import re
import shlex
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger(__name__)

ConfirmFn = Callable[[str], bool]

# ---------------------------------------------------------------------------
# Yes / no
# ---------------------------------------------------------------------------

ACCEPT = ("yes", "yeah", "yep", "yup", "confirm", "confirmed", "do it", "go ahead", "sure", "ok", "okay",
          "affirmative", "please do", "proceed")
REJECT = ("no", "nope", "cancel", "stop", "don't", "dont", "do not", "not", "never", "abort", "wait", "negative")


def _normalize(answer: str) -> str:
    text = answer.lower().replace("’", "'")
    text = re.sub(r"[^a-z' ]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def parse_yes_no(answer: str | None) -> bool:
    """True only for a clear yes. Rejections, anything unclear, or silence mean no.

    Accepts "yes", "yeah", "confirm", "do it", "go ahead" (and close variants);
    any reject word ("no", "cancel", "stop", "don't") anywhere wins over a yes,
    so "yes, no wait" is a no.
    """
    if not answer:
        return False
    text = _normalize(answer)
    if not text:
        return False
    padded = f" {text} "
    if any(f" {word} " in padded for word in REJECT):
        return False
    words = text.split()
    for phrase in ACCEPT:
        if text == phrase or text.startswith(phrase + " "):
            # "yes please", "go ahead jarvis" - allow a few trailing words only.
            return len(words) - len(phrase.split()) <= 3
    return False


# ---------------------------------------------------------------------------
# Shell policy
# ---------------------------------------------------------------------------

DEFAULT_ALLOWLIST = ["git", "npm", "npx", "python", "pip", "node", "ls", "dir"]

#: Always blocked, whatever the allowlist or the user's answer says.
DENY_PATTERNS: list[tuple[str, str]] = [
    (r"(^|\s)format(\.com)?(\s|$)", "formatting drives"),
    (r"\bdel(\.exe)?\b.*\s/[sq]\b", "recursive or silent delete"),
    (r"\b(erase)\b.*\s/[sq]\b", "recursive or silent delete"),
    (r"\brd\b.*\s/s\b|\brmdir\b.*\s/s\b", "recursive folder delete"),
    (r"\brm\b(?=.*\s-\w*r)(?=.*\s-\w*f)|\brm\s+-\w*(rf|fr)\w*", "rm -rf"),
    (r"\bremove-item\b.*-recurse", "recursive delete"),
    (r"\bshutdown\b", "shutting down (use the shutdown tool, which has a cancel window)"),
    (r"\breg(\.exe)?\s+(delete|add|import)\b", "editing the registry"),
    (r"\b(diskpart|bcdedit|vssadmin|cipher\s+/w|takeown|icacls|mkfs|fdisk|sfc|dism)\b", "system administration"),
    (r"\bdd\s+if=", "raw disk writes"),
    (r"\bset-executionpolicy\b|\binvoke-expression\b|\biex\b", "PowerShell policy/eval"),
    (r":\(\)\s*\{", "fork bomb"),
]

#: Anything mentioning these locations is blocked.
SYSTEM_PATH_PATTERNS = [
    r"[a-z]:[\\/]+windows\b", r"\\windows\\system32", r"\bsystem32\b", r"\bsyswow64\b",
    r"[a-z]:[\\/]+program files", r"[a-z]:[\\/]+programdata", r"%(systemroot|windir|programfiles|programdata)%",
    r"\$env:(systemroot|windir)", r"(^|\s)/(etc|usr|bin|sbin|boot|sys|proc|system|library)(/|\s|$)",
    r"[a-z]:[\\/]*$", r"(^|\s)[a-z]:[\\/]?(\s|$)",
]

#: Chaining, redirection and substitution would let a command sneak past the allowlist.
CHAIN_PATTERN = re.compile(r"&&|\|\||[;&|<>`]|\$\(|\r|\n")


@dataclass
class ShellVerdict:
    """Result of checking a command: blocked, or allowed (with/without a question)."""

    allowed: bool
    reason: str = ""
    program: str = ""


class ShellPolicy:
    """Denylist + allowlist for shell commands.

    Args:
        allowlist: Program names that may run (e.g. git, npm). Matched on the
            first word, ignoring a path and ``.exe``/``.cmd``.
    """

    def __init__(self, allowlist: list[str] | None = None) -> None:
        names = allowlist if allowlist is not None else DEFAULT_ALLOWLIST
        self.allowlist = {self._program_name(n) for n in names}

    @staticmethod
    def _program_name(token: str) -> str:
        name = token.strip().strip("\"'").replace("\\", "/").rsplit("/", 1)[-1].lower()
        for ext in (".exe", ".cmd", ".bat", ".com", ".ps1"):
            name = name.removesuffix(ext)
        return name

    def check(self, command: str) -> ShellVerdict:
        """Decide whether ``command`` may run (confirmation is the gate's job)."""
        text = (command or "").strip()
        if not text:
            return ShellVerdict(False, "the command is empty")
        lowered = text.lower()
        for pattern, why in DENY_PATTERNS:
            if re.search(pattern, lowered):
                return ShellVerdict(False, f"it's on the denylist ({why})")
        for pattern in SYSTEM_PATH_PATTERNS:
            if re.search(pattern, lowered):
                return ShellVerdict(False, "it touches a system folder")
        if CHAIN_PATTERN.search(text):
            return ShellVerdict(False, "chaining, pipes and redirection aren't allowed; run one command at a time")
        try:
            tokens = shlex.split(text, posix=os.name != "nt")
        except ValueError:
            return ShellVerdict(False, "the command has unbalanced quotes")
        if not tokens:
            return ShellVerdict(False, "the command is empty")
        program = self._program_name(tokens[0])
        if program not in self.allowlist:
            allowed = ", ".join(sorted(self.allowlist)) or "nothing"
            return ShellVerdict(False, f"'{program}' isn't on the shell allowlist (allowed: {allowed})", program)
        return ShellVerdict(True, "allowlisted", program)


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


@dataclass
class Decision:
    """Outcome of :meth:`SafetyGate.review`.

    ``confirmed`` is True/False when the user was asked, None when no question was needed.
    """

    allowed: bool
    confirmed: bool | None = None
    blocked: bool = False
    reason: str = ""

    def denial_message(self) -> str:
        """Tool result sent to the model when the call doesn't run."""
        if self.blocked:
            return f"Blocked by the safety policy: {self.reason}. Nothing was run. Tell the user why."
        return "The user declined, so nothing was done. Acknowledge briefly."


class SafetyGate:
    """Decides whether a tool call may run, and logs every call.

    Args:
        confirm: Asks a yes/no question and returns the answer (text mode types
            it; voice mode speaks it and listens). Timeouts must return False.
        log_path: Rotating log file for tool calls (``logs/tool_calls.log``).
        confirm_dangerous: When False, dangerous tools run without asking.
        shell_policy: Policy for tools that run shell commands.
        auto_approve_allowlist: Run allowlisted shell commands without asking.
    """

    def __init__(
        self,
        confirm: ConfirmFn,
        log_path: Path,
        confirm_dangerous: bool = True,
        shell_policy: ShellPolicy | None = None,
        auto_approve_allowlist: bool = False,
    ) -> None:
        self.confirm = confirm
        self.log_path = log_path
        self.confirm_dangerous = confirm_dangerous
        self.shell_policy = shell_policy or ShellPolicy()
        self.auto_approve_allowlist = auto_approve_allowlist
        self._audit = self._make_audit_logger(log_path)

    @classmethod
    def from_config(cls, config: Any, confirm: ConfirmFn) -> "SafetyGate":
        return cls(
            confirm=confirm,
            log_path=config.path("paths.logs_dir", "logs") / "tool_calls.log",
            confirm_dangerous=bool(config.get("safety.confirm_dangerous", True)),
            shell_policy=ShellPolicy(config.get("safety.shell_allowlist", DEFAULT_ALLOWLIST)),
            auto_approve_allowlist=bool(config.get("safety.auto_approve_allowlist", False)),
        )

    @staticmethod
    def _make_audit_logger(path: Path) -> logging.Logger:
        path.parent.mkdir(parents=True, exist_ok=True)
        audit = logging.Logger(f"jarvis.tool_calls:{path}")  # private, not in the global tree
        handler = logging.handlers.RotatingFileHandler(path, maxBytes=1_000_000, backupCount=5, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(message)s"))
        audit.addHandler(handler)
        audit.propagate = False
        return audit

    # -- decisions -----------------------------------------------------------

    def review(self, spec: Any, args: dict[str, Any]) -> Decision:
        """Check policy and ask for confirmation if needed. Blocked/declined calls are logged here."""
        question: str | None = None
        if spec.shell_arg:
            command = str(args.get(spec.shell_arg, ""))
            verdict = self.shell_policy.check(command)
            if not verdict.allowed:
                decision = Decision(False, None, blocked=True, reason=f"'{command}' was blocked because {verdict.reason}")
                self.record(spec.name, args, result=decision.reason, is_error=True, confirmed=None, blocked=True)
                return decision
            if not self.auto_approve_allowlist:
                question = f"I'm about to {spec.describe(args)}. Confirm?"
        if spec.dangerous and self.confirm_dangerous:
            question = f"I'm about to {spec.describe(args)}. Confirm?"
        if question is None:
            return Decision(True, None)

        try:
            approved = bool(self.confirm(question))
        except Exception:  # noqa: BLE001 - a broken prompt must fail closed
            log.exception("Confirmation prompt failed; denying %s", spec.name)
            approved = False
        log.info("Confirmation for %s: %s", spec.name, "approved" if approved else "denied")
        if not approved:
            self.record(spec.name, args, result="declined by user", is_error=True, confirmed=False)
            return Decision(False, False, reason="declined")
        return Decision(True, True)

    def record(
        self,
        name: str,
        args: dict[str, Any],
        *,
        result: str,
        is_error: bool,
        confirmed: bool | None,
        blocked: bool = False,
        duration_s: float = 0.0,
    ) -> None:
        """Write one line of JSON to the tool-call log (never raises)."""
        entry = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "tool": name,
            "args": args,
            "confirmed": confirmed,
            "blocked": blocked,
            "is_error": is_error,
            "duration_s": round(duration_s, 3),
            "result": result[:500],
        }
        try:
            self._audit.info(json.dumps(entry, ensure_ascii=False, default=str))
        except Exception:  # noqa: BLE001
            log.exception("Could not write the tool-call log %s", self.log_path)

    def close(self) -> None:
        for handler in list(self._audit.handlers):
            handler.close()
            self._audit.removeHandler(handler)


# ---------------------------------------------------------------------------
# Typed confirmation with a timeout (text mode)
# ---------------------------------------------------------------------------


def timed_input(prompt: str, timeout: float) -> str | None:
    """``input()`` that gives up after ``timeout`` seconds (returns None)."""
    print(prompt, end="", flush=True)
    if os.name == "nt":
        import msvcrt

        chars: list[str] = []
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if msvcrt.kbhit():
                ch = msvcrt.getwche()
                if ch in ("\r", "\n"):
                    print()
                    return "".join(chars)
                if ch == "\b":
                    if chars:
                        chars.pop()
                        print(" \b", end="", flush=True)
                    continue
                chars.append(ch)
            else:
                time.sleep(0.03)
        print()
        return None
    import select
    import sys

    try:
        ready, _, _ = select.select([sys.stdin], [], [], timeout)
    except (OSError, ValueError):  # stdin isn't selectable (e.g. some IDEs)
        return input()
    if not ready:
        print()
        return None
    return sys.stdin.readline().rstrip("\n")


_input_lock = threading.Lock()


def typed_confirm_factory(timeout: float) -> ConfirmFn:
    """A text-mode confirm function: "yes"/"no" typed within ``timeout`` seconds."""

    def confirm(question: str) -> bool:
        with _input_lock:
            try:
                answer = timed_input(f"\n  [confirm] {question} (yes/no, {timeout:.0f}s) > ", timeout)
            except (EOFError, KeyboardInterrupt):
                return False
        if answer is None:
            print("  (no answer - treated as no)")
            return False
        return parse_yes_no(answer)

    return confirm
