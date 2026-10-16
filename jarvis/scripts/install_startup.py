"""Register Jarvis to start when you log in to Windows, and/or put a shortcut on the desktop.

Usage (from the jarvis folder, with the Python that has Jarvis's requirements):
    python scripts\\install_startup.py                  # start at login (Task Scheduler)
    python scripts\\install_startup.py --desktop        # start at login + desktop shortcut
    python scripts\\install_startup.py --desktop-only   # desktop shortcut only
    python scripts\\install_startup.py --run-key        # registry Run key instead of Task Scheduler
    python scripts\\install_startup.py --status
    python scripts\\install_startup.py --push-to-talk   # start in push-to-talk mode
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.startup import DEFAULT_ARGS, create_desktop_shortcut, install, status  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Start Jarvis at Windows login")
    parser.add_argument("--run-key", action="store_true", help="use the registry Run key instead of Task Scheduler")
    parser.add_argument("--status", action="store_true", help="show what is registered")
    parser.add_argument("--push-to-talk", action="store_true", help="start in push-to-talk mode instead of wake word")
    parser.add_argument("--desktop", action="store_true", help="also put a Jarvis shortcut on the desktop")
    parser.add_argument("--desktop-only", action="store_true", help="only create the desktop shortcut")
    args = parser.parse_args()
    if args.status:
        print(status())
        return 0
    launch_args = ["--push-to-talk", "--tray"] if args.push_to_talk else list(DEFAULT_ARGS)
    try:
        if not args.desktop_only:
            print(install(launch_args, use_task=not args.run_key))
            print("Jarvis will start about 30 seconds after you log in. Remove it with scripts\\uninstall_startup.py")
        if args.desktop or args.desktop_only:
            print(f"Desktop shortcut created: {create_desktop_shortcut(launch_args)}")
    except (OSError, FileNotFoundError) as exc:
        print(f"Failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
