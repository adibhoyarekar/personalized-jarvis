"""Path safety for file tools: everything must stay inside ``safe_roots``."""

from __future__ import annotations

import os
import re
from pathlib import Path

from core.config import resolve_path
from tools import setting

DEFAULT_SAFE_ROOTS = ["~/Documents", "~/Downloads", "~/Desktop"]


class UnsafePathError(PermissionError):
    """A path outside the allowed folders (message is meant for the user)."""


def safe_roots() -> list[Path]:
    """The configured safe roots, fully resolved."""
    roots = setting("safe_roots", DEFAULT_SAFE_ROOTS) or []
    return [resolve_path(str(r), Path.home()).resolve() for r in roots]


def _norm(path: Path) -> str:
    return os.path.normcase(os.path.normpath(str(path)))


def is_within(path: Path, root: Path) -> bool:
    """True if ``path`` is ``root`` or inside it (case-insensitive on Windows)."""
    p, r = _norm(path), _norm(root)
    try:
        return os.path.commonpath([p, r]) == r
    except ValueError:  # different drives on Windows
        return False


def _named_folder(name: str) -> str | None:
    folders = {k.lower(): v for k, v in (setting("tools.folders", {}) or {}).items()}
    key = name.strip().lower().removeprefix("my ").removesuffix(" folder").strip()
    return folders.get(key)


def resolve_safe_path(path: str, must_exist: bool = False) -> Path:
    """Resolve a spoken folder name or a path, and refuse anything outside ``safe_roots``.

    Relative paths are tried against each safe root (the first existing match
    wins). Symlinks and ``..`` are resolved *before* the check, so
    "../../Windows/System32" can't escape.

    Raises:
        UnsafePathError: The path is outside every safe root.
        FileNotFoundError: ``must_exist`` and nothing is there.
    """
    roots = safe_roots()
    if not roots:
        raise UnsafePathError("No safe folders are configured (safe_roots in config.yaml)")
    raw = (path or "").strip().strip("\"'")
    if not raw:
        raise ValueError("No path given")
    named = _named_folder(raw)
    if named:
        raw = named
    # Treat "\\" as a separator everywhere, so Windows-style tricks behave the
    # same on every OS.
    raw = os.path.expandvars(os.path.expanduser(raw)).replace("\\", "/")
    if re.match(r"^[a-zA-Z]:", raw) and not Path(raw).is_absolute():
        raise UnsafePathError("That's a Windows drive path, which isn't allowed here.")

    expanded = Path(raw)
    if expanded.is_absolute():
        candidates = [expanded.resolve()]
    else:
        candidates = [(root / expanded).resolve() for root in roots]
        existing = [c for c in candidates if c.exists()]
        candidates = existing or candidates[:1]
    target = candidates[0]

    if not any(is_within(target, root) for root in roots):
        names = ", ".join(r.name or str(r) for r in roots)
        raise UnsafePathError(f"That's outside the folders I'm allowed to use ({names}).")
    if must_exist and not target.exists():
        raise FileNotFoundError(f"Nothing found at {target}")
    return target
