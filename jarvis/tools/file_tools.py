"""File-system tools: browsing folders (more arrive in Phase 4)."""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Literal

from core.config import resolve_path
from tools import setting, tool


def resolve_folder(path: str) -> Path:
    """Turn a spoken folder name ("downloads") or a path into an absolute Path."""
    folders: dict[str, str] = {k.lower(): v for k, v in (setting("tools.folders", {}) or {}).items()}
    key = path.strip().lower().removeprefix("my ").removesuffix(" folder").strip()
    raw = folders.get(key, path.strip())
    return resolve_path(raw, Path.home())


def _human_size(size: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.0f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


@tool
def list_folder(
    path: str = "downloads", limit: int = 25, sort_by: Literal["modified", "name", "size"] = "modified"
) -> dict:
    """List the files and sub-folders inside a folder, newest first by default.

    Use for "what's in my downloads", "show my desktop", "latest files in Documents".

    Args:
        path: A named folder (desktop, downloads, documents, pictures, music, videos, home) or a full path.
        limit: Maximum number of entries to return.
        sort_by: "modified" (newest first), "name", or "size" (largest first).
    """
    folder = resolve_folder(path)
    if not folder.exists():
        raise FileNotFoundError(f"Folder not found: {folder}")
    if not folder.is_dir():
        raise NotADirectoryError(f"Not a folder: {folder}")

    entries = []
    for child in folder.iterdir():
        try:
            stat = child.stat()
        except OSError:
            continue
        entries.append((child, stat))

    keys = {
        "name": (lambda e: e[0].name.lower(), False),
        "size": (lambda e: e[1].st_size, True),
        "modified": (lambda e: e[1].st_mtime, True),
    }
    key_fn, reverse = keys.get(sort_by, keys["modified"])
    entries.sort(key=key_fn, reverse=reverse)

    limit = max(1, min(int(limit), 200))
    items = [
        {
            "name": p.name,
            "type": "folder" if p.is_dir() else "file",
            "size": None if p.is_dir() else _human_size(st.st_size),
            "modified": dt.datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M"),
        }
        for p, st in entries[:limit]
    ]
    return {"folder": str(folder), "total_entries": len(entries), "shown": len(items), "items": items}
