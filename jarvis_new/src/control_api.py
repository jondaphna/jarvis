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
import os
import secrets
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

REPO = Path(__file__).resolve().parents[2]
DEFAULT_PORT = 8765

#: The most a request body may declare. This is a local settings API - the
#: biggest thing anyone legitimately sends is a settings document or a routine
#: - and the declared length was previously the size of an allocation chosen
#: by the caller.
MAX_BODY_BYTES = 256 * 1024


class RequestTooLargeError(ValueError):
    """A body bigger than this API will read. Answered with 413."""

if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


# --------------------------------------------------------------------------- #
# Token
# --------------------------------------------------------------------------- #

def token_path() -> Path:
    from jarvis import paths

    paths.ensure_dirs()
    return paths.ROOT / "control.token"


#: How many times to try to establish the token before giving up. More than
#: the old three, because each round now waits: the case being survived is two
#: processes starting together, and the loser needs the winner to have
#: finished writing, not just to have created the file.
TOKEN_ATTEMPTS = 5

#: Base delay between attempts, multiplied by the attempt number.
TOKEN_RETRY_DELAY = 0.05

#: How old an empty token file has to be before it counts as wreckage rather
#: than as a process that is about to write it. Generous: the cost of waiting
#: is a slow start-up, and the cost of being wrong is two processes holding
#: different tokens, which presents as a dashboard that silently does nothing.
TOKEN_STALE_SECONDS = 30.0


def _token_file_is_stale(path: Path) -> bool:
    """Is this empty token file wreckage rather than a write in progress?"""
    try:
        return (time.time() - path.stat().st_mtime) > TOKEN_STALE_SECONDS
    except OSError:
        return False


def load_token() -> str:
    """The shared secret, created once and reused.

    Creation is a single exclusive open rather than "is it there? no? write
    one". Two processes starting together - and `butler-web.bat` starts the
    control API alongside the one `butler-agent.bat` may already have started
    - both used to find no file, both wrote, and each went on believing its
    own token was the one. Whoever loses the race now reads the winner's.

    Raises rather than inventing a token it could not store. See the comment
    at the end for why that is the safer failure.
    """
    path = token_path()
    last_error = ""
    for attempt in range(TOKEN_ATTEMPTS):
        try:
            existing = path.read_text(encoding="utf-8").strip()
            if existing:
                return existing
            # The file is there but empty. That is either the winner of the
            # race a moment before it writes, or a token file a crash left
            # behind. Telling those apart by *age* is the only safe way:
            # deleting it because it happens to be empty right now is deleting
            # the file another process is in the middle of writing, which is
            # how two processes end up with different tokens.
            if not _token_file_is_stale(path):
                time.sleep(TOKEN_RETRY_DELAY * (attempt + 1))
                continue
            with contextlib.suppress(OSError):
                path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            last_error = f"{type(exc).__name__}: {exc}"

        fresh = secrets.token_urlsafe(32)
        try:
            # O_EXCL: the file is created by exactly one caller, and the mode
            # is set as it is created rather than a moment afterwards.
            handle = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            continue                        # somebody else won; read theirs
        except OSError as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            time.sleep(TOKEN_RETRY_DELAY * (attempt + 1))
            continue
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(fresh)
            stream.flush()
            with contextlib.suppress(OSError):
                os.fsync(stream.fileno())
        with contextlib.suppress(OSError):  # best effort; Windows ignores mode
            path.chmod(0o600)
        return fresh

    # This used to return a fresh in-memory token here, on the reasoning that
    # a token only this process knows is still a boundary. It is not: the
    # dashboard proxy reads the *file* to get the token it sends, so a token
    # that never reached the file is a control API that refuses every request
    # the dashboard makes, and the visible symptom is a panel that has simply
    # stopped working with no explanation. Refusing to start says which file
    # and why.
    detail = f" ({last_error})" if last_error else ""
    raise RuntimeError(
        f"couldn't establish the control token at {path}{detail}. The "
        f"dashboard reads this file to talk to JARVIS, so a token that isn't "
        f"in it is no use. Check the file's permissions, or delete it and "
        f"start again.")


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
        #: (checked_at, answer) for the content pipeline's readiness. See
        #: `_content_readiness` for why it is not worked out every time.
        self._readiness_cache: tuple[float, dict[str, Any]] | None = None

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

    # -- lessons: what you have taught it ---------------------------------- #

    def lessons(self, limit: int = 200) -> list[dict[str, Any]]:
        from jarvis.core.memory import Memory

        try:
            return Memory().lessons(limit=limit)
        except Exception:
            return []

    def teach(self, trigger: str, steps: str) -> dict[str, Any]:
        from jarvis.core.memory import Memory

        trigger, steps = trigger.strip(), steps.strip()
        if not trigger or not steps:
            raise ValueError("Both the phrase and the steps are needed.")
        memory = Memory()
        key = memory.learn(trigger, steps, said=trigger,
                           source="corrected" if memory.lesson_for(trigger)
                           else "taught")
        if not key:
            raise ValueError("That phrase has nothing in it to learn.")
        return {"trigger": key, "steps": steps}

    def unlearn(self, trigger: str) -> dict[str, Any]:
        from jarvis.core.memory import Memory

        return {"trigger": trigger, "forgotten": Memory().unlearn(trigger)}

    # -- what thinking costs ------------------------------------------------ #

    def thinking_models(self) -> list[dict[str, Any]]:
        """Every brain you can choose, honest about which ones cost money.

        Built rather than hard-coded so the free options say what they will
        actually use - "Gemini 2.5 Flash", not "whatever Google gives you" -
        and so an Ollama install that appeared this morning shows up.
        """
        import thinker

        options: list[dict[str, Any]] = [{
            "id": "auto",
            "label": "Free - best free brain you have (recommended)",
            "free": True,
            "detail": thinker.describe(),
        }]
        ollama = thinker.Ollama()
        if ollama.available():
            options.append({
                "id": f"ollama:{ollama.model()}",
                "label": f"Free - {ollama.model()} on this machine",
                "free": True,
                "detail": "Runs locally. Nothing you say leaves the computer.",
            })
        gemini = thinker.Gemini()
        if gemini.available():
            options.append({
                "id": gemini.model(),
                "label": f"Free - Google {gemini.model()}",
                "free": True,
                "detail": "Google's free tier, on the key the voice already uses.",
            })
        for model, detail in thinker.PAID_MODELS.items():
            options.append({
                "id": model,
                "label": f"Paid - {detail.split(' - ')[0]}",
                "free": False,
                "detail": detail,
            })
        return options

    def costs(self) -> dict[str, Any]:
        """What is switched on that can spend money, and what has been spent."""
        from jarvis.core.memory import Memory

        import thinker

        try:
            spent = Memory().spend_since(hours=24 * 30)
        except Exception:
            spent = 0.0
        return {
            "allow_paid": thinker.paid_allowed(),
            "using": thinker.describe(),
            "spent_30d_usd": round(float(spent), 4),
            "note": ("Nothing here spends money while paid thinking is off. "
                     "The voice, the browser, memory and learning are free."),
        }

    # -- custom commands --------------------------------------------------- #

    def commands(self) -> list[dict[str, Any]]:
        book = self._rulebook()
        if book is None:
            return []
        return [rule.to_dict() for rule in book.all()]

    def add_command(self, trigger: str, response: str,
                    kind: str = "reply") -> dict[str, Any]:
        """Add a custom command of the kind the user actually meant.

        The kind is not a detail. "Say exactly this" and "always do this" are
        opposite instructions, and everything used to be created as the first
        one - so "every time you open up, do this and that" came out as an
        order to *recite* those words rather than act on them.
        """
        from jarvis.core.rules import KINDS

        book = self._rulebook()
        if book is None:
            raise ValueError("The rule book isn't available.")
        kind = (kind or "reply").strip().lower()
        if kind not in KINDS:
            raise ValueError(f"{kind!r} isn't a kind of command.")
        trigger, response = trigger.strip(), response.strip()
        if not response:
            raise ValueError("A command needs something to do or say.")
        if kind != "instruct" and not trigger:
            raise ValueError("This kind of command needs a phrase to trigger it.")
        rule = book.create(trigger, response, kind=kind)
        return rule.to_dict()

    def orders(self) -> dict[str, Any]:
        """Exactly what the agent is told about your rules, and whether it
        has picked the latest version up.

        Shown in the panel on purpose. "He ignores my commands" and "the
        running agent has never seen my commands" look identical from the
        outside, and this is what tells them apart.
        """
        import personalise

        book = self._rulebook()
        settings = self.config.settings
        try:
            block = personalise.your_orders(settings, book)
            mark = personalise.fingerprint(settings, book)
        except Exception as exc:                  # pragma: no cover - defensive
            return {"text": "", "fingerprint": "", "error": str(exc)}
        return {
            "text": block,
            "fingerprint": mark,
            "note": ("This is word for word what Jarvis is told, before "
                     "anything else. A running call picks up changes within "
                     "a few seconds."),
        }

    def delete_command(self, rule_id: str) -> dict[str, Any]:
        book = self._rulebook()
        if book is None:
            raise ValueError("The rule book isn't available.")
        return {"id": rule_id, "deleted": book.remove(rule_id)}

    # -- standing routines ------------------------------------------------ #

    def routines(self) -> dict[str, Any]:
        """Every routine, the actions available, and how the engine is doing.

        One call rather than three, because the Rules panel draws the whole
        tab at once and three round trips to localhost is three chances for
        the tab to be half-drawn.
        """
        from routines import catalogue
        from routines.engine import get_engine

        runner = get_engine()
        return {
            "routines": runner.listing(),
            "actions": catalogue(),
            "summary": runner.summary(),
            "recent": runner.store.runs(limit=20),
        }

    def save_routine(self, raw: dict[str, Any]) -> dict[str, Any]:
        """Create or edit one. The schedule is validated before it is stored."""
        from routines.engine import get_engine

        return get_engine().save(raw)

    def delete_routine(self, routine_id: str) -> dict[str, Any]:
        from routines.engine import get_engine

        return {"id": routine_id, "deleted": get_engine().store.delete(routine_id)}

    def set_routine_enabled(self, routine_id: str, enabled: bool) -> dict[str, Any]:
        from routines.engine import get_engine

        return {"id": routine_id, "enabled": enabled,
                "changed": get_engine().set_enabled(routine_id, enabled)}

    def run_routine(self, routine_id: str) -> dict[str, Any]:
        """Fire one now, from the panel.

        Returns as soon as the job is queued. It runs on the same background
        host the schedule uses, so pressing the button cannot block the page
        or the conversation, and the result appears in the run history when
        it is ready.
        """
        from routines.engine import get_engine

        return {"id": routine_id, "job": get_engine().run_now(routine_id)}

    def routine_runs(self, routine_id: str = "", limit: int = 20) -> list[dict[str, Any]]:
        from routines.engine import get_engine

        return get_engine().store.runs(routine_id, limit=limit)

    # -- background workers and the content engine ------------------------ #

    def workers(self) -> dict[str, Any]:
        """What the background host is doing, for the dashboard's live panel.

        In-memory only: no database is touched, because this is polled every
        few seconds for as long as the dashboard is open and the one thing
        that must stay cheap is looking at the machine.
        """
        try:
            import workers

            return workers.host().status()
        except Exception as exc:
            return {"running": False, "paused": False, "queued": 0,
                    "in_progress": 0, "concurrency": 1, "jobs": [],
                    "error": str(exc)}

    def services(self) -> dict[str, Any]:
        """What the always-on background side is doing, for the kill switch.

        In-memory only, like `workers()`: the dashboard polls this while it is
        open and looking at the machine has to stay free.
        """
        try:
            import services as service_layer

            return service_layer.state()
        except Exception as exc:
            return {"state": "unknown", "running": False, "killed": False,
                    "error": f"{type(exc).__name__}: {exc}"}

    def set_services(self, action: str) -> dict[str, Any]:
        """The master switch: `kill`, `start` or `restart`.

        What "kill" can and cannot do is worth being exact about, because the
        button says kill and somebody will believe it. It stops the routine
        ticker, cancels every queued and running job, and stops the worker
        host. A queued job never starts. A job that is a coroutine stops at its
        next await.

        A job that is a plain blocking function does not stop: Python cannot
        end a thread from outside, so an FFmpeg render or an API call already
        in flight runs to its end with nobody waiting for the result. It is
        cancelled from every caller's point of view and nothing new follows it.
        The reply says how many were cancelled and what is still winding down
        rather than reporting an empty machine.

        The voice call is deliberately untouched. It is not background work,
        and cutting somebody off mid-sentence because they pressed a button
        labelled "stop the background jobs" is not what the button says.
        """
        import services as service_layer

        wanted = (action or "").strip().lower()
        if wanted in ("kill", "stop", "off"):
            return service_layer.kill(reason="dashboard kill switch")
        if wanted in ("start", "on"):
            return service_layer.start(reason="dashboard")
        if wanted == "restart":
            return service_layer.restart(reason="dashboard")
        raise ValueError(
            f"{action!r} isn't something the services switch does. "
            f"Use 'kill', 'start' or 'restart'.")

    def set_workers_paused(self, paused: bool) -> dict[str, Any]:
        """Hold back background work, or let it flow again.

        Pausing rather than stopping, so the queue survives: press resume and
        everything that was waiting runs, in the order it arrived.
        """
        import workers

        return {"paused": workers.host().set_paused(bool(paused))}

    def _content_readiness(self) -> dict[str, Any]:
        """Which pipeline stages can run, cached for a short while.

        Checking costs a vault decryption, a handful of imports and a PATH
        search per stage. That is fine once; it is not fine every three
        seconds for as long as the dashboard is open, so the answer is held
        for a minute. Nothing in it changes faster than that without a key
        being added, and adding a key reloads the panel anyway.
        """
        now = time.time()
        cached = self._readiness_cache
        if cached and now - cached[0] < 60:
            return cached[1]
        from content import pipeline

        fresh = pipeline.readiness()
        self._readiness_cache = (now, fresh)
        return fresh

    def content(self) -> dict[str, Any]:
        """The whole content side in one call: switch, stages, jobs, scripts.

        Errors are reported in the payload rather than raised. A machine with
        no database yet is the normal state before the first job, and it must
        read as "nothing has run" rather than as a broken panel.
        """
        import permissions

        payload: dict[str, Any] = {
            "enabled": permissions.allowed("content", self.config.settings),
            "workers": self.workers(),
            "jobs": [],
            "scripts": [],
            "counts": {"queued": 0, "running": 0, "done": 0, "failed": 0},
            "stages": [],
            "style": {},
            "error": "",
        }
        try:
            ready = self._content_readiness()
            payload["stages"] = ready.get("stages", [])
            payload["style"] = ready.get("style", {})
        except Exception as exc:
            payload["error"] = str(exc)

        try:
            from content.store import store

            db = store()
            jobs = db.recent_jobs(limit=25)
            payload["jobs"] = jobs
            payload["scripts"] = db.assets(kind="script", limit=12)
            counts = payload["counts"]
            for job in jobs:
                status = str(job.get("status", ""))
                if status in counts:
                    counts[status] += 1
        except Exception as exc:
            payload["error"] = payload["error"] or str(exc)
        return payload

    def queue_content(self, topic: str, count: int = 3,
                      style: str = "") -> dict[str, Any]:
        """Start a script job from the dashboard.

        Refused when the switch is off, rather than quietly queued: a button
        that appears to work while the capability is off is how you end up
        waiting all evening for a job nobody was going to run.
        """
        import permissions

        topic = topic.strip()
        if not topic:
            raise ValueError("Say what the scripts should be about.")
        if not permissions.allowed("content", self.config.settings):
            raise ValueError("The content engine is switched off. Turn it on "
                             "in the permissions matrix first.")
        from content import pipeline

        ref = pipeline.queue_scripts(topic, count=max(1, min(int(count), 10)),
                                     style=style)
        return {"ref": ref, "topic": topic}

    def _rulebook(self) -> Any:
        try:
            from jarvis.core.rules import RuleBook

            return RuleBook(self.config.settings)
        except Exception:
            return None

    def permissions(self) -> list[dict[str, Any]]:
        """Every capability switch and whether it is on."""
        try:
            import permissions

            return permissions.current(self.config.settings)
        except Exception:
            return []

    def browser_profiles(self) -> list[dict[str, str]]:
        """The Chrome profiles on this machine, named as Chrome names them."""
        try:
            from chrome_finder import list_profiles, preferred_profile
        except Exception:
            return []
        try:
            configured = str(self.config.settings.get("browser.profile", "") or "")
            active = preferred_profile(configured)
            return [{**p, "active": p["directory"] == active}
                    for p in list_profiles()]
        except Exception:
            return []

    # -- everything at once ------------------------------------------------ #

    def _routines_for_state(self) -> dict[str, Any]:
        """The routines, folded into the one call that draws the whole panel.

        Wrapped because `state` is what the page loads with: one broken
        import here should cost the Rules tab its routine list, not cost the
        settings page every other tab as well.
        """
        try:
            return self.routines()
        except Exception as exc:
            return {"routines": [], "actions": [], "summary": {},
                    "recent": [], "error": str(exc)}

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
            "browser_profiles": self.browser_profiles(),
            "permissions": self.permissions(),
            "thinking_models": self.thinking_models(),
            "costs": self.costs(),
            "orders": self.orders(),
            "lessons": self.lessons(),
            "routines": self._routines_for_state(),
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
        """The request body, as an object, within a size this can hold.

        Three things this used to trust and no longer does. The declared
        length: `int()` of a header nobody checked, then a read of exactly
        that many bytes, which is an allocation chosen by the caller. A
        negative or unparseable length, which reached `rfile.read` as-is. And
        a JSON scalar, which was wrapped as `{"value": ...}` and let a request
        that does not match the contract go on to be handled as though it did.
        """
        raw_length = (self.headers.get("Content-Length") or "0").strip()
        try:
            length = int(raw_length)
        except ValueError:
            raise ValueError("Content-Length isn't a number.") from None
        if length < 0:
            raise ValueError("Content-Length can't be negative.")
        if length > MAX_BODY_BYTES:
            raise RequestTooLargeError(
                f"That request body is {length} bytes; the limit is "
                f"{MAX_BODY_BYTES}.")
        if not length:
            return {}

        raw = self.rfile.read(length)
        if len(raw) != length:
            raise ValueError("The request body ended early.")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"That wasn't valid JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise ValueError("The request body has to be a JSON object.")
        return data

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
        except RequestTooLargeError as exc:
            self._send(413, {"error": str(exc)})
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

        if head == "lessons":
            if method == "GET":
                return control.lessons()
            if method == "POST":
                body = self._body()
                # Deleting by POST rather than by path. A spoken trigger is a
                # sentence with spaces in it, and putting one through two
                # proxies as a URL segment is a round of encoding bugs nobody
                # needs; in a body it is just a string.
                if rest[:1] == ["forget"]:
                    return control.unlearn(str(body.get("trigger", "")))
                return control.teach(str(body.get("trigger", "")),
                                     str(body.get("steps", "")))
            if method == "DELETE" and rest:
                return control.unlearn(rest[0])   # _route already unquoted it

        if head == "routines":
            if method == "GET":
                if rest[:1] == ["runs"]:
                    return control.routine_runs(rest[1] if rest[1:] else "")
                return control.routines()
            if method == "POST":
                body = self._body()
                if rest[1:2] == ["run"]:
                    return control.run_routine(rest[0])
                if rest[1:2] == ["enabled"]:
                    return control.set_routine_enabled(
                        rest[0], bool(body.get("enabled", True)))
                return control.save_routine(body)
            if method == "DELETE" and rest:
                return control.delete_routine(rest[0])

        if head == "content":
            if method == "GET":
                return control.content()
            if method == "POST":
                body = self._body()
                return control.queue_content(str(body.get("topic", "")),
                                             int(body.get("count", 3) or 3),
                                             str(body.get("style", "")))

        if head == "workers":
            if method == "GET":
                return control.workers()
            if method == "POST":
                body = self._body()
                return control.set_workers_paused(bool(body.get("paused", True)))

        if head == "services":
            if method == "GET":
                return control.services()
            if method == "POST":
                body = self._body()
                return control.set_services(str(body.get("action", "")))

        if head == "costs" and method == "GET":
            return control.costs()

        if head == "orders" and method == "GET":
            return control.orders()

        if head == "commands":
            if method == "GET":
                return control.commands()
            if method == "POST":
                body = self._body()
                return control.add_command(str(body.get("trigger", "")),
                                           str(body.get("response", "")),
                                           str(body.get("kind", "reply")))
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


def serve(port: int = DEFAULT_PORT, background: bool = False,
          with_services: bool = True) -> ThreadingHTTPServer:
    """Start the control API on localhost, and the background services with it.

    Binding the port is also the election for who runs the workers and the
    routine ticker. Two processes can try to be the control API - the voice
    agent starts one, and `butler-web.bat` starts another beside it - and only
    one of them can have the socket. Tying the services to the socket is what
    stops both processes running their own worker host, which is what used to
    make "pause the workers" pause a host that was not running the job you were
    looking at. See `services` for the whole argument.

    The bind comes first and the services second, deliberately: if the port is
    taken, `ThreadingHTTPServer` raises here and the services are never touched,
    which is exactly the behaviour the losing process needs.
    """
    _Handler.control = Control()
    _Handler.token = load_token()

    server = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    if background:
        thread = threading.Thread(target=server.serve_forever, daemon=True,
                                  name="jarvis-control-api")
        thread.start()

    if with_services:
        import services

        services.start(reason=f"control API bound to port {port}")
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
