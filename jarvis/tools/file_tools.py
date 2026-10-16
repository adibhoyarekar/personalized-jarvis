"""File tools. Every path is confined to ``safe_roots`` in config.yaml."""

from __future__ import annotations

import datetime as dt
import logging
import os
import time
from pathlib import Path
from typing import Literal

from core.config import resolve_path
from tools import setting, tool
from tools._paths import UnsafePathError, resolve_safe_path, safe_roots
from tools._platform import open_with_default_handler

log = logging.getLogger(__name__)

MAX_RESULTS = 20
MAX_READ_BYTES = 200_000
SKIP_DIRS = {"node_modules", ".git", "__pycache__", ".venv", "venv", "appdata", "$recycle.bin", ".cache"}
#: Opening these would run code, so open_file refuses them.
EXECUTABLE_EXTS = {".exe", ".bat", ".cmd", ".com", ".ps1", ".vbs", ".vbe", ".js", ".jse", ".wsf", ".wsh",
                   ".msi", ".scr", ".lnk", ".reg", ".hta", ".cpl", ".pif", ".jar", ".sh"}


def _human_size(size: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.0f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def _when(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def resolve_folder(path: str) -> Path:
    """Spoken folder name or path -> safe absolute folder (kept for other modules)."""
    return resolve_safe_path(path)


@tool
def list_folder(
    path: str = "downloads", limit: int = 25, sort_by: Literal["modified", "name", "size"] = "modified"
) -> dict:
    """List the files and sub-folders inside a folder, newest first by default.

    Use for "what's in my downloads", "show my desktop", "latest files in Documents".

    Args:
        path: A named folder (desktop, downloads, documents) or a path inside the allowed folders.
        limit: Maximum number of entries to return.
        sort_by: "modified" (newest first), "name", or "size" (largest first).
    """
    folder = resolve_safe_path(path, must_exist=True)
    if not folder.is_dir():
        raise NotADirectoryError(f"Not a folder: {folder}")
    entries = []
    with os.scandir(folder) as it:
        for entry in it:
            try:
                stat = entry.stat()
            except OSError:
                continue
            entries.append((entry, stat))
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
            "name": e.name,
            "type": "folder" if e.is_dir() else "file",
            "size": None if e.is_dir() else _human_size(st.st_size),
            "modified": _when(st.st_mtime),
        }
        for e, st in entries[:limit]
    ]
    return {"folder": str(folder), "total_entries": len(entries), "shown": len(items), "items": items}


def _walk(root: Path, max_depth: int, deadline: float):  # type: ignore[no-untyped-def]
    """Breadth-first os.scandir walk that skips noise folders and stops at the deadline."""
    stack: list[tuple[Path, int]] = [(root, 0)]
    while stack and time.monotonic() < deadline:
        folder, depth = stack.pop(0)
        try:
            with os.scandir(folder) as it:
                for entry in it:
                    name = entry.name
                    if name.startswith(".") and name not in (".env",):
                        continue
                    try:
                        is_dir = entry.is_dir(follow_symlinks=False)
                    except OSError:
                        continue
                    if is_dir:
                        if name.lower() not in SKIP_DIRS and depth < max_depth:
                            stack.append((Path(entry.path), depth + 1))
                    yield entry, is_dir
        except (PermissionError, FileNotFoundError, NotADirectoryError, OSError):
            continue


@tool
def search_files(query: str, folder: str = "", extension: str = "", include_folders: bool = False) -> dict:
    """Find files by name inside the allowed folders (Documents, Downloads, Desktop by default).

    Every word of the query must appear in the file name (case-insensitive).
    Returns up to 20 matches, newest first, with full paths for open_file.

    Args:
        query: Words from the file name, e.g. "invoice march" or "report".
        folder: Limit the search to one folder (name or path); empty searches all allowed folders.
        extension: Only this file type, e.g. "pdf" or ".docx".
        include_folders: Also match folder names.
    """
    words = [w for w in query.lower().replace("_", " ").split() if w]
    if not words:
        raise ValueError("Give me part of the file name to search for")
    ext = extension.lower().strip()
    if ext and not ext.startswith("."):
        ext = "." + ext
    roots = [resolve_safe_path(folder, must_exist=True)] if folder else [r for r in safe_roots() if r.exists()]

    deadline = time.monotonic() + 6.0
    matches: list[tuple[float, str, str]] = []
    scanned = 0
    for root in roots:
        for entry, is_dir in _walk(root, max_depth=8, deadline=deadline):
            scanned += 1
            if is_dir and not include_folders:
                continue
            name = entry.name.lower()
            if ext and not name.endswith(ext):
                continue
            haystack = name.replace("_", " ").replace("-", " ")
            if all(w in haystack for w in words):
                try:
                    mtime = entry.stat().st_mtime
                except OSError:
                    mtime = 0.0
                matches.append((mtime, entry.path, "folder" if is_dir else "file"))
    matches.sort(reverse=True)
    results = [{"path": p, "type": kind, "modified": _when(m)} for m, p, kind in matches[:MAX_RESULTS]]
    return {
        "query": query,
        "matches": results,
        "total_matches": len(matches),
        "note": "search timed out early" if time.monotonic() > deadline else "",
        "scanned": scanned,
    }


@tool
def open_file(path: str) -> str:
    """Open a file or folder with its default app (e.g. a PDF in the PDF viewer).

    Use a full path from search_files or list_folder. Programs and scripts are refused.

    Args:
        path: Path of the file or folder inside the allowed folders.
    """
    target = resolve_safe_path(path, must_exist=True)
    if target.is_file() and target.suffix.lower() in EXECUTABLE_EXTS:
        raise UnsafePathError(f"I won't open {target.name}: it's a program or script")
    open_with_default_handler(str(target))
    return f"Opened {target.name}."


@tool
def read_text_file(path: str, max_chars: int = 4000) -> dict:
    """Read a text file (notes, code, CSV, logs...) so you can answer questions about it.

    Only the first part of large files is returned.

    Args:
        path: Path of the file inside the allowed folders.
        max_chars: Maximum characters to return (up to 20000).
    """
    target = resolve_safe_path(path, must_exist=True)
    if not target.is_file():
        raise IsADirectoryError(f"{target.name} is a folder, not a file")
    max_chars = max(100, min(int(max_chars), 20_000))
    with target.open("rb") as fh:
        raw = fh.read(MAX_READ_BYTES)
    if b"\x00" in raw[:4096]:
        raise ValueError(f"{target.name} looks like a binary file, not text")
    text = raw.decode("utf-8", errors="replace")
    size = target.stat().st_size
    return {
        "file": target.name,
        "size": _human_size(size),
        "truncated": len(text) > max_chars or size > MAX_READ_BYTES,
        "content": text[:max_chars],
    }


def notes_dir() -> Path:
    raw = str(setting("notes_dir", "~/Documents/Jarvis Notes"))
    folder = resolve_path(raw, Path.home())
    return resolve_safe_path(str(folder))


@tool
def create_note(text: str, title: str = "") -> str:
    """Write a note down. Notes are appended, with a timestamp, to today's file in the notes folder.

    Use for "take a note", "note that...", "write down...".

    Args:
        text: The note itself.
        title: Optional short heading.
    """
    if not text.strip():
        raise ValueError("The note is empty")
    folder = notes_dir()
    folder.mkdir(parents=True, exist_ok=True)
    now = dt.datetime.now()
    file = folder / f"{now:%Y-%m-%d}.md"
    heading = f"## {now:%H:%M}" + (f" - {title.strip()}" if title.strip() else "")
    with file.open("a", encoding="utf-8") as fh:
        fh.write(f"{heading}\n{text.strip()}\n\n")
    return f"Noted in {file.name} ({file.parent})."


@tool(dangerous=True, summary="delete {path}. It goes to the Recycle Bin")
def delete_file(path: str) -> str:
    """Delete a file by moving it to the Recycle Bin (never a permanent delete).

    Find the exact path with search_files or list_folder first. Folders are not deleted.

    Args:
        path: Full path of the file inside the allowed folders.
    """
    target = resolve_safe_path(path, must_exist=True)
    if target.is_dir():
        raise IsADirectoryError(f"{target.name} is a folder; I only delete single files")
    if any(target == root for root in safe_roots()):
        raise UnsafePathError("I won't delete one of the main folders")
    try:
        from send2trash import send2trash
    except ImportError as exc:
        raise RuntimeError("Deleting needs the send2trash package: pip install send2trash") from exc
    send2trash(str(target))
    log.info("Moved to Recycle Bin: %s", target)
    return f"Moved {target.name} to the Recycle Bin."
