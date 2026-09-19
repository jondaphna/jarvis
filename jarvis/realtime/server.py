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


class PortInUse(OSError):
    """The web port is taken - said in words, not as a raw socket error."""


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
        self.send_header("X-Content-Type-Options", "nosniff")
        # The page may load the LiveKit SDK and talk to LiveKit, and nothing
        # else. If something ever manages to inject a script tag here, it has
        # nowhere to send what it finds.
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; "
            "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net "
            "https://unpkg.com; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data: blob:; media-src 'self' blob:; "
            "connect-src 'self' https://cdn.jsdelivr.net https://unpkg.com "
            "wss: https:; "
            "base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
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


def _outbound_address() -> str:
    """The address this machine uses to reach the local network.

    Asking the OS to resolve our own hostname is the usual trick and it is not
    reliable: plenty of machines answer 127.0.1.1, and some answer nothing.
    Opening a UDP socket towards the network picks the interface the phone will
    actually come in on. Nothing is sent - UDP connect only sets a route.
    """
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("192.168.255.255", 9))
        return str(probe.getsockname()[0])
    except OSError:
        return ""
    finally:
        probe.close()


def local_addresses(port: int, scheme: str = "http") -> list[str]:
    """Addresses this machine can be reached on, for the phone."""
    urls = [f"{scheme}://localhost:{port}"]

    def offer(address: str) -> None:
        url = f"{scheme}://{address}:{port}"
        if address and not address.startswith("127.") and url not in urls:
            urls.append(url)

    offer(_outbound_address())
    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
            offer(info[4][0])
    except OSError:
        pass
    return urls


def serve(config, port: int = 8787, lan: bool = False,
          room: str = "jarvis") -> tuple[ThreadingHTTPServer, str | None, str]:
    """Start the web server. Returns (server, pin, scheme)."""
    # A PIN is mandatory the moment this is reachable from anywhere but here.
    pin = new_pin() if lan else None

    _Handler.config = config
    _Handler.gate = _Gate(pin)
    _Handler.room = room

    host = "0.0.0.0" if lan else "127.0.0.1"
    try:
        server = ThreadingHTTPServer((host, port), _Handler)
    except OSError as exc:
        raise PortInUse(
            f"Port {port} is already in use ({exc}).\n"
            f"  Another JARVIS may still be running, or something else has it.\n"
            f"  Pick a different one:  python -m jarvis realtime --port {port + 1}"
        ) from exc
    scheme = "http"

    # A phone on http://192.168.x.x is not a "secure context", so the browser
    # refuses microphone access outright - the page loads and the mic button
    # just never works. HTTPS is what makes the phone usable at all.
    if lan:
        try:
            import ssl

            from .certs import ensure

            cert_path, key_path = ensure()
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(str(cert_path), str(key_path))
            server.socket = context.wrap_socket(server.socket, server_side=True)
            scheme = "https"
        except Exception as exc:
            log.warning("couldn't enable HTTPS (%s) - the phone microphone "
                        "will be blocked by the browser", exc)

    thread = threading.Thread(target=server.serve_forever, daemon=True,
                              name="jarvis-realtime-web")
    thread.start()
    log.info("realtime web server on %s://%s:%d (pin=%s)", scheme, host, port,
             "yes" if pin else "no")
    return server, pin, scheme
