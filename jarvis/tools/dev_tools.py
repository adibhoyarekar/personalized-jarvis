"""Developer tools: projects from config, git, gated shell commands, dev servers."""

from __future__ import annotations

import difflib
import logging
import shlex
import shutil
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path

import psutil

from core.config import resolve_path
from tools import setting, tool
from tools._platform import IS_WINDOWS, open_with_default_handler

log = logging.getLogger(__name__)

COMMAND_TIMEOUT_S = 120
MAX_OUTPUT = 1500
WINDOWS_BUILTINS = {"dir", "echo", "type", "ver", "where", "cd"}


@dataclass
class Project:
    name: str
    path: Path
    dev_command: str


def projects() -> dict[str, Project]:
    """Projects from config.yaml: ``projects: {name: {path: ..., dev_command: ...}}``."""
    out = {}
    for name, spec in (setting("projects") or {}).items():
        spec = spec or {}
        raw = str(spec.get("path") or "").strip()
        if not raw:
            continue
        out[str(name).lower()] = Project(str(name), resolve_path(raw, Path.home()), str(spec.get("dev_command") or ""))
    return out


def find_project(name: str) -> Project:
    """Look up a project by (fuzzy) name; errors list what's configured."""
    known = projects()
    if not known:
        raise ValueError("No projects are configured yet. Add them under 'projects' in config.yaml")
    key = name.strip().lower()
    spoken = key.replace(" ", "-")
    for candidate in (key, spoken, key.replace(" ", "")):
        if candidate in known:
            project = known[candidate]
            break
    else:
        close = difflib.get_close_matches(spoken, list(known), n=1, cutoff=0.5)
        if not close:
            raise ValueError(f"I don't know a project called {name}. Configured: {', '.join(sorted(known))}")
        project = known[close[0]]
    if not project.path.is_dir():
        raise FileNotFoundError(f"The folder for {project.name} doesn't exist: {project.path}")
    return project


def _clip(text: str) -> str:
    text = text.strip()
    return text if len(text) <= MAX_OUTPUT else "..." + text[-MAX_OUTPUT:]


def run_args(args: list[str], cwd: Path, timeout: int = COMMAND_TIMEOUT_S) -> dict:
    """Run a command without a shell and return exit code + trimmed output."""
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        proc = subprocess.run(  # noqa: S603 - args are validated by the safety gate / fixed
            args, cwd=cwd, capture_output=True, text=True, timeout=timeout,
            encoding="utf-8", errors="replace", creationflags=flags,
        )
    except subprocess.TimeoutExpired:
        return {"exit_code": None, "output": f"Stopped after {timeout} seconds (still running too long)"}
    output = (proc.stdout or "") + (("\n" + proc.stderr) if proc.stderr else "")
    return {"exit_code": proc.returncode, "output": _clip(output) or "(no output)"}


def _split(command: str) -> list[str]:
    tokens = shlex.split(command, posix=not IS_WINDOWS)
    return [t.strip('"') for t in tokens] if IS_WINDOWS else tokens


def resolve_command(command: str) -> list[str]:
    """Turn a validated command line into argv (npm -> npm.cmd on Windows, dir -> cmd /c dir)."""
    tokens = _split(command)
    exe = shutil.which(tokens[0])
    if exe:
        return [exe, *tokens[1:]]
    if IS_WINDOWS and tokens[0].lower() in WINDOWS_BUILTINS:
        return ["cmd", "/c", *tokens]
    raise FileNotFoundError(f"'{tokens[0]}' isn't installed or isn't on PATH")


@tool
def open_project(project: str) -> str:
    """Open one of the user's coding projects in VS Code.

    Args:
        project: Project name as configured, e.g. "cs-events" (fuzzy matched).
    """
    proj = find_project(project)
    code = shutil.which("code")
    if code:
        subprocess.Popen([code, str(proj.path)], creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))  # noqa: S603
        return f"Opened {proj.name} in VS Code."
    open_with_default_handler(str(proj.path))
    return f"VS Code isn't on PATH, so I opened the {proj.name} folder instead."


@tool(shell_arg="command", summary=lambda a: f"run '{a.get('command', '')}' in {a.get('project', 'the project')}")
def run_command_in_project(project: str, command: str) -> dict:
    """Run one shell command inside a project folder and return its output.

    Only allowlisted programs (git, npm, python, ...) can run, one command at a
    time (no pipes, &&, or redirection). The user is asked to confirm first.

    Args:
        project: Project name as configured.
        command: The command line, e.g. "npm test" or "git log --oneline -5".
    """
    proj = find_project(project)
    result = run_args(resolve_command(command), proj.path)
    return {"project": proj.name, "command": command, **result}


@tool
def git_status(project: str) -> dict:
    """Show a project's git branch, changed files and latest commit.

    Args:
        project: Project name as configured.
    """
    proj = find_project(project)
    git = shutil.which("git") or "git"
    status = run_args([git, "status", "--short", "--branch"], proj.path, timeout=30)
    last = run_args([git, "log", "-1", "--format=%h %s (%cr)"], proj.path, timeout=30)
    return {"project": proj.name, "status": status["output"], "last_commit": last["output"]}


@tool(dangerous=True, summary="pull the latest changes into {project}")
def git_pull(project: str) -> dict:
    """Pull the latest changes for a project (fast-forward only, never a merge).

    Args:
        project: Project name as configured.
    """
    proj = find_project(project)
    git = shutil.which("git") or "git"
    return {"project": proj.name, **run_args([git, "pull", "--ff-only"], proj.path)}


_SERVERS: dict[str, int] = {}
_SERVERS_LOCK = threading.Lock()


@tool
def start_dev_server(project: str) -> str:
    """Start a project's dev server (its configured dev_command) in a new terminal window.

    Args:
        project: Project name as configured.
    """
    proj = find_project(project)
    if not proj.dev_command:
        raise ValueError(f"No dev_command is configured for {proj.name}")
    with _SERVERS_LOCK:
        pid = _SERVERS.get(proj.name)
        if pid and psutil.pid_exists(pid):
            return f"The {proj.name} dev server is already running."
        if IS_WINDOWS:
            # The command comes from the user's own config, not from the model.
            proc = subprocess.Popen(  # noqa: S603
                ["cmd", "/k", proj.dev_command], cwd=proj.path, creationflags=subprocess.CREATE_NEW_CONSOLE
            )
        else:
            proc = subprocess.Popen(proj.dev_command, cwd=proj.path, shell=True, start_new_session=True)  # noqa: S602
        _SERVERS[proj.name] = proc.pid
    log.info("Started dev server for %s (pid %s): %s", proj.name, proc.pid, proj.dev_command)
    return f"Started {proj.dev_command} for {proj.name} in a new window."


@tool
def stop_dev_server(project: str) -> str:
    """Stop a dev server that start_dev_server launched (closes its window and child processes).

    Args:
        project: Project name as configured.
    """
    proj = find_project(project)
    with _SERVERS_LOCK:
        pid = _SERVERS.pop(proj.name, None)
    if not pid or not psutil.pid_exists(pid):
        return f"No dev server I started is running for {proj.name}."
    try:
        parent = psutil.Process(pid)
        procs = parent.children(recursive=True) + [parent]
    except psutil.NoSuchProcess:
        return f"The {proj.name} dev server had already stopped."
    for p in procs:
        try:
            p.terminate()
        except psutil.Error:
            pass
    _gone, alive = psutil.wait_procs(procs, timeout=5)
    for p in alive:
        try:
            p.kill()
        except psutil.Error:
            pass
    return f"Stopped the {proj.name} dev server."

