"""The local API behind the interface's Settings, Missions and AI panels.

The browser never talks to this directly. It calls the Next.js app, which calls
this from the server side with a shared token. That matters because this
endpoint can write API keys into your vault and schedule work that runs while
you're asleep - so it binds to 127.0.0.1 only, and refuses anything without the
token. A page you happened to have open in another tab cannot reach it, and
neither can anything else on your network.

The token is generated on first run and kept in your JARVIS folder, readable
only by processes running as you.

Run it with:  butler-api.bat  (or it starts with the agent)
"""

from __future__ import annotations

import contextlib
import json
import secrets
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

REPO = Path(__file__).resolve().parents[2]
DEFAULT_PORT = 8765

if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


# --------------------------------------------------------------------------- #
# Token
# --------------------------------------------------------------------------- #

def token_path() -> Path:
    from jarvis import paths

    paths.ensure_dirs()
    return paths.ROOT / "control.token"


def load_token() -> str:
    """The shared secret, created once and reused."""
    path = token_path()
    try:
        existing = path.read_text(encoding="utf-8").strip()
        if existing:
            return existing
    except OSError:
        pass
    fresh = secrets.token_urlsafe(32)
    path.write_text(fresh, encoding="utf-8")
    with contextlib.suppress(OSError):     # best effort; Windows ignores mode
        path.chmod(0o600)
    return fresh


# --------------------------------------------------------------------------- #
# The things the interface can see and change
# --------------------------------------------------------------------------- #

#: Plugins a mission step can call, with a human label for the dropdown.
PLUGINS = {
    "web_search": "Search the web",
    "browser": "Open and read a web page",
    "llm": "Think / write / summarise",
    "notify": "Send me a notification",
    "image_flux": "Generate an image",
    "tts_batch": "Turn text into speech",
    "social_post": "Post to social media",
    "video_kling": "Generate a video",
    "video_heygen": "Generate an avatar video",
    "video_merge": "Merge video and audio",
}

#: Ready-made authorisation sentences, so the risky ones are a checkbox rather
#: than something you have to phrase correctly at midnight. These are the exact
#: wording the grant parser understands.
AUTHORISATIONS = [
    {"id": "spend",
     "label": "May spend money (paid APIs)",
     "sentence": "You have permission to spend money on this task."},
    {"id": "post",
     "label": "May post publicly",
     "sentence": "You have permission to post publicly this time."},
    {"id": "send",
     "label": "May send messages and email",
     "sentence": "You have permission to send messages on my behalf."},
    {"id": "upload",
     "label": "May upload my files",
     "sentence": "You have permission to upload files this time."},
    {"id": "outside",
     "label": "May work outside my workspace folder",
     "sentence": "You have permission to work outside the workspace."},
]


class Control:
    """Reads and writes the same files the rest of JARVIS uses."""

    def __init__(self) -> None:
        from jarvis import paths
        from jarvis.config import KEY_SPECS, Config

        self.paths = paths
        self.config = Config()
        self.key_specs = KEY_SPECS

    # -- settings --------------------------------------------------------- #

    def get_settings(self) -> dict[str, Any]:
        return self.config.settings.as_dict()

    def patch_settings(self, patch: dict[str, Any]) -> dict[str, Any]:
        for dotted, value in patch.items():
            self.config.settings.set(str(dotted), value)
        self.config.settings.save()
        return self.get_settings()

    # -- AI providers ----------------------------------------------------- #

    def providers(self) -> list[dict[str, Any]]:
        """Every AI this build can use, and whether a key is stored for it."""
        out = []
        for spec in self.key_specs:
            out.append({
                "name": spec.name,
                "label": spec.label,
                "where": spec.where,
                "free_tier": getattr(spec, "free_tier", ""),
                "note": getattr(spec, "note", ""),
                "required": bool(getattr(spec, "required", False)),
                "configured": bool(self.config.key(spec.name)),
            })
        return out

    def set_key(self, name: str, value: str) -> dict[str, Any]:
        value = (value or "").strip()
        known = {spec.name for spec in self.key_specs}
        if name not in known:
            raise ValueError(f"{name} isn't a key this build knows about.")
        if not value:
            raise ValueError("The key is empty.")
        # The failure that cost an evening once already: a whole command line
        # pasted into a key box dies later inside an HTTP header, nowhere near
        # here. Catch it at the door instead.
        if any(c.isspace() for c in value):
            raise ValueError(
                "That has a space in it - it looks like more than one thing "
                "got pasted together. Paste only the key.")
        self.config.vault.set(name, value)
        return {"name": name, "configured": True}

    def clear_key(self, name: str) -> dict[str, Any]:
        try:
            self.config.vault.delete(name)
        except Exception:
            self.config.vault.set(name, "")
        return {"name": name, "configured": False}

    # -- missions --------------------------------------------------------- #

    def missions(self) -> list[dict[str, Any]]:
        from jarvis.core.mission import Mission

        out = []
        directory = self.paths.MISSION_DIR
        directory.mkdir(parents=True, exist_ok=True)
        for path in sorted(directory.glob("*.json")):
            try:
                mission = Mission.load(path)
            except Exception as exc:
                out.append({"id": path.stem, "name": path.stem,
                            "broken": str(exc)})
                continue
            data = mission.to_dict()
            data["id"] = mission.id
            out.append(data)
        return out

    def save_mission(self, raw: dict[str, Any]) -> dict[str, Any]:
        from jarvis.core.mission import Mission

        mission = Mission.from_dict(raw)          # validates cron and steps
        path = mission.save(self.paths.MISSION_DIR)
        saved = mission.to_dict()
        saved["id"] = mission.id
        saved["path"] = str(path)
        return saved

    def delete_mission(self, mission_id: str) -> dict[str, Any]:
        safe = "".join(c for c in mission_id if c.isalnum() or c in "_.-")
        if not safe or safe != mission_id:
            raise ValueError("That isn't a valid mission id.")
        path = self.paths.MISSION_DIR / f"{safe}.json"
        if path.exists():
            path.unlink()
        return {"id": safe, "deleted": True}

    def runs(self, limit: int = 20) -> list[dict[str, Any]]:
        from jarvis.core.memory import Memory

        try:
            return Memory().runs(limit=limit)
        except Exception:
            return []

    # -- memory ----------------------------------------------------------- #

    def facts(self) -> list[dict[str, Any]]:
        from jarvis.core.memory import Memory

        try:
            return Memory().all_facts()
        except Exception:
            return []

    def remember(self, key: str, value: str) -> dict[str, Any]:
        from jarvis.core.memory import Memory

        Memory().remember(key.strip().lower(), value.strip(),
                          category="personal", source="interface")
        return {"key": key.strip().lower(), "value": value.strip()}

    def forget(self, key: str) -> dict[str, Any]:
        from jarvis.core.memory import Memory

        return {"key": key, "forgotten": Memory().forget(key)}

    def conversations(self, limit: int = 20) -> list[dict[str, Any]]:
        from jarvis.core.memory import Memory

        try:
            return Memory().recent_conversations(limit=limit)
        except Exception:
            return []

    def transcript(self, conversation_id: int, limit: int = 200) -> list[dict[str, Any]]:
        from jarvis.core.memory import Memory

        try:
            history = Memory().history(int(conversation_id), limit=limit)
        except Exception:
            return []
        return [{"role": getattr(m, "role", ""),
                 "content": getattr(m, "content", ""),
                 "at": str(getattr(m, "created_at", ""))} for m in history]

    # -- custom commands --------------------------------------------------- #

    def commands(self) -> list[dict[str, Any]]:
        book = self._rulebook()
        if book is None:
            return []
        return [rule.to_dict() for rule in book.all()]

    def add_command(self, trigger: str, response: str) -> dict[str, Any]:
        book = self._rulebook()
        if book is None:
            raise ValueError("The rule book isn't available.")
        rule = book.create(trigger.strip(), response.strip())
        return rule.to_dict()

    def delete_command(self, rule_id: str) -> dict[str, Any]:
        book = self._rulebook()
        if book is None:
            raise ValueError("The rule book isn't available.")
        return {"id": rule_id, "deleted": book.remove(rule_id)}

    def _rulebook(self) -> Any:
        try:
            from jarvis.core.rules import RuleBook

            return RuleBook(self.config.settings)
        except Exception:
            return None

    # -- everything at once ------------------------------------------------ #

    def state(self) -> dict[str, Any]:
        return {
            "settings": self.get_settings(),
            "providers": self.providers(),
            "missions": self.missions(),
            "facts": self.facts(),
            "commands": self.commands(),
            "conversations": self.conversations(limit=12),
            "runs": self.runs(limit=12),
            "plugins": PLUGINS,
            "authorisations": AUTHORISATIONS,
        }


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #

class _Handler(BaseHTTPRequestHandler):
    control: Control
    token: str = ""

    def log_message(self, fmt: str, *args: Any) -> None:
        pass                                   # quiet; this is a local service

    # -- plumbing --------------------------------------------------------- #

    def _send(self, code: int, payload: Any) -> None:
        body = json.dumps(payload, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _authorised(self) -> bool:
        return secrets.compare_digest(
            self.headers.get("X-Jarvis-Token", ""), self.token)

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"That wasn't valid JSON: {exc}") from exc
        return data if isinstance(data, dict) else {"value": data}

    # -- routes ------------------------------------------------------------ #

    def do_GET(self) -> None:
        self._route("GET")

    def do_POST(self) -> None:
        self._route("POST")

    def do_DELETE(self) -> None:
        self._route("DELETE")

    def _route(self, method: str) -> None:
        if not self._authorised():
            self._send(403, {"error": "bad or missing token"})
            return

        path = urlparse(self.path).path.rstrip("/")
        parts = [unquote(p) for p in path.strip("/").split("/") if p]
        if not parts or parts[0] != "api":
            self._send(404, {"error": "not found"})
            return
        parts = parts[1:]

        try:
            self._send(200, self._dispatch(method, parts))
        except ValueError as exc:
            self._send(400, {"error": str(exc)})
        except Exception as exc:               # pragma: no cover - defensive
            self._send(500, {"error": str(exc)})

    def _dispatch(self, method: str, parts: list[str]) -> Any:
        control = self.control
        head = parts[0] if parts else ""
        rest = parts[1:]

        if head == "state" and method == "GET":
            return control.state()

        if head == "settings":
            if method == "GET":
                return control.get_settings()
            if method == "POST":
                return control.patch_settings(self._body())

        if head == "providers":
            if method == "GET":
                return control.providers()
            if method == "POST":
                body = self._body()
                if rest[:1] == ["clear"]:
                    return control.clear_key(str(body.get("name", "")))
                return control.set_key(str(body.get("name", "")),
                                       str(body.get("value", "")))

        if head == "missions":
            if method == "GET":
                return control.missions()
            if method == "POST":
                return control.save_mission(self._body())
            if method == "DELETE" and rest:
                return control.delete_mission(rest[0])

        if head == "runs" and method == "GET":
            return control.runs()

        if head == "memory":
            if rest[:1] == ["facts"]:
                if method == "GET":
                    return control.facts()
                if method == "POST":
                    body = self._body()
                    return control.remember(str(body.get("key", "")),
                                            str(body.get("value", "")))
                if method == "DELETE" and rest[1:]:
                    return control.forget(rest[1])
            if rest[:1] == ["conversations"] and method == "GET":
                if rest[1:]:
                    return control.transcript(int(rest[1]))
                return control.conversations()

        if head == "commands":
            if method == "GET":
                return control.commands()
            if method == "POST":
                body = self._body()
                return control.add_command(str(body.get("trigger", "")),
                                           str(body.get("response", "")))
            if method == "DELETE" and rest:
                return control.delete_command(rest[0])

        raise ValueError(f"No route for {method} /api/{'/'.join(parts)}")


def serve_in_background(port: int = DEFAULT_PORT) -> bool:
    """Start the control service alongside the agent, and say so out loud.

    Returns False rather than raising: a settings panel that can't be reached
    is a nuisance, but it must never be the reason a call fails to start.
    """
    try:
        serve(port=port, background=True)
        print(f"  Settings service ready on http://127.0.0.1:{port}")
        return True
    except OSError as exc:
        # Already bound is the normal case when the agent restarts quickly.
        if getattr(exc, "errno", None) in (48, 98, 10048):
            print(f"  Settings service already running on port {port}.")
            return True
        print(f"  Settings service could not start: {exc}")
        return False
    except Exception as exc:
        print(f"  Settings service could not start: {exc}")
        print("  The voice will still work; only the settings panel is affected.")
        return False


def serve(port: int = DEFAULT_PORT, background: bool = False) -> ThreadingHTTPServer:
    """Start the control API on localhost."""
    _Handler.control = Control()
    _Handler.token = load_token()

    server = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    if background:
        thread = threading.Thread(target=server.serve_forever, daemon=True,
                                  name="jarvis-control-api")
        thread.start()
    return server


def main() -> int:
    port = DEFAULT_PORT
    if "--port" in sys.argv:
        port = int(sys.argv[sys.argv.index("--port") + 1])

    server = serve(port)
    print(f"\n  Control API on http://127.0.0.1:{port}")
    print(f"  Token file: {token_path()}")
    print("  Localhost only. Ctrl-C to stop.\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("  Stopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
