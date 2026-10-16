"""File tools must never reach outside safe_roots."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import tools
from core.config import Config
from tools import ToolContext, set_context
from tools._paths import UnsafePathError, resolve_safe_path


@pytest.fixture
def roots(tmp_path: Path) -> dict[str, Path]:
    docs, downloads, outside = tmp_path / "Documents", tmp_path / "Downloads", tmp_path / "Secret"
    for d in (docs, downloads, outside):
        d.mkdir()
    (docs / "report.docx").write_text("report")
    (docs / "notes.txt").write_text("hello notes")
    (docs / "tool.bat").write_text("echo hi")
    (outside / "passwords.txt").write_text("nope")
    (downloads / "Invoice March.pdf").write_text("pdf")
    sub = docs / "projects" / "deep"
    sub.mkdir(parents=True)
    (sub / "march-invoice-draft.txt").write_text("x")
    cfg = Config(
        {"safe_roots": [str(docs), str(downloads)], "tools": {"folders": {"documents": str(docs), "secret": str(outside)}},
         "notes_dir": str(docs / "Jarvis Notes")},
        tmp_path,
    )
    set_context(ToolContext(config=cfg))
    return {"docs": docs, "downloads": downloads, "outside": outside}


@pytest.mark.parametrize(
    "attempt",
    ["../../Windows/System32", "..\\..\\Windows\\System32\\drivers", "../Secret/passwords.txt",
     "C:/Windows/System32/config", "/etc/passwd", "~", "secret", "Documents/../../Secret/passwords.txt"],
)
def test_escapes_are_rejected(roots: dict[str, Path], attempt: str) -> None:
    with pytest.raises(UnsafePathError):
        resolve_safe_path(attempt)


def test_symlink_escape_is_rejected(roots: dict[str, Path]) -> None:
    link = roots["docs"] / "sneaky"
    try:
        os.symlink(roots["outside"], link, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not permitted here")
    with pytest.raises(UnsafePathError):
        resolve_safe_path(str(link / "passwords.txt"))


def test_allowed_paths_resolve(roots: dict[str, Path]) -> None:
    assert resolve_safe_path("documents") == roots["docs"].resolve()
    assert resolve_safe_path("report.docx") == (roots["docs"] / "report.docx").resolve()
    assert resolve_safe_path(str(roots["downloads"] / "Invoice March.pdf"), must_exist=True).exists()
    with pytest.raises(FileNotFoundError):
        resolve_safe_path("missing.txt", must_exist=True)


def test_file_tools_respect_roots(roots: dict[str, Path]) -> None:
    from tools import file_tools

    with pytest.raises(UnsafePathError):
        file_tools.read_text_file(str(roots["outside"] / "passwords.txt"))
    with pytest.raises(UnsafePathError):
        file_tools.list_folder("../Secret")
    with pytest.raises(UnsafePathError):
        file_tools.delete_file("../Secret/passwords.txt")
    assert file_tools.read_text_file("notes.txt")["content"] == "hello notes"
    with pytest.raises(UnsafePathError):
        file_tools.open_file("tool.bat")  # executables are refused


def test_search_files_finds_by_words_and_never_leaves_roots(roots: dict[str, Path]) -> None:
    from tools import file_tools

    found = file_tools.search_files("march invoice")
    names = sorted(Path(m["path"]).name for m in found["matches"])
    assert names == ["Invoice March.pdf", "march-invoice-draft.txt"]
    assert file_tools.search_files("passwords")["matches"] == []
    assert [Path(m["path"]).name for m in file_tools.search_files("invoice", extension="pdf")["matches"]] == [
        "Invoice March.pdf"
    ]


def test_search_is_capped(roots: dict[str, Path]) -> None:
    from tools import file_tools

    for i in range(30):
        (roots["docs"] / f"log-{i}.txt").write_text("x")
    assert len(file_tools.search_files("log")["matches"]) == file_tools.MAX_RESULTS


def test_create_note_and_delete_to_recycle_bin(roots: dict[str, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    from tools import file_tools

    message = file_tools.create_note("buy milk", title="shopping")
    note = next((roots["docs"] / "Jarvis Notes").glob("*.md"))
    assert "buy milk" in note.read_text() and "shopping" in note.read_text() and "Noted" in message

    trashed: list[str] = []
    monkeypatch.setattr("send2trash.send2trash", trashed.append)
    assert "Recycle Bin" in file_tools.delete_file("report.docx")
    assert trashed == [str((roots["docs"] / "report.docx").resolve())]
    with pytest.raises(IsADirectoryError):
        file_tools.delete_file("projects")
    assert tools.all_tools()["delete_file"].dangerous
