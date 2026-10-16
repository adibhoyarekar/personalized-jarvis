"""Logging configuration: a rotating file log plus quiet console output."""

from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path


def setup_logging(logs_dir: Path, level: str = "INFO", console: bool = False) -> Path:
    """Configure the root logger.

    Args:
        logs_dir: Folder for ``jarvis.log`` (created if missing).
        level: Log level name for the file log.
        console: Also echo WARNING+ to stderr (kept off in text mode so the
            conversation stays readable; errors are reported to the user anyway).

    Returns:
        Path of the main log file.
    """
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_file = logs_dir / "jarvis.log"

    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    for handler in list(root.handlers):
        root.removeHandler(handler)

    file_handler = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=2_000_000, backupCount=4, encoding="utf-8"  # 5 files x 2 MB
    )
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    )
    root.addHandler(file_handler)

    if console:
        stream = logging.StreamHandler()
        stream.setLevel(logging.WARNING)
        stream.setFormatter(logging.Formatter("[%(levelname)s] %(name)s: %(message)s"))
        root.addHandler(stream)

    # Third-party HTTP chatter is noise at INFO.
    for noisy in ("httpx", "httpcore", "anthropic", "urllib3", "primp"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return log_file
