"""The J.A.R.V.I.S. HUD: a GPU-rendered heads-up display in a browser "app" window.

tkinter can't do glow, transparency or smooth anti-aliased animation, so the
HUD is a local web page (``ui/hud/``) drawn by Microsoft Edge (or Chrome) in
``--app`` mode: a plain window with no browser bars. Jarvis serves it from a
tiny HTTP server on 127.0.0.1 and pushes live events to it over Server-Sent
Events; typed commands and the mic button come back as small POST requests.

* Every request needs a random per-run token, and the Host header must be the
  loopback address, so other local programs and web pages can't drive Jarvis.
* Closing the HUD window just closes the browser window; Jarvis keeps running
  in the tray. ``stop()`` (Quit) tells the page to close and ends the browser.
* All methods are thread-safe: the voice loop, tray and mic callback call them.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import secrets
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from collections import deque
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Protocol
from urllib.parse import parse_qs, urlsplit

from core.state import AssistantState

log = logging.getLogger(__name__)

S = AssistantState

ASSETS = Path(__file__).with_name("hud")
STATIC = {
    "/hud.css": "text/css; charset=utf-8",
    "/hud.js": "text/javascript; charset=utf-8",
}
WINDOW_TITLE = "J.A.R.V.I.S."
CHAT_LIMIT = 80
LOG_LIMIT = 60
MAX_POST_BYTES = 8192
CLOSE_GRACE_S = 2.5   # a reload reconnects within this; longer means the window was closed


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def mic_level(rms: float) -> float:
    """Map microphone RMS to 0..1 on a dB scale (-55 dB is silence, -15 dB is loud speech)."""
    import math

    if rms <= 0:
        return 0.0
    return min(1.0, max(0.0, (20 * math.log10(rms) + 55) / 40))


class ChatModel:
    """The conversation the HUD shows (and replays to a freshly opened window)."""

    def __init__(self, limit: int = CHAT_LIMIT, clock: Callable[[], datetime] = datetime.now) -> None:
        self.limit = limit
        self.clock = clock
        self.messages: list[dict[str, Any]] = []

    def stamp(self) -> str:
        return self.clock().strftime("%H:%M:%S")

    def add_user(self, text: str) -> dict[str, Any] | None:
        text = text.strip()
        if not text:
            return None
        self.finish_reply()
        return self._append({"role": "user", "text": text, "time": self.stamp(), "streaming": False})

    def add_reply_chunk(self, chunk: str) -> dict[str, Any] | None:
        if not chunk:
            return None
        last = self.messages[-1] if self.messages else None
        if last is not None and last["role"] == "assistant" and last["streaming"]:
            last["text"] += chunk
            return last
        return self._append({"role": "assistant", "text": chunk.lstrip(), "time": self.stamp(), "streaming": True})

    def finish_reply(self) -> None:
        if self.messages and self.messages[-1]["streaming"]:
            self.messages[-1]["streaming"] = False
            self.messages[-1]["text"] = self.messages[-1]["text"].strip()

    def add_notice(self, text: str) -> dict[str, Any] | None:
        """Something Jarvis said outside a reply: the greeting, a reminder, a question."""
        text = text.strip()
        if not text:
            return None
        self.finish_reply()
        return self._append({"role": "assistant", "text": text, "time": self.stamp(), "streaming": False})

    def _append(self, message: dict[str, Any]) -> dict[str, Any]:
        self.messages.append(message)
        del self.messages[: max(0, len(self.messages) - self.limit)]
        return message


def find_browser() -> str | None:
    """Edge (always on Windows 10/11), then Chrome/Brave/Chromium. None if there is none."""
    candidates: list[Path] = []
    if sys.platform == "win32":
        roots = [os.environ.get(k) for k in ("PROGRAMFILES(X86)", "PROGRAMFILES", "LOCALAPPDATA")]
        for root in filter(None, roots):
            for rel in (r"Microsoft\Edge\Application\msedge.exe", r"Google\Chrome\Application\chrome.exe",
                        r"BraveSoftware\Brave-Browser\Application\brave.exe"):
                candidates.append(Path(root) / rel)
    for path in candidates:
        if path.is_file():
            return str(path)
    for name in ("msedge", "microsoft-edge", "google-chrome", "chrome", "chromium", "chromium-browser", "brave"):
        found = shutil.which(name)
        if found:
            return found
    return None


def default_profile_dir() -> Path:
    """Browser cache for the HUD window: per-user app data, never the project folder."""
    base = os.environ.get("LOCALAPPDATA")
    return (Path(base) if base else Path.home() / ".cache") / "Jarvis" / "hud-browser"


def screen_size() -> tuple[int, int]:
    if sys.platform == "win32":
        try:
            import ctypes

            user32 = ctypes.windll.user32  # type: ignore[attr-defined]
            return int(user32.GetSystemMetrics(0)), int(user32.GetSystemMetrics(1))
        except Exception:  # noqa: BLE001
            pass
    return 1920, 1080


def browser_command(exe: str, url: str, profile: Path, screen: tuple[int, int]) -> list[str]:
    """Chromium flags for a chrome-less app window, centred, with its own profile.

    The separate profile makes it a separate browser process that Jarvis owns and
    can close, and keeps the HUD out of the user's normal browsing.
    """
    sw, sh = screen
    w, h = min(1680, int(sw * 0.84)), min(1020, int(sh * 0.86))
    x, y = max(0, (sw - w) // 2), max(0, (sh - h) // 2 - 20)
    return [
        exe,
        f"--app={url}",
        f"--user-data-dir={profile}",
        f"--window-size={w},{h}",
        f"--window-position={x},{y}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-background-mode",
        "--disable-features=Translate,msEdgeSidebarV2,msHubApps",
        "--autoplay-policy=no-user-gesture-required",
    ]


def focus_window(title: str = WINDOW_TITLE) -> bool:
    """Windows: restore and raise the HUD window. False if it isn't open."""
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        user32 = ctypes.windll.user32  # type: ignore[attr-defined]
        hwnd = user32.FindWindowW(None, title)
        if not hwnd:
            return False
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        user32.SetForegroundWindow(hwnd)
        return True
    except Exception:  # noqa: BLE001
        log.debug("Could not focus the HUD window", exc_info=True)
        return False


class StatsSampler:
    """Live system readouts for the HUD's diagnostics panel (psutil; {} if unavailable)."""

    def __init__(self) -> None:
        self._net: tuple[float, int, int] | None = None
        try:
            import psutil

            self.psutil: Any = psutil
            psutil.cpu_percent(None)  # prime: the first call always returns 0
        except Exception:  # noqa: BLE001
            self.psutil = None

    def sample(self) -> dict[str, Any]:
        ps = self.psutil
        if ps is None:
            return {}
        try:
            mem = ps.virtual_memory()
            disk = ps.disk_usage(Path.home().anchor or "/")
            net = ps.net_io_counters()
            now = time.monotonic()
            down = up = 0.0
            if self._net is not None and net is not None:
                dt = max(1e-3, now - self._net[0])
                down = (net.bytes_recv - self._net[1]) / dt / 1024
                up = (net.bytes_sent - self._net[2]) / dt / 1024
            if net is not None:
                self._net = (now, net.bytes_recv, net.bytes_sent)
            stats: dict[str, Any] = {
                "cpu": round(ps.cpu_percent(None), 1),
                "cores": ps.cpu_count() or 0,
                "mem": round(mem.percent, 1),
                "mem_used": round(mem.used / 2**30, 1),
                "mem_total": round(mem.total / 2**30, 1),
                "disk": round(disk.percent, 1),
                "down": round(max(0.0, down), 1),
                "up": round(max(0.0, up), 1),
                "procs": len(ps.pids()),
                "uptime": int(time.time() - ps.boot_time()),
            }
            freq = ps.cpu_freq() if hasattr(ps, "cpu_freq") else None
            if freq:
                stats["ghz"] = round(freq.current / 1000, 2)
            battery = ps.sensors_battery() if hasattr(ps, "sensors_battery") else None
            if battery is not None:
                stats["battery"] = round(battery.percent)
                stats["plugged"] = bool(battery.power_plugged)
            return stats
        except Exception:  # noqa: BLE001 - a stats hiccup must never break the HUD
            log.debug("Stats sample failed", exc_info=True)
            return {}


class HudActions(Protocol):
    """What the HUD asks the app to do (called from server threads)."""

    def submit_text(self, text: str) -> None: ...
    def toggle_mute(self) -> None: ...
    def is_muted(self) -> bool: ...
    def current_mode(self) -> str: ...  # "wake" | "push-to-talk"
    def window_closed(self) -> None: ...


Launcher = Callable[[str], Any]


# ---------------------------------------------------------------------------
# The HUD
# ---------------------------------------------------------------------------


class HudWindow:
    """Serves the HUD and keeps every open copy of it up to date.

    Args:
        actions: App callbacks (typed messages, mute, window closed).
        name: Assistant name.
        user_name: Shown in the boot sequence.
        hotkey: Push-to-talk key, shown in hints.
        start_hidden: Don't open the window until :meth:`show` (start in the tray).
        level: Returns the latest microphone RMS (drives the listening animation).
        profile_dir: Browser profile folder for the app window.
        launcher: Opens the window for a URL (default: Edge/Chrome app mode); for tests.
        stats: Sample live system stats for the diagnostics panel.
    """

    def __init__(
        self,
        actions: HudActions,
        name: str = "Jarvis",
        user_name: str = "",
        hotkey: str = "f9",
        start_hidden: bool = False,
        level: Callable[[], float] | None = None,
        profile_dir: Path | None = None,
        launcher: Launcher | None = None,
        stats: bool = True,
        close_grace_s: float = CLOSE_GRACE_S,
    ) -> None:
        self.actions = actions
        self.name = name
        self.user_name = user_name
        self.hotkey = hotkey
        self.start_hidden = start_hidden
        self.level = level or (lambda: 0.0)
        self.profile_dir = profile_dir or default_profile_dir()
        self.launcher = launcher or self._launch_browser
        self.close_grace_s = close_grace_s
        self.token = secrets.token_urlsafe(18)
        self.model = ChatModel()
        self.log_lines: deque[dict[str, str]] = deque(maxlen=LOG_LIMIT)
        self.state = AssistantState.IDLE
        self.note = "starting"
        self.error = False
        self.muted = False
        self.mode = "wake"
        self._stats = StatsSampler() if stats else None
        self._lock = threading.Lock()
        self._clients: list[queue.Queue[bytes | None]] = []
        self._stop = threading.Event()
        self._server: ThreadingHTTPServer | None = None
        self._threads: list[threading.Thread] = []
        self._proc: subprocess.Popen[bytes] | None = None
        self._connected_once = False
        self._empty_since: float | None = None
        self._close_reported = True

    # -- lifecycle --------------------------------------------------------------------

    @property
    def port(self) -> int:
        return self._server.server_address[1] if self._server else 0

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/?t={self.token}"

    def start(self) -> None:
        self._server = _HudServer(self)
        self._threads = [
            threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.25}, name="hud-server", daemon=True),
            threading.Thread(target=self._ticker, name="hud-ticker", daemon=True),
        ]
        for thread in self._threads:
            thread.start()
        log.info("HUD at http://127.0.0.1:%d/", self.port)
        self._log("SYS", "Core online. HUD link established on loopback.")
        if not self.start_hidden:
            self.show()

    def stop(self) -> None:
        if self._stop.is_set():
            return
        self._stop.set()
        self._broadcast({"type": "shutdown"})
        deadline = time.monotonic() + 1.0
        while self.client_count and time.monotonic() < deadline:
            time.sleep(0.05)  # let the page close itself
        with self._lock:
            for q in self._clients:
                q.put(None)
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        for thread in self._threads:
            if thread is not threading.current_thread():
                thread.join(timeout=2)
        proc = self._proc
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except Exception:  # noqa: BLE001
                pass

    def show(self) -> None:
        """Open the HUD window, or bring it to the front if it is already open."""
        if self._server is None or self._stop.is_set():
            return
        if self.client_count and focus_window():
            return
        if self.client_count and self._proc is not None and self._proc.poll() is None:
            return  # open but couldn't be raised (e.g. not Windows); don't open a second copy
        try:
            self.launcher(self.url)
        except Exception:  # noqa: BLE001
            log.exception("Could not open the HUD window")

    def hide(self) -> None:
        """Nothing to do: the user closes the browser window themselves."""

    def _launch_browser(self, url: str) -> None:
        exe = find_browser()
        if exe is None:
            log.info("No Chromium browser found; opening the HUD in the default browser")
            webbrowser.open(url)
            return
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        cmd = browser_command(exe, url, self.profile_dir, screen_size())
        flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0  # type: ignore[attr-defined]
        self._proc = subprocess.Popen(  # noqa: S603 - fixed browser path, our own URL
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags
        )

    # -- inputs from the app (any thread) ---------------------------------------------

    def on_state(self, _old: AssistantState, new: AssistantState, reason: str = "") -> None:
        with self._lock:
            self.state = new
            self._send({"type": "state", "state": new.value, "reason": reason})
            label = new.name.replace("_", " ")
            self._log_locked("CORE", f"{label}" + (f"  //  {reason}" if reason else ""))

    def on_transcript(self, text: str) -> None:
        with self._lock:
            message = self.model.add_user(text)
            if message:
                self._send({"type": "user", **message})
                self._log_locked("INPUT", text.strip()[:90])

    def on_reply(self, chunk: str) -> None:
        with self._lock:
            message = self.model.add_reply_chunk(chunk)
            if message:
                self._send({"type": "reply", "chunk": chunk, "time": message["time"]})

    def on_reply_done(self) -> None:
        with self._lock:
            self.model.finish_reply()
            self._send({"type": "done"})

    def on_notice(self, text: str) -> None:
        with self._lock:
            message = self.model.add_notice(text)
            if message:
                self._send({"type": "notice", **message})

    def on_status(self, state: str, detail: str) -> None:
        if state == "tool":
            self._broadcast({"type": "tool", "name": detail})
            self._log("EXEC", f"Running module {detail}")

    def set_status_note(self, note: str) -> None:
        with self._lock:
            if note == self.note:
                return
            self.note = note
            self._send({"type": "note", "note": note})
            text = {"offline": "Uplink lost. Local protocols only.", "": "Uplink stable.",
                    "loading": "Loading speech recognition matrix.", "starting": "Boot sequence."}.get(note, note)
            self._log_locked("NET" if note in ("offline", "") else "SYS", text)

    def set_error(self, error: bool) -> None:
        with self._lock:
            if error == self.error:
                return
            self.error = error
            self._send({"type": "error", "error": error})
            self._log_locked("ALERT" if error else "SYS", "Fault detected. Recovering." if error else "Systems nominal.")

    # -- server side ----------------------------------------------------------------------

    @property
    def client_count(self) -> int:
        with self._lock:
            return len(self._clients)

    def snapshot(self) -> dict[str, Any]:
        """Everything a freshly opened window needs (call with the lock held)."""
        return {
            "type": "hello",
            "name": self.name,
            "user": self.user_name,
            "hotkey": self.hotkey,
            "state": self.state.value,
            "note": self.note,
            "error": self.error,
            "muted": self.muted,
            "mode": self.mode,
            "messages": list(self.model.messages),
            "log": list(self.log_lines),
        }

    def connect(self) -> queue.Queue[bytes | None]:
        q: queue.Queue[bytes | None] = queue.Queue()
        with self._lock:
            q.put(json.dumps(self.snapshot()).encode())
            self._clients.append(q)
            self._connected_once = True
            self._empty_since = None
            self._close_reported = False
        return q

    def disconnect(self, q: queue.Queue[bytes | None]) -> None:
        with self._lock:
            if q in self._clients:
                self._clients.remove(q)
            if not self._clients:
                self._empty_since = time.monotonic()

    def handle_post(self, path: str, body: dict[str, Any]) -> int:
        """A request from the page. Returns the HTTP status."""
        if path == "/api/send":
            text = str(body.get("text", "")).strip()[:2000]
            if text:
                self.actions.submit_text(text)
            return 204
        if path == "/api/mute":
            self.actions.toggle_mute()
            self._poll_controls(force=True)
            return 204
        return 404

    def _send(self, message: dict[str, Any]) -> None:
        data = json.dumps(message).encode()
        for q in self._clients:
            q.put(data)

    def _broadcast(self, message: dict[str, Any]) -> None:
        with self._lock:
            self._send(message)

    def _log_locked(self, tag: str, text: str) -> None:
        line = {"time": datetime.now().strftime("%H:%M:%S"), "tag": tag, "text": text}
        self.log_lines.append(line)
        self._send({"type": "log", **line})

    def _log(self, tag: str, text: str) -> None:
        with self._lock:
            self._log_locked(tag, text)

    def _poll_controls(self, force: bool = False) -> None:
        try:
            muted, mode = bool(self.actions.is_muted()), str(self.actions.current_mode())
        except Exception:  # noqa: BLE001
            return
        with self._lock:
            if force or muted != self.muted or mode != self.mode:
                if muted != self.muted:
                    self._log_locked("AUDIO", "Microphone disengaged." if muted else "Microphone engaged.")
                self.muted, self.mode = muted, mode
                self._send({"type": "controls", "muted": muted, "mode": mode})

    def _ticker(self) -> None:
        """Mic level at 20 Hz while listening, stats every second, controls, close detection."""
        last_stats = last_controls = 0.0
        last_level = -1.0
        while not self._stop.wait(0.05):
            now = time.monotonic()
            if now - last_controls >= 0.25:
                last_controls = now
                self._poll_controls()
            if self.client_count:
                if self.state in (S.LISTENING, S.FOLLOW_UP, S.WAKE_DETECTED):
                    level = round(mic_level(float(self.level() or 0.0)), 3)
                    if abs(level - last_level) >= 0.01:
                        last_level = level
                        self._broadcast({"type": "level", "v": level})
                if self._stats is not None and now - last_stats >= 1.0:
                    last_stats = now
                    stats = self._stats.sample()
                    if stats:
                        self._broadcast({"type": "stats", **stats})
            self._check_closed(now)

    def _check_closed(self, now: float) -> None:
        with self._lock:
            closed = (
                self._connected_once and not self._clients and not self._close_reported
                and self._empty_since is not None and now - self._empty_since >= self.close_grace_s
            )
            if closed:
                self._close_reported = True
        if closed and not self._stop.is_set():
            log.info("HUD window closed")
            try:
                self.actions.window_closed()
            except Exception:  # noqa: BLE001
                log.exception("Window-closed handler failed")


class _HudServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, hud: HudWindow) -> None:
        self.hud = hud
        super().__init__(("127.0.0.1", 0), _Handler)


class _Handler(BaseHTTPRequestHandler):
    server: _HudServer
    server_version = "JarvisHUD"
    sys_version = ""

    def log_message(self, fmt: str, *args: Any) -> None:
        log.debug("HUD %s - " + fmt, self.client_address[0], *args)

    def _host_ok(self) -> bool:
        port = self.server.server_address[1]
        return self.headers.get("Host", "") in (f"127.0.0.1:{port}", f"localhost:{port}")

    def _token_ok(self, query: dict[str, list[str]]) -> bool:
        given = self.headers.get("X-Jarvis-Token") or (query.get("t") or [""])[0]
        return secrets.compare_digest(given, self.server.hud.token)

    def _reply(self, status: int, body: bytes = b"", content_type: str = "text/plain; charset=utf-8") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        if not self._host_ok():
            return self._reply(403, b"forbidden")
        parts = urlsplit(self.path)
        query = parse_qs(parts.query)
        if parts.path in STATIC:
            return self._reply(200, (ASSETS / parts.path.lstrip("/")).read_bytes(), STATIC[parts.path])
        if not self._token_ok(query):
            return self._reply(403, b"forbidden")
        if parts.path == "/":
            return self._reply(200, (ASSETS / "index.html").read_bytes(), "text/html; charset=utf-8")
        if parts.path == "/events":
            return self._events()
        return self._reply(404, b"not found")

    def do_POST(self) -> None:  # noqa: N802
        parts = urlsplit(self.path)
        if not self._host_ok() or not self._token_ok({}):
            return self._reply(403, b"forbidden")
        if not self.headers.get("Content-Type", "").startswith("application/json"):
            return self._reply(415, b"json only")
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_POST_BYTES:
            return self._reply(413, b"too large")
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return self._reply(400, b"bad json")
        if not isinstance(body, dict):
            return self._reply(400, b"bad json")
        status = self.server.hud.handle_post(parts.path, body)
        self._reply(status)

    def _events(self) -> None:
        hud = self.server.hud
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        q = hud.connect()
        try:
            while True:
                try:
                    item = q.get(timeout=10)
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    continue
                if item is None:
                    break
                self.wfile.write(b"data: " + item + b"\n\n")
                self.wfile.flush()
        except OSError:  # the window closed or reloaded
            pass
        finally:
            hud.disconnect(q)
            self.close_connection = True
