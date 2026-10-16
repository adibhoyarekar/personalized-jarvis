"""Stop Jarvis from starting at Windows login (removes the task and the Run key)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.startup import uninstall  # noqa: E402


def main() -> int:
    try:
        removed = uninstall()
    except OSError as exc:
        print(f"Failed: {exc}", file=sys.stderr)
        return 1
    print("Removed: " + ", ".join(removed) if removed else "Nothing was registered.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
