# Adding a tool

A tool is **one Python function** with type hints, a docstring and the `@tool`
decorator, in any file under `tools/`. There's nothing to register: Jarvis
imports every module in `tools/` at startup.

## Worked example: `open_my_portfolio`

Goal: "Hey Jarvis, open my portfolio" opens your portfolio website, and
"open the projects page of my portfolio" opens a sub-page.

### 1. Add a setting (optional)

Keep personal values out of code. In `config.yaml`, add a top-level section
(Jarvis allows new top-level sections for your own tools):

```yaml
portfolio:
  url: "https://adityabhoyarekar.dev"
```

### 2. Write the tool

Create `tools/portfolio_tools.py`:

```python
"""Tools for my personal portfolio site."""

from __future__ import annotations

import webbrowser
from typing import Literal

from tools import setting, tool


@tool
def open_my_portfolio(page: Literal["home", "projects", "blog", "contact"] = "home") -> str:
    """Open the user's personal portfolio website in the browser.

    Use when the user says "open my portfolio", "show my website" or names a
    section of it.

    Args:
        page: Which section to open.
    """
    base = str(setting("portfolio.url", "")).rstrip("/")
    if not base:
        raise ValueError("No portfolio URL set. Add portfolio.url to config.yaml")
    url = base if page == "home" else f"{base}/{page}"
    webbrowser.open(url)
    return f"Opened the {page} page of your portfolio."
```

What the decorator does with it:

| You write | The model sees |
|---|---|
| function name `open_my_portfolio` | tool name |
| docstring text before `Args:` | tool description: say *when* to use it |
| `page: Literal[...] = "home"` | `{"type": "string", "enum": [...], "default": "home"}`, optional |
| `Args:` line for `page` | the parameter's description |

Supported types: `str`, `int`, `float`, `bool`, `list[str]` (or other
lists), `dict`, `Optional[X]` / `X | None`, and `Literal[...]`. Parameters
without a default are required.

### 3. Try it

```powershell
python main.py --text
You > /tools                       # open_my_portfolio should be listed
You > open the projects page of my portfolio
```

Then by voice: "Hey Jarvis, open my portfolio."

### 4. Test it

`tests/test_portfolio.py`:

```python
from pathlib import Path

from core.config import Config
from tools import ToolContext, set_context


def test_open_my_portfolio(monkeypatch):
    set_context(ToolContext(config=Config({"portfolio": {"url": "https://me.dev/"}}, Path("."))))
    opened = []
    monkeypatch.setattr("webbrowser.open", opened.append)
    from tools.portfolio_tools import open_my_portfolio

    assert open_my_portfolio("projects") == "Opened the projects page of your portfolio."
    assert opened == ["https://me.dev/projects"]
```

`tests/test_tool_decorator.py::test_all_real_tools_have_valid_schemas`
also checks your new tool's schema automatically.

## Rules of thumb

- **Return a short sentence or a small dict.** Jarvis speaks the result in
  its own words. Dicts and lists are sent as JSON; anything over 12,000
  characters is truncated.
- **Raise on failure** with a message a person would understand
  (`ValueError("No portfolio URL set...")`). The model receives
  `Error: <message>` and explains it. Never return "success" text for
  something that didn't happen.
- **Destructive or hard to undo?** Use
  `@tool(dangerous=True, summary="delete {path}")`. Jarvis then says
  "I'm about to delete report.docx. Confirm?" and only runs the tool on a clear yes.
- **Runs a shell command?** Use `@tool(shell_arg="command")` so the command
  is checked against the denylist and `safety.shell_allowlist` first.
- **Files?** Resolve every path with `tools._paths.resolve_safe_path(path)`
  so it stays inside `safe_roots`.
- **Needs memory or to speak later?** `get_memory()` gives the SQLite store,
  and `notify("...")` speaks a message (used by timers).
- **Slow?** Tools time out after `tools.timeout_s` (30 s). Jarvis says
  "One moment." if a tool takes more than 2.5 s.
- **Windows-only?** Import platform libraries inside the function and raise
  a clear error elsewhere (see `tools/_platform.py:require_windows`). A module
  that fails to import is skipped with a log message instead of crashing Jarvis.
- **Turn tools off** without deleting them: `tools.disabled: [open_my_portfolio]`
  or `tools.disabled_modules: [portfolio_tools]` in `config.yaml`.
