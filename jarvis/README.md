# Jarvis - personal voice assistant for Windows

Say "Jarvis", ask for something, and it uses tools to control your PC and
answers out loud. Built with Claude (Anthropic API) and local speech models.

> **Status: Phase 1 of 5 - text mode.** The agent loop, tool system and five
> tools work. Voice arrives in Phases 2 and 3.

## Setup (Windows, Python 3.11+)

```powershell
cd jarvis
py -3.11 -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env        # then paste your key into .env
```

Get an API key at https://platform.claude.com/settings/keys and put it in
`jarvis\.env` as `ANTHROPIC_API_KEY=...`.

## Run

```powershell
python main.py --text           # type to Jarvis (debug mode)
python main.py --text --debug   # same, with verbose logs
python -m pytest                # run the tests
```

In text mode: `/tools` lists tools, `/reset` clears the conversation, `/quit` exits.

## Tools so far

| Tool | What it does |
|---|---|
| `open_app` | Launches an app by name (aliases in `config.yaml` -> `tools.apps`) |
| `system_stats` | CPU, RAM, disk and battery |
| `open_url` | Opens a website in your default browser |
| `web_search` | DuckDuckGo search, top results |
| `list_folder` | Lists a folder (desktop, downloads, ... or any path) |

## Adding a tool

Write one function anywhere in `tools/` with type hints and a docstring. That's it:

```python
from tools import tool

@tool
def flip_coin(times: int = 1) -> str:
    """Flip a coin and report the results.

    Args:
        times: How many flips.
    """
    import random
    return ", ".join(random.choice(["heads", "tails"]) for _ in range(times))
```

The decorator builds the JSON schema from the type hints (`str`, `int`,
`float`, `bool`, `list[str]`, `Optional[...]`, `Literal[...]`) and the
description from the docstring (the `Args:` section becomes parameter
descriptions). Use `@tool(dangerous=True)` for anything destructive, so Jarvis
asks "yes or no?" first. Disable tools without code changes via
`tools.disabled` / `tools.disabled_modules` in `config.yaml`.

## Files and logs

- `config.yaml` - all settings; `.env` - your API key only.
- `logs/jarvis.log` - main log (rotating).
- `logs/tool_calls.jsonl` - one line per tool call: arguments, approval, result.

## Defaults chosen (change in `config.yaml`)

- Model `claude-sonnet-5-5` at effort `low` for snappy spoken replies; raise
  `llm.effort` to `medium` if answers feel shallow.
- Last 10 exchanges kept as context (`llm.max_history_turns`).
- `llm.refusal_fallback: true` - if the model declines a request, the API
  retries it on a fallback model inside the same call.
- Jarvis calls you "sir" until you set `assistant.user_name`.
- Tool calls time out after 30 s (`tools.timeout_s`).
