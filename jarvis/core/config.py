"""Configuration loading: ``config.yaml`` for settings, ``.env`` for secrets."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

log = logging.getLogger(__name__)

PROJECT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_DIR / "config.yaml"

_MISSING = object()


class ConfigError(Exception):
    """Raised when the configuration is missing or invalid."""


class Config:
    """Read-only view over the YAML config with dotted-key access.

    Example:
        >>> cfg.get("llm.model")
        'claude-sonnet-5-5'
    """

    def __init__(self, data: dict[str, Any], base_dir: Path) -> None:
        self._data = data
        self.base_dir = base_dir

    def get(self, key: str, default: Any = None) -> Any:
        """Return the value at a dotted key like ``"llm.model"``, or ``default``."""
        node: Any = self._data
        for part in key.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return default if node is None else node

    def require(self, key: str) -> Any:
        """Like :meth:`get` but raise :class:`ConfigError` when the key is absent."""
        value = self.get(key, _MISSING)
        if value is _MISSING:
            raise ConfigError(f"Missing required setting '{key}' in config.yaml")
        return value

    def path(self, key: str, default: str) -> Path:
        """Resolve a path setting: expand ~ and env vars, make relative paths absolute."""
        return resolve_path(str(self.get(key, default)), self.base_dir)

    @property
    def data(self) -> dict[str, Any]:
        """The raw config dictionary."""
        return self._data


def resolve_path(raw: str, base_dir: Path | None = None) -> Path:
    """Expand ``~`` and environment variables; anchor relative paths at ``base_dir``."""
    expanded = Path(os.path.expandvars(os.path.expanduser(raw)))
    if not expanded.is_absolute() and base_dir is not None:
        expanded = base_dir / expanded
    return expanded


def load_config(path: Path | None = None, validate: bool = True) -> Config:
    """Load ``config.yaml`` and the sibling ``.env`` file.

    Args:
        path: Config file to load. Defaults to ``config.yaml`` next to ``main.py``.
        validate: Check types and ranges (see :mod:`core.config_schema`).

    Raises:
        ConfigError: If the file is missing, isn't valid YAML, or has bad values.
            The message lists every problem, one per line.
    """
    path = path or DEFAULT_CONFIG_PATH
    if not path.is_file():
        raise ConfigError(f"Config file not found: {path}")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"config.yaml is not valid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError("config.yaml must contain a mapping at the top level")
    if validate:
        from core.config_schema import validate_config

        problems = validate_config(data)
        if problems:
            lines = "\n".join(f"  - {p}" for p in problems)
            raise ConfigError(f"{path.name} has {len(problems)} problem(s):\n{lines}")

    env_file = path.parent / ".env"
    if env_file.is_file():
        load_dotenv(env_file, override=False)
        log.debug("Loaded environment from %s", env_file)
    return Config(data, path.parent)
