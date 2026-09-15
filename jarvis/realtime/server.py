"""A small web server so a browser or phone can join the call.

Security posture, deliberately stricter than the usual example code:

* Binds to **localhost only** unless you explicitly pass --lan.
* On the network, a six-digit PIN is **required** - there is no way to turn it
  off - and the check is rate limited, because six digits only means something
  if guessing is slow.
* It hands out a short-lived, room-scoped LiveKit token. It never serves your
  API secret, never runs shell commands, and never touches your firewall.

That last point matters: a voice assistant with a listening socket is a way into
your machine, so this one is as small and as closed as it can be.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from ..core.events import log
from .tokens import TokenError, mint, new_pin

WEB_DIR = Path(__file__).parent / "web"

#: Slow enough that guessing a six-digit PIN is hopeless, loose enough that a
#: fat-fingered entry isn't punished.
_MAX_ATTEMPTS = 8
_LOCKOUT_SECONDS = 300


class _Gate:
    """Tracks PIN attempts per client address."""

    def __init__(self, pin: str | None) -> None:
        self.pin = pin
        self._failures: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def check(self, client: str, offered: str | None) -> tuple[bool, str]:
        if not self.pin:
            return True, ""

        now = time.monotonic()
        with self._lock:
            recent = [t for t in self._failures.get(client, [])
                      if now - t < _LOCKOUT_SECONDS]
            self._failures[client] = recent
            if len(recent) >= _MAX_ATTEMPTS:
                return False, "too many attempts - wait five minutes"

        if offered and str(offered).strip() == self.pin:
            with self._lock:
                self._failures.pop(client, None)
            return True, ""

        with self._lock:
            self._failures.setdefault(client, []).append(now)
        return False, "wrong code"


class _Handler(BaseHTTPRequestHandler):
    config: Any = None
    gate: _Gate = _Gate(None)
    room: str = "jarvis"

    # Quiet by default; the access log is noise on a personal machine.
    def log_message(self, fmt: str, *args: Any) -> None:
        log.debug("realtime web: " + fmt, *args)

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        # This page is only ever served to you; nothing may embed it.
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, payload: dict[str, Any]) -> None:
        self._send(code, json.dumps(payload).encode("utf-8"), "application/json")

    def do_GET(self) -> None:  # noqa: N802 - http.server naming
        path = self.path.split("?", 1)[0]

        if path in ("/", "/index.html"):
            page = WEB_DIR / "index.html"
            if not page.exists():
                self._send(500, b"client page missing", "text/plain")
                return
            self._send(200, page.read_bytes(), "text/html; charset=utf-8")
            return

        if path == "/config":
            self._json(200, {"needs_pin": bool(self.gate.pin),
                             "room": self.room})
            return

        if path.startswith("/token"):
            from urllib.parse import parse_qs, urlparse

            query = parse_qs(urlparse(self.path).query)
            offered = (query.get("pin") or [None])[0]
            client = self.client_address[0]

            ok, why = self.gate.check(client, offered)
            if not ok:
                log.warning("realtime web: refused a token to %s (%s)", client, why)
                self._json(403, {"error": why})
                return

            try:
                self._json(200, mint(self.config, room=self.room))
            except TokenError as exc:
                self._json(500, {"error": str(exc)})
            return

        self._send(404, b"not found", "text/plain")


def local_addresses(port: int) -> list[str]:
    """Addresses this machine can be reached on, for the phone."""
    urls = [f"http://localhost:{port}"]
    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
            address = info[4][0]
            if not address.startswith("127.") and f"http://{address}:{port}" not in urls:
                urls.append(f"http://{address}:{port}")
    except OSError:
        pass
    return urls


def serve(config, port: int = 8787, lan: bool = False,
          room: str = "jarvis") -> tuple[ThreadingHTTPServer, str | None]:
    """Start the web server. Returns (server, pin)."""
    # A PIN is mandatory the moment this is reachable from anywhere but here.
    pin = new_pin() if lan else None

    _Handler.config = config
    _Handler.gate = _Gate(pin)
    _Handler.room = room

    host = "0.0.0.0" if lan else "127.0.0.1"
    server = ThreadingHTTPServer((host, port), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True,
                              name="jarvis-realtime-web")
    thread.start()
    log.info("realtime web server on %s:%d (pin=%s)", host, port,
             "yes" if pin else "no")
    return server, pin
