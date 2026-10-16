"""Small, comment-preserving edits to config.yaml and .env (used by the setup wizard).

Re-dumping YAML would throw away every comment in config.yaml, so these
helpers change only the lines they need to.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml


def yaml_value(value: Any) -> str:
    """Render a value as inline YAML (strings quoted when needed, lists in [a, b] form)."""
    if value is None:
        return "null"
    text = yaml.safe_dump(value, default_flow_style=True, allow_unicode=True, width=10_000).strip()
    return text.removesuffix("\n...").removesuffix("...").strip()


def _section_bounds(lines: list[str], section: str) -> tuple[int, int] | None:
    """(header index, end index exclusive) of a top-level section, or None."""
    header = re.compile(rf"^{re.escape(section)}:\s*(#.*)?$")
    for i, line in enumerate(lines):
        if header.match(line):
            end = i + 1
            while end < len(lines) and (not lines[end].strip() or lines[end][0] in " \t"):
                end += 1
            while end > i + 1 and not lines[end - 1].strip():
                end -= 1  # trailing blank lines belong outside the section
            return i, end
    return None


def set_value(text: str, key: str, value: Any) -> str:
    """Set ``section.key`` (or a top-level ``key``) to ``value``, keeping any trailing comment."""
    lines = text.splitlines()
    rendered = yaml_value(value)
    parts = key.split(".")
    if len(parts) == 1:
        pattern = re.compile(rf"^({re.escape(key)}:)([ \t]*)([^#\n]*?)([ \t]+#.*)?$")
        for i, line in enumerate(lines):
            m = pattern.match(line)
            if m:
                lines[i] = f"{m.group(1)} {rendered}{m.group(4) or ''}"
                return "\n".join(lines) + "\n"
        return text.rstrip("\n") + f"\n{key}: {rendered}\n"
    if len(parts) != 2:
        raise ValueError("Only top-level and section.key settings are supported")
    section, name = parts
    bounds = _section_bounds(lines, section)
    if bounds is None:
        return text.rstrip("\n") + f"\n\n{section}:\n  {name}: {rendered}\n"
    start, end = bounds
    pattern = re.compile(rf"^(\s+{re.escape(name)}:)([ \t]*)([^#\n]*?)([ \t]+#.*)?$")
    for i in range(start + 1, end):
        m = pattern.match(lines[i])
        if m:
            comment = m.group(4) or ""
            new = f"{m.group(1)} {rendered}"
            if comment:  # keep the comment in the same column
                column = len(m.group(1)) + len(m.group(2)) + len(m.group(3)) + len(comment) - len(comment.lstrip())
                new += " " * max(1, column - len(new)) + comment.lstrip()
            lines[i] = new
            return "\n".join(lines) + "\n"
    lines.insert(start + 1, f"  {name}: {rendered}")
    return "\n".join(lines) + "\n"


def set_block(text: str, key: str, value: Any) -> str:
    """Replace a whole top-level block (e.g. ``projects``) with ``value`` in block style."""
    lines = text.splitlines()
    dumped = yaml.safe_dump({key: value}, sort_keys=False, default_flow_style=False, allow_unicode=True,
                            indent=2).rstrip("\n").splitlines()
    bounds = _section_bounds(lines, key)
    if bounds is None:
        single = re.compile(rf"^{re.escape(key)}:.*$")
        idx = next((i for i, line in enumerate(lines) if single.match(line)), None)
        if idx is None:
            return text.rstrip("\n") + "\n\n" + "\n".join(dumped) + "\n"
        bounds = (idx, idx + 1)
    start, end = bounds
    lines[start:end] = dumped
    return "\n".join(lines) + "\n"


def set_env(path: Path, key: str, value: str) -> None:
    """Set ``KEY=value`` in a .env file, keeping its other lines."""
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    pattern = re.compile(rf"^\s*{re.escape(key)}\s*=")
    entry = f"{key}={value}"
    for i, line in enumerate(lines):
        if pattern.match(line):
            lines[i] = entry
            break
    else:
        lines.append(entry)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
