"""The part of Jarvis that actually thinks - and it is free unless you say so.

The voice and the thinking want opposite things. A realtime speech model has to
answer in well under a second, which is exactly why conversation feels good now
- and it is also why it is poor at anything needing several steps held in mind
at once. Asking one model to be both is how you get an assistant that talks
beautifully and cannot work out what to do.

So the two jobs are split. Gemini Live keeps the ears and the mouth: it hears
you, answers instantly, and runs the simple things itself. When something needs
real thought - a plan, a piece of writing, an analysis - it hands the problem
here and speaks the result.

Which brain does that thinking is your choice, and the default costs nothing:

* **Your own machine** (Ollama), if you have it running. Free forever, private,
  works with the internet down. Chosen first when it is there.
* **Google's free tier**, using the same key the voice already uses. Nothing
  extra to set up, nothing to pay. This is what almost everyone gets.
* **Claude**, which is better at hard problems and costs money per question.
  Switched off until you turn it on in settings, and then it stays on.

Nothing in here ever quietly spends money. A paid model set in settings while
paid models are switched off is ignored, not honoured - the switch wins.

The specialist modes are the useful half of a multi-agent design without the
expensive half. A router that classifies every single message costs a model
call before anything happens, on "what's the time" as much as on "plan my
week". Here the voice model picks a mode only when it already knows it is
delegating, so simple turns stay instant and hard ones get a focused expert.
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path
from typing import Any, ClassVar

from livekit.agents import RunContext, function_tool
from livekit.agents.llm import ToolError

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

#: "auto" means: the best FREE brain you actually have, decided at call time.
#: It is a word rather than a model ID on purpose - a machine that gains Ollama
#: next week should start using it without anyone editing a settings file.
DEFAULT_MODEL = "auto"

#: How hard to think. Only Claude has a dial for this; the free brains decide
#: for themselves. Kept here so turning the paid half on doesn't also need a
#: second decision.
DEFAULT_EFFORT = "medium"

#: Paid models, and what they cost, in the words the settings panel shows. If
#: you add a model here it is treated as costing money until proven otherwise,
#: which is the safe direction for a mistake to go in.
PAID_MODELS: dict[str, str] = {
    "claude-opus-5": "Claude Opus 5 - the smartest, about 5 to 25 dollars per "
                     "million words",
    "claude-sonnet-5": "Claude Sonnet 5 - nearly as good, a fifth of the price",
    "claude-haiku-4-5": "Claude Haiku 4.5 - fast and cheap",
}

#: Google models worth thinking with, best first. Never trusted blindly: the
#: list Google actually serves is fetched and this is filtered against it,
#: because a model ID that has quietly retired does not error at import - it
#: fails at the exact moment you asked a question, which is the worst possible
#: moment to find out.
GEMINI_PREFERENCE: tuple[str, ...] = (
    "gemini-3-flash",
    "gemini-3.1-flash",
    "gemini-2.5-flash",
    "gemini-flash-latest",
    "gemini-2.0-flash",
)

#: Used when the model list can't be fetched. Old enough to be safe.
GEMINI_FALLBACK = "gemini-2.5-flash"

#: Local models worth thinking with, best first, filtered the same way.
OLLAMA_PREFERENCE: tuple[str, ...] = (
    "qwen2.5", "llama3.1", "llama3.2", "mistral-nemo", "phi4", "gemma2",
)

MODES: dict[str, str] = {
    "general": (
        "You are the reasoning half of JARVIS, a voice assistant. Something "
        "was asked that needs actual thought rather than a quick reply.\n\n"
        "Answer it properly, then say it in a way that can be read aloud: no "
        "markdown, no bullet points, no headings. Short sentences. If the "
        "answer is long, lead with the conclusion."
    ),
    "plan": (
        "You are the planning half of JARVIS, a voice assistant that can open "
        "websites, click and type in them, open apps, control volume and "
        "music, remember things, and run scheduled tasks.\n\n"
        "Turn the request into a short ordered list of concrete steps the "
        "assistant can carry out with those abilities. Be specific: name the "
        "site, the button, the search text. If a step needs something you "
        "were not told, say what is missing instead of inventing it.\n\n"
        "Keep it under six steps. This will be read aloud and then acted on, "
        "so write plainly, not as markdown."
    ),
    "research": (
        "You are the research half of JARVIS. Work from what you are given, "
        "reason about it, and answer the question directly.\n\n"
        "Say how confident you are and what you are unsure about. If the "
        "question needs current information you do not have, say so plainly "
        "and say what should be looked up.\n\n"
        "This is read aloud, so no markdown, no bullet points, no headings - "
        "an asterisk gets spoken as the word 'asterisk'."
    ),
    "write": (
        "You are the writing half of JARVIS. Produce the actual text asked "
        "for - a script, a caption, a message, an email.\n\n"
        "Give the piece itself, not advice about writing it, and no preamble. "
        "Match the length to the format. The result will be shown on screen "
        "or put on the clipboard, so ordinary punctuation is fine here."
    ),
    "code": (
        "You are the engineering half of JARVIS. Diagnose or write what is "
        "asked.\n\n"
        "Be concrete and short. If you are reading an error, say what caused "
        "it before saying how to fix it. Spoken aloud, so describe code rather "
        "than reciting it unless asked for the code itself."
    ),
}


def _settings() -> Any:
    try:
        from jarvis.config import Settings

        return Settings.load()
    except Exception:
        return None


def _key(name: str) -> str | None:
    try:
        from jarvis.config import Config

        return Config().key(name)
    except Exception:
        return None


def _api_key(settings: Any = None) -> str | None:
    """The Claude key, if one is stored. Named for the tests that predate the
    other backends and for doctor.py, both of which mean specifically this."""
    return _key("ANTHROPIC_API_KEY")


def paid_allowed(settings: Any = None) -> bool:
    """Has the money switch been turned on in settings?

    Off is the default and off is what an unreadable settings file means. A
    bug in configuration should cost nothing.
    """
    settings = settings if settings is not None else _settings()
    if settings is None:
        return False
    try:
        return bool(settings.get("thinking.allow_paid", False))
    except Exception:
        return False


def is_paid(model: str) -> bool:
    """Does asking this model a question cost money?

    Anything not recognised as free is treated as paid. A new model ID that
    nobody has classified yet should be blocked by the switch rather than
    quietly billed.
    """
    name = (model or "").strip().lower()
    if not name or name in {"auto", "free"}:
        return False
    # Local models and Google's free tier cost nothing. Everything else does
    # until somebody says otherwise.
    return not name.startswith(("ollama", "local", "gemini"))


# --------------------------------------------------------------------------- #
# The brains
# --------------------------------------------------------------------------- #

class Backend:
    """One brain Jarvis can hand a hard question to."""

    id = ""
    label = ""
    free = True

    def available(self) -> bool:            # pragma: no cover - overridden
        return False

    def ask(self, system: str, prompt: str, effort: str) -> str:
        raise NotImplementedError

    @staticmethod
    def _text(value: Any) -> str:
        return (value or "").strip() if isinstance(value, str) else ""


class Ollama(Backend):
    """A model running on this machine. Free, private, offline-capable."""

    id = "ollama"
    label = "Your own machine (Ollama) - free and private"
    free = True

    #: Shared across instances, because "is Ollama running" is asked on every
    #: question and on every settings refresh, and each ask is a round trip.
    #: A machine that gains or loses Ollama is noticed within a minute, which
    #: is soon enough; a voice assistant pausing to find out is not.
    _probe: ClassVar[dict[str, Any]] = {"at": 0.0, "model": None}
    _probe_seconds = 60.0

    def __init__(self) -> None:
        self._model: str | None = None

    def host(self) -> str:
        settings = _settings()
        default = "http://127.0.0.1:11434"
        if settings is None:
            return default
        try:
            return str(settings.get("ollama_host", default) or default).rstrip("/")
        except Exception:
            return default

    def models(self) -> list[str]:
        import requests

        try:
            response = requests.get(f"{self.host()}/api/tags", timeout=1.5)
            response.raise_for_status()
            return [str(m.get("name", "")) for m in response.json().get("models", [])]
        except Exception:
            return []

    def model(self) -> str | None:
        """The best installed model, or None if Ollama isn't running."""
        import time

        if self._model:
            return self._model
        cached = Ollama._probe
        if time.monotonic() - float(cached["at"]) < Ollama._probe_seconds:
            self._model = cached["model"]
            return self._model

        chosen: str | None = None
        installed = self.models()
        for wanted in OLLAMA_PREFERENCE:
            for name in installed:
                # Installed models carry a tag - "llama3.1:8b" - so match the
                # family rather than demanding an exact string.
                if name.split(":")[0] == wanted:
                    chosen = name
                    break
            if chosen:
                break
        if chosen is None and installed:
            chosen = installed[0]

        Ollama._probe = {"at": time.monotonic(), "model": chosen}
        self._model = chosen
        return chosen

    def available(self) -> bool:
        return self.model() is not None

    def ask(self, system: str, prompt: str, effort: str) -> str:
        import requests

        model = self.model()
        if not model:
            raise ToolError("Ollama isn't running on this machine.")
        try:
            response = requests.post(
                f"{self.host()}/api/chat",
                json={
                    "model": model,
                    "stream": False,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": prompt},
                    ],
                },
                timeout=180,
            )
            response.raise_for_status()
            data = response.json()
        except Exception as exc:
            raise ToolError(f"The local model didn't answer: {exc}") from exc
        return self._text((data.get("message") or {}).get("content"))


class Gemini(Backend):
    """Google's free tier, on the key the voice already uses."""

    id = "gemini"
    label = "Google Gemini - free, no extra key needed"
    free = True

    #: Discovery is one network call, and the answer is the same all session.
    _resolved: ClassVar[dict[str, str]] = {}
    _lock = threading.Lock()

    def __init__(self, model: str = "") -> None:
        self._wanted = (model or "").strip()

    def key(self) -> str | None:
        return _key("GEMINI_API_KEY") or _key("GOOGLE_API_KEY")

    def available(self) -> bool:
        return bool(self.key())

    def client(self) -> Any:
        key = self.key()
        if not key:
            raise ToolError(
                "Thinking needs a Google key - the same one the voice uses. "
                "Add it in the AI tab of settings.")
        try:
            from google import genai
        except ImportError as exc:            # pragma: no cover - env specific
            raise ToolError("The google-genai package isn't installed.") from exc
        return genai.Client(api_key=key)

    def model(self) -> str:
        """A model Google is serving right now, preferring the newest.

        Asking is worth the one call. A retired model ID is the single most
        expensive bug this project has had: it does not raise, it just gives
        you an assistant that says nothing when it matters.
        """
        if self._wanted and self._wanted != "gemini":
            return self._wanted
        with Gemini._lock:
            cached = Gemini._resolved.get("model")
            if cached:
                return cached
            served: set[str] = set()
            try:
                for entry in self.client().models.list():
                    name = str(getattr(entry, "name", "") or "")
                    actions = getattr(entry, "supported_actions", None) or []
                    if actions and "generateContent" not in actions:
                        continue
                    served.add(name.split("/")[-1])
            except Exception:
                served = set()
            chosen = next((m for m in GEMINI_PREFERENCE if m in served),
                          GEMINI_FALLBACK)
            Gemini._resolved["model"] = chosen
            return chosen

    def ask(self, system: str, prompt: str, effort: str) -> str:
        from google.genai import types as genai_types

        client = self.client()
        config = genai_types.GenerateContentConfig(
            system_instruction=system,
            max_output_tokens=4096,
        )
        try:
            response = client.models.generate_content(
                model=self.model(), contents=prompt, config=config)
        except Exception as exc:
            raise ToolError(self.explain(exc)) from exc
        return self._text(getattr(response, "text", ""))

    @staticmethod
    def explain(exc: Any) -> str:
        message = str(getattr(exc, "message", "") or exc)
        lowered = message.lower()
        if "api key" in lowered or "unauthenticated" in lowered or "401" in lowered:
            return ("That Google key was rejected. Check it in the AI tab of "
                    "settings.")
        if "quota" in lowered or "resource_exhausted" in lowered or "429" in lowered:
            return ("I've used up the free allowance for now. It resets - try "
                    "again shortly, or turn on a paid model in settings.")
        if "not found" in lowered or "404" in lowered:
            return "That thinking model isn't available on this key."
        return f"I couldn't think that through: {message[:200]}"


class Claude(Backend):
    """The paid brain. Better at hard problems, and billed per question."""

    id = "claude"
    label = "Claude - the smartest, costs money per question"
    free = False

    def __init__(self, model: str = "") -> None:
        self._model = (model or "claude-sonnet-5").strip()
        self._client: Any = None

    def available(self) -> bool:
        return bool(_api_key())

    def client(self) -> Any:
        if self._client is not None:
            return self._client
        key = _api_key()
        if not key:
            raise ToolError(
                "The paid brain needs a Claude key. Add one from the AI tab "
                "in settings, or switch thinking back to free.")
        try:
            import anthropic
        except ImportError as exc:            # pragma: no cover
            raise ToolError("The anthropic package isn't installed.") from exc
        self._client = anthropic.Anthropic(api_key=key)
        return self._client

    def ask(self, system: str, prompt: str, effort: str) -> str:
        errors = _sdk_errors()
        request: dict[str, Any] = {
            "model": self._model,
            "max_tokens": 8000,
            "system": system,
            "messages": [{"role": "user", "content": prompt}],
            # Opus 5 thinks by default; saying so keeps the intent visible.
            # budget_tokens is rejected on this model - effort is the dial.
            "thinking": {"type": "adaptive"},
            "output_config": {"effort": effort},
        }
        try:
            response = self._send(request)
        except errors.status as exc:
            raise ToolError(self.explain(exc)) from exc
        except errors.connection as exc:
            raise ToolError("I couldn't reach Claude just now.") from exc

        if getattr(response, "stop_reason", "") == "refusal":
            raise ToolError("Claude declined that one.")

        return "\n".join(block.text for block in response.content
                         if getattr(block, "type", "") == "text").strip()

    def _send(self, request: dict[str, Any]) -> Any:
        """Send it, preferring the version that survives a refusal.

        Opus 5 can decline a request outright. With server-side fallbacks the
        API quietly re-runs it on another model inside the same call, which for
        a voice assistant is the difference between a considered answer and an
        apology. If this build's SDK or account doesn't have that beta, fall
        back to the plain call rather than failing over a nicety.
        """
        client = self.client()
        errors = _sdk_errors()
        try:
            return client.beta.messages.create(
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
                **request)
        except errors.status:
            # A real answer from the API - rejected key, rate limit, outage.
            # Retrying the whole request on the plain path would double the
            # latency and the rate-limit pressure to arrive at the same
            # failure. Let it through to be explained.
            raise
        except Exception:
            # Only the beta itself being unavailable gets a second attempt.
            return client.messages.create(**request)

    @staticmethod
    def explain(exc: Any) -> str:
        status = getattr(exc, "status_code", 0)
        if status == 401:
            return ("That Claude key was rejected. Check it in the AI tab of "
                    "settings.")
        if status == 429:
            return "Claude is rate limiting me. Try again in a moment."
        if status >= 500:
            return "Claude is having trouble. Try again shortly."
        return f"Claude couldn't answer that: {getattr(exc, 'message', exc)}"


class _Errors:
    """The Anthropic SDK's exception types, or stand-ins that never match.

    Catching by type means the types have to exist. When the package is
    missing, `client()` has already raised something the user can act on, so
    these only need to be harmless.
    """

    def __init__(self) -> None:
        try:
            import anthropic

            self.status: type[BaseException] = anthropic.APIStatusError
            self.connection: type[BaseException] = anthropic.APIConnectionError
        except Exception:
            class _NeverRaisedError(Exception):
                pass

            self.status = _NeverRaisedError
            self.connection = _NeverRaisedError


def _sdk_errors() -> _Errors:
    return _Errors()


# --------------------------------------------------------------------------- #
# Choosing one
# --------------------------------------------------------------------------- #

def free_backends() -> list[Backend]:
    """Every brain that costs nothing, best first.

    Your own machine comes first when it is there: it is free, it is private,
    and nothing you say leaves the house. Google's free tier is second because
    it needs no setup at all - the key is already stored for the voice.
    """
    return [Ollama(), Gemini()]


def resolve(model: str = "", settings: Any = None) -> tuple[Backend, str]:
    """The brain to use, and a line explaining the choice if it wasn't yours.

    The second half of the pair is not decoration. Silently answering with a
    different model than the settings say is how you end up wondering why it
    got worse; the caller can log it and the doctor can print it.
    """
    settings = settings if settings is not None else _settings()
    wanted = (model or "").strip()
    if not wanted and settings is not None:
        try:
            wanted = str(settings.get("thinking.model", DEFAULT_MODEL) or DEFAULT_MODEL)
        except Exception:
            wanted = DEFAULT_MODEL
    wanted = (wanted or DEFAULT_MODEL).strip()

    note = ""
    if is_paid(wanted) and not paid_allowed(settings):
        # The switch wins over the dropdown, always. This is the line that
        # makes "free unless I say otherwise" true rather than aspirational.
        note = (f"{wanted} costs money and paid thinking is switched off, so "
                f"a free brain answered instead.")
        wanted = DEFAULT_MODEL

    if wanted in {"auto", "free", ""}:
        for backend in free_backends():
            if backend.available():
                return backend, note
        if paid_allowed(settings) and Claude().available():
            return Claude(), (note or "No free brain was reachable, so the "
                                       "paid one answered.")
        return Gemini(), note              # will explain itself when called

    lowered = wanted.lower()
    if lowered.startswith(("ollama", "local")):
        return Ollama(), note
    if lowered.startswith("gemini"):
        return Gemini(wanted), note
    return Claude(wanted), note


def available() -> bool:
    """Can Jarvis think at all right now?

    True as long as any brain is reachable. The free ones almost always are,
    which is the point of the change: thinking is no longer something you have
    to buy before it works.
    """
    if any(backend.available() for backend in free_backends()):
        return True
    return paid_allowed() and bool(_api_key())


def describe() -> str:
    """One short line for the doctor and the settings panel."""
    backend, note = resolve()
    detail = backend.label
    if isinstance(backend, Gemini) and backend.available():
        detail = f"{detail} ({backend.model()})"
    if isinstance(backend, Ollama) and backend.available():
        detail = f"{detail} ({backend.model()})"
    return f"{detail}{' - ' + note if note else ''}"


# --------------------------------------------------------------------------- #
# The tool
# --------------------------------------------------------------------------- #

class Thinker:
    """Hands hard problems to whichever brain is chosen, free by default."""

    def __init__(self) -> None:
        self.settings = _settings()
        #: Set by the tests, and by anything that wants a specific backend.
        self._client: Any = None

    def _setting(self, key: str, fallback: Any) -> Any:
        if self.settings is None:
            return fallback
        try:
            return self.settings.get(key, fallback) or fallback
        except Exception:
            return fallback

    def backend(self) -> tuple[Backend, str]:
        return resolve("", self.settings)

    def ask(self, task: str, mode: str = "general", context: str = "") -> str:
        """One considered answer. Raises ToolError with something sayable."""
        system = MODES.get(mode, MODES["general"])
        effort = str(self._setting("thinking.effort", DEFAULT_EFFORT))

        prompt = task if not context.strip() else (
            f"{task}\n\nWhat's going on right now:\n{context.strip()}")

        backend, note = self.backend()
        if self._client is not None and isinstance(backend, Claude):
            backend._client = self._client       # test seam
        if note:
            print(f"  (thinking: {note})")

        answer = backend.ask(system, prompt, effort)
        return answer or "I thought about it and came back with nothing useful."

    @property
    def tools(self) -> list:
        return [self.think]

    @function_tool()
    async def think(self, context: RunContext, task: str,
                    mode: str = "general", background: str = "") -> str:
        """Answer a hard QUESTION, or write a piece of TEXT. Takes seconds.

        Only two things belong here:

        1. A question you cannot answer well off the top of your head -
           explaining, comparing, analysing, working out why something broke.
        2. Writing something real - a script, an email, a caption, a message.

        NEVER use this to do something. If the user asked you to open, play,
        find, search, close, start, turn up, or look at anything, that is a job
        for the tool that does it - open_url, search_on_site, open_app,
        control_music, window_action. Doing is never thinking, no matter how
        many steps it takes. "Open Spotify and play something" is two tool
        calls, not a question.

        Also never use it for small talk, for anything you already know, or to
        decide which tool to use. If you are unsure which tool does something,
        pick the closest one and try it - trying is faster than thinking, and
        you will find out immediately.

        Say you are thinking about it first, in a few words, because it is not
        instant. Then say what comes back, in your own voice.

        Args:
            task: The question, or what to write. In full - it cannot see the
                conversation.
            mode: "general" for questions, "research" for analysis, "write" for
                producing text, "code" for engineering problems, "plan" only
                when they explicitly asked you to plan something rather than
                do it.
            background: Anything that matters - what is on screen, what was
                said earlier, what they already tried.
        """
        import asyncio

        cleaned = (task or "").strip()
        if not cleaned:
            raise ToolError("Think about what?")

        try:
            # Off the event loop: the SDK call blocks, and blocking here would
            # freeze the audio pipeline mid-conversation.
            return await asyncio.to_thread(
                self.ask, cleaned, (mode or "general").strip().lower(),
                background or "")
        except ToolError:
            raise
        except Exception as exc:
            raise ToolError(f"That didn't work: {exc}") from exc
