"""Register Jarvis to start when you log in to Windows.

Usage (from the jarvis folder, with the virtual environment's Python):
    .venv\\Scripts\\python scripts\\install_startup.py            # Task Scheduler (preferred)
    .venv\\Scripts\\python scripts\\install_startup.py --run-key  # registry Run key instead
    .venv\\Scripts\\python scripts\\install_startup.py --status
    .venv\\Scripts\\python scripts\\install_startup.py --push-to-talk   # start in push-to-talk mode
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.startup import DEFAULT_ARGS, install, status  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Start Jarvis at Windows login")
    parser.add_argument("--run-key", action="store_true", help="use the registry Run key instead of Task Scheduler")
    parser.add_argument("--status", action="store_true", help="show what is registered")
    parser.add_argument("--push-to-talk", action="store_true", help="start in push-to-talk mode instead of wake word")
    args = parser.parse_args()
    if args.status:
        print(status())
        return 0
    launch_args = ["--push-to-talk", "--tray"] if args.push_to_talk else list(DEFAULT_ARGS)
    try:
        print(install(launch_args, use_task=not args.run_key))
    except (OSError, FileNotFoundError) as exc:
        print(f"Failed: {exc}", file=sys.stderr)
        return 1
    print("Jarvis will start about 30 seconds after you log in. Remove it with scripts\\uninstall_startup.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
