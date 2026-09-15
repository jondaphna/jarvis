"""The part of Jarvis that actually thinks.

The voice and the thinking want opposite things. A realtime speech model has to
answer in well under a second, which is exactly why conversation feels good now
- and it is also why it is poor at anything needing several steps held in mind
at once. Asking one model to be both is how you get an assistant that talks
beautifully and cannot work out what to do.

So the two jobs are split. Gemini Live keeps the ears and the mouth: it hears
you, answers instantly, and runs the simple things itself. When something needs
real thought - a plan, a piece of writing, an analysis, a multi-step job - it
hands the problem here, to Claude, and speaks the result.

The specialist modes are the useful half of a multi-agent design without the
expensive half. A router that classifies every single message costs a model
call before anything happens, on "what's the time" as much as on "plan my
week". Here the voice model picks a mode only when it already knows it is
delegating, so simple turns stay instant and hard ones get a focused expert.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from livekit.agents import RunContext, function_tool
from livekit.agents.llm import ToolError

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

#: Claude Opus 5. The whole point of this module is to be the smart half, so
#: it gets the capable model; the fast half is already handled by the voice.
DEFAULT_MODEL = "claude-opus-5"

#: How hard to think. Higher is better and slower - and since the complaint
#: being answered here is "not smart enough", this starts high rather than
#: cheap. Lower it in settings if you would rather have the seconds back.
DEFAULT_EFFORT = "high"

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


def _api_key(settings: Any = None) -> str | None:
    try:
        from jarvis.config import Config

        return Config().key("ANTHROPIC_API_KEY")
    except Exception:
        return None


class _Errors:
    """The SDK's exception types, or stand-ins that never match.

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


def available() -> bool:
    """Is the thinking half switched on and usable?"""
    return bool(_api_key())


class Thinker:
    """Hands hard problems to Claude and brings back something speakable."""

    def __init__(self) -> None:
        self.settings = _settings()
        self._client: Any = None

    # ------------------------------------------------------------------ #
    # The call
    # ------------------------------------------------------------------ #

    def _setting(self, key: str, fallback: Any) -> Any:
        if self.settings is None:
            return fallback
        try:
            return self.settings.get(key, fallback) or fallback
        except Exception:
            return fallback

    def client(self) -> Any:
        if self._client is not None:
            return self._client
        key = _api_key(self.settings)
        if not key:
            raise ToolError(
                "The thinking half needs a Claude key. Add one with "
                "jarvis keys set ANTHROPIC_API_KEY, or from the AI tab in "
                "settings.")
        try:
            import anthropic
        except ImportError as exc:                # pragma: no cover
            raise ToolError("The anthropic package isn't installed.") from exc
        self._client = anthropic.Anthropic(api_key=key)
        return self._client

    def ask(self, task: str, mode: str = "general", context: str = "") -> str:
        """One considered answer. Raises ToolError with something sayable."""
        errors = _sdk_errors()

        system = MODES.get(mode, MODES["general"])
        model = str(self._setting("thinking.model", DEFAULT_MODEL))
        effort = str(self._setting("thinking.effort", DEFAULT_EFFORT))

        prompt = task if not context.strip() else (
            f"{task}\n\nWhat's going on right now:\n{context.strip()}")

        request: dict[str, Any] = {
            "model": model,
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
            raise ToolError(self._explain(exc)) from exc
        except errors.connection as exc:
            raise ToolError("I couldn't reach Claude just now.") from exc

        if getattr(response, "stop_reason", "") == "refusal":
            raise ToolError("Claude declined that one.")

        text = "\n".join(block.text for block in response.content
                         if getattr(block, "type", "") == "text").strip()
        return text or "I thought about it and came back with nothing useful."

    def _send(self, request: dict[str, Any]) -> Any:
        """Send it, preferring the version that survives a refusal.

        Opus 5 can decline a request outright. With server-side fallbacks the
        API quietly re-runs it on another model inside the same call, which for
        a voice assistant is the difference between a considered answer and an
        apology. If this build's SDK or account doesn't have that beta, fall
        back to the plain call rather than failing over a nicety.
        """
        client = self.client()
        try:
            return client.beta.messages.create(
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
                **request)
        except Exception:
            return client.messages.create(**request)

    @staticmethod
    def _explain(exc: Any) -> str:
        status = getattr(exc, "status_code", 0)
        if status == 401:
            return ("That Claude key was rejected. Check it in the AI tab of "
                    "settings.")
        if status == 429:
            return "Claude is rate limiting me. Try again in a moment."
        if status >= 500:
            return "Claude is having trouble. Try again shortly."
        return f"Claude couldn't answer that: {getattr(exc, 'message', exc)}"

    # ------------------------------------------------------------------ #
    # What the voice can call
    # ------------------------------------------------------------------ #

    @property
    def tools(self) -> list:
        return [self.think]

    @function_tool()
    async def think(self, context: RunContext, task: str,
                    mode: str = "general", background: str = "") -> str:
        """Think properly about something hard. Takes a few seconds.

        You are fast but shallow. This is the opposite. Hand it anything that
        needs real reasoning rather than a quick answer:

        - working out how to do something with several steps
        - writing anything real: a script, an email, a caption, a message
        - explaining, analysing or comparing
        - diagnosing why something went wrong
        - any question where being right matters more than being quick

        Tell them you're thinking about it first, because it is not instant.
        Then say what comes back, in your own voice.

        Don't use it for small talk, for anything you already know, or for
        something you can simply do - opening a site is doing, not thinking.

        Args:
            task: The problem, in full. Include everything relevant - it
                cannot see the conversation.
            mode: "general" for questions, "plan" for working out how to do
                something, "research" for analysis, "write" for producing
                text, "code" for engineering problems.
            background: Anything that matters - what's on screen, what was
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
