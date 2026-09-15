"""The realtime JARVIS agent.

Speech goes straight to a speech-to-speech model, so there is no
transcribe-then-think-then-speak chain to wait through. Echo cancellation and
turn detection happen where they belong - on the audio - which is what makes
interrupting work properly and stops JARVIS answering its own voice.
"""

from __future__ import annotations

import asyncio
from typing import Any

from ..core import events
from ..core.events import bus, log, setup_logging
from .bridge import build_tools, instructions_for

#: Gemini's realtime models. Kept in settings so a newer one can be swapped in
#: without touching code.
#:
#: This must be a model the Live API still serves *on the plain Gemini API* -
#: the free key from aistudio.google.com, not Vertex AI. Two different things
#: go wrong here and neither announces itself:
#:
#:   * A retired name (`gemini-2.0-flash-live-001` was the obvious choice once)
#:     is accepted locally and refused by the speech socket, so the agent joins,
#:     the browser says "Listening", and nobody ever speaks.
#:   * A Vertex-only name (`gemini-live-2.5-flash-native-audio`) reads like the
#:     newest and best model and cannot be used with an API key at all.
#:
#: So the name is checked against the plugin's own lists at startup rather than
#: trusted, and a stale one is replaced instead of honoured.
#:
#: Native audio is the pick: it hears *how* something was said, which is most of
#: the difference between a friend and a phone menu.
DEFAULT_MODEL = "gemini-2.5-flash-native-audio-preview-12-2025"
DEFAULT_VOICE = "Charon"


def _known(kind: str) -> tuple[str, ...]:
    """The model / voice names that work here.

    For models this is deliberately narrower than the plugin's type hint: that
    hint covers Vertex AI too, and JARVIS authenticates with an API key, so a
    Vertex model would be rejected outright.
    """
    import typing

    try:
        from livekit.plugins.google.realtime import realtime_api
    except Exception:                       # pragma: no cover - plugin missing
        return ()
    if kind == "LiveAPIModels":
        return tuple(sorted(
            getattr(realtime_api, "KNOWN_GEMINI_API_MODELS", ()) or ()))
    return typing.get_args(getattr(realtime_api, kind, None)) or ()


class JarvisRealtime:
    """Owns the LiveKit session and keeps it wired to the JARVIS core."""

    def __init__(self, app) -> None:
        self.app = app
        self.session: Any = None
        # asyncio only holds a *weak* reference to a running task, so a task
        # nobody keeps can be collected mid-flight and the custom command it
        # was answering just never happens. Hold them until they finish.
        self._pending: set[asyncio.Task] = set()

    # ------------------------------------------------------------------ #
    # Session
    # ------------------------------------------------------------------ #

    def build_agent(self):
        from livekit.agents import Agent
        from livekit.plugins import google

        settings = self.app.settings
        api_key = (self.app.config.key("GEMINI_API_KEY")
                   or self.app.config.key("GOOGLE_API_KEY"))
        if not api_key:
            raise RuntimeError(
                "Realtime voice needs a Gemini key - it's free at "
                "aistudio.google.com. Add it with:\n"
                "  jarvis keys set GEMINI_API_KEY")

        name = str(settings.get("realtime.model", DEFAULT_MODEL))
        voice = str(settings.get("realtime.voice", DEFAULT_VOICE))
        models, voices = _known("LiveAPIModels"), _known("Voice")
        if models and name not in models:
            # A config file written months ago still names whatever was current
            # then, and a retired model gives you a call where nobody speaks.
            # Say so, and use one that works rather than honouring a dead name.
            log.warning("realtime: %r is not a Live API model any more - "
                        "using %s instead. (Known: %s)",
                        name, DEFAULT_MODEL, ", ".join(models))
            bus.publish(events.INFO,
                        f"Switched realtime model to {DEFAULT_MODEL} "
                        f"({name} is retired).")
            name = DEFAULT_MODEL if DEFAULT_MODEL in models else models[0]
        if voices and voice not in voices:
            log.warning("realtime: voice %r is unknown, falling back to %s",
                        voice, DEFAULT_VOICE)
            voice = DEFAULT_VOICE

        from google.genai import types as genai_types

        extra: dict[str, Any] = {}
        # Without this the Live API session hits its context ceiling and simply
        # drops - mid-conversation, with no error you can see. Sliding-window
        # compression is what lets a call last longer than a few minutes.
        extra["context_window_compression"] = genai_types.ContextWindowCompressionConfig(
            sliding_window=genai_types.SlidingWindow(),
        )
        # Native-audio models can hear how you say something and answer in kind,
        # which is most of the difference between "friendly" and "robotic".
        if "native-audio" in name and settings.get("realtime.expressive", True):
            extra["enable_affective_dialog"] = True

        model = google.realtime.RealtimeModel(
            model=name,
            voice=voice,
            api_key=api_key,
            temperature=float(settings.get("realtime.temperature", 0.8)),
            **extra,
        )

        return Agent(
            instructions=instructions_for(self.app),
            llm=model,
            tools=build_tools(self.app),
        )

    async def start(self, ctx) -> None:
        """Run one call. `ctx` is the LiveKit JobContext."""
        from livekit.agents import AgentSession, room_io

        settings = self.app.settings

        session = AgentSession()
        self.session = session
        self._wire_events(session)

        await session.start(
            agent=self.build_agent(),
            room=ctx.room,
            room_options=room_io.RoomOptions(
                # Lets you show JARVIS things through the camera.
                video_input=bool(settings.get("realtime.vision", True)),
            ),
        )
        await ctx.connect()

        bus.publish(events.INFO, "Realtime session connected")
        greeting = settings.get(
            "realtime.greeting",
            "Hey - I'm here. What are we doing?")
        if greeting:
            await session.say(greeting, allow_interruptions=True)

    # ------------------------------------------------------------------ #
    # Custom commands, live
    # ------------------------------------------------------------------ #

    def _wire_events(self, session) -> None:
        """Hook the session so JARVIS's own features still apply."""

        @session.on("user_input_transcribed")
        def _on_user_speech(event: Any) -> None:
            text = getattr(event, "transcript", "") or ""
            if not getattr(event, "is_final", True) or not text.strip():
                return
            bus.publish(events.TRANSCRIPT, text)
            # A matching `reply` command answers in your exact words, straight
            # away, without the model being asked at all.
            task = asyncio.create_task(self._maybe_answer_locally(session, text))
            self._pending.add(task)
            task.add_done_callback(self._pending.discard)

        @session.on("conversation_item_added")
        def _on_item(event: Any) -> None:
            item = getattr(event, "item", None)
            role = getattr(item, "role", "")
            text = getattr(item, "text_content", "") or ""
            if role == "assistant" and text.strip():
                bus.publish(events.REPLY, text)
                if self.app.conversation_id is not None:
                    self.app.memory.add_message(
                        self.app.conversation_id, "assistant", text,
                        model="realtime")

    async def _maybe_answer_locally(self, session, text: str) -> None:
        try:
            if self.app.conversation_id is None:
                self.app.conversation_id = self.app.memory.start_conversation("voice")
            self.app.memory.add_message(self.app.conversation_id, "user", text)

            # "From now on, when I say X, say Y" - learned without a model call.
            from ..core.rules import KIND_REPLY, parse_rule_command

            new_rule = parse_rule_command(text)
            if new_rule is not None:
                self.app.rules.add(new_rule)
                await session.interrupt()
                await session.say(f"Got it. {new_rule.describe()}")
                return

            rule = self.app.rules.find(text)
            if rule is not None and rule.kind == KIND_REPLY:
                self.app.rules.consume(rule)
                await session.interrupt()
                await session.say(rule.response)
        except Exception:
            log.debug("local command handling failed", exc_info=True)


# --------------------------------------------------------------------------- #
# Worker entry point
# --------------------------------------------------------------------------- #

def build_server(config=None):
    """The LiveKit AgentServer that `jarvis realtime` runs."""
    from livekit.agents import AgentServer, JobContext, JobExecutorType

    from ..config import Config
    from ..core.assistant import Assistant

    setup_logging()

    # THREAD, not the default PROCESS. LiveKit normally runs each call in its
    # own subprocess, which is right for a server fielding many callers and
    # wrong here: the vault passphrase you typed lives in memory in *this*
    # process only. A subprocess would build a fresh, locked Config, find no
    # Gemini key, and die somewhere you never see it - so realtime voice would
    # be dead on arrival for anyone who set a passphrase. One user, one call:
    # keep it in-process, and the unlocked vault (and the assistant's state)
    # is simply there.
    kwargs: dict[str, Any] = {"job_executor_type": JobExecutorType.THREAD}
    if config is not None:
        # Hand the credentials over directly as well as through the
        # environment, so nothing depends on export order.
        from .tokens import credentials

        url, key, secret = credentials(config)
        kwargs.update(ws_url=url, api_key=key, api_secret=secret)

    server = AgentServer(**kwargs)

    # No agent_name on purpose. Setting one switches LiveKit to *explicit
    # dispatch*, and the agent then sits there never joining the room the
    # browser just opened - which looks exactly like "it doesn't work".
    @server.rtc_session
    async def entrypoint(ctx: JobContext) -> None:
        ctx.log_context_fields = {"room": ctx.room.name}

        # Reuse the Config whose vault is already unlocked, when there is one.
        app = Assistant(config or Config())
        # No scheduler in the call worker: missions belong to the daemon, and
        # two schedulers would fire every mission twice.
        await app.start(with_scheduler=False)
        ctx.add_shutdown_callback(_shutdown(app))

        try:
            await JarvisRealtime(app).start(ctx)
        except Exception as exc:
            # Otherwise this dies inside the job and the browser just sits on
            # "Listening" with nothing ever said.
            log.error("realtime session failed to start: %s", exc, exc_info=True)
            bus.publish(events.ERROR, f"The call couldn't start: {exc}")
            raise

    return server


def _shutdown(app):
    async def close() -> None:
        try:
            app.shutdown()
        except Exception:
            log.debug("shutdown hiccup", exc_info=True)
    return close


def export_credentials(config) -> None:
    """The worker reads LiveKit credentials from the environment, so put the
    vault's copies there for this process only."""
    import os

    from .tokens import credentials

    url, key, secret = credentials(config)
    os.environ["LIVEKIT_URL"] = url
    os.environ["LIVEKIT_API_KEY"] = key
    os.environ["LIVEKIT_API_SECRET"] = secret


def preflight(config) -> tuple[str, bool]:
    """Check the LiveKit server answers. Returns (problem, fatal).

    Without this the worker retries in the background forever while the
    terminal cheerfully says "Realtime voice is up" - so the failure is
    invisible until you wonder why nothing happens.

    A server on this machine either answers or isn't running, so silence there
    is fatal. LiveKit Cloud is a different matter: it sits behind a CDN that may
    refuse a bare GET, and this check can fail on a working project. Refusing to
    start on that would be worse than the problem it guards against, so a Cloud
    hiccup is a warning and the worker - which has the real credentials and
    retries properly - gets to decide.
    """
    import urllib.error
    import urllib.request
    from urllib.parse import urlparse

    from .tokens import credentials

    url, _, _ = credentials(config)
    parsed = urlparse(url)
    scheme = "https" if parsed.scheme in ("wss", "https") else "http"
    port = f":{parsed.port}" if parsed.port else ""
    host = parsed.hostname or ""
    health = f"{scheme}://{host}{port}/"
    local = host in ("localhost", "127.0.0.1", "::1", "0.0.0.0")

    # Never through a proxy: this is usually a server on this machine.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(health, timeout=6):
            return "", False
    except urllib.error.HTTPError:
        return "", False              # answered, which is all we needed
    except Exception as exc:
        if local:
            return (f"Couldn't reach the LiveKit server at {url} ({exc}).\n"
                    f"  If you're using --local, make sure it started."), True
        return (f"Couldn't get a reply from {url} ({exc}).\n"
                f"  Starting anyway - LiveKit Cloud often refuses a plain\n"
                f"  health check even when the project is fine. If the call\n"
                f"  never connects, check LIVEKIT_URL is the wss:// address\n"
                f"  from your project page."), False


def _explain_connection_failures(threshold: int = 3) -> None:
    """Turn LiveKit's retry loop into an explanation.

    When the credentials are wrong the worker logs "failed to connect to
    livekit, retrying in 2s" for ever, under a banner that already said
    "Realtime voice is up". That is technically honest and completely useless:
    the retries never say *why*, so it reads as noise rather than as the reason
    nothing works. Watch for them and say it plainly, once.
    """
    import logging

    state = {"count": 0, "explained": False}

    class Watcher(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            try:
                message = record.getMessage().lower()
            except Exception:
                return
            if "failed to connect" not in message:
                return
            state["count"] += 1
            if state["count"] < threshold or state["explained"]:
                return
            state["explained"] = True
            bus.publish(events.ERROR, "Can't reach LiveKit - see the terminal.")
            print(
                "\n  The agent can't connect to LiveKit, and will keep retrying.\n"
                "  Nothing will work until this is fixed. The usual causes:\n"
                "    * LIVEKIT_URL isn't the wss:// address from your project\n"
                "    * the API key and secret belong to a different project\n"
                "    * the key was revoked or the project was deleted\n"
                "  Check them on cloud.livekit.io, then:\n"
                "    jarvis keys set LIVEKIT_URL\n"
                "    jarvis keys set LIVEKIT_API_KEY\n"
                "    jarvis keys set LIVEKIT_API_SECRET\n",
                flush=True)

    watcher = Watcher()
    watcher.setLevel(logging.WARNING)
    logging.getLogger("livekit").addHandler(watcher)
    logging.getLogger("livekit.agents").addHandler(watcher)


def run(config=None) -> int:
    """Run the agent worker in the foreground.

    Deliberately NOT livekit's `cli.run_app`: that builds a Click app over
    `sys.argv`, so it tried to parse JARVIS's own arguments and died with
    "No such command 'realtime'". It is also deprecated upstream.
    """
    if config is not None:
        export_credentials(config)

    _explain_connection_failures()

    # AgentServer.run is a coroutine - calling it without awaiting returns a
    # coroutine object and does nothing at all, silently.
    server = build_server(config)
    try:
        asyncio.run(server.run(devmode=True))
    except KeyboardInterrupt:
        pass
    except RuntimeError as exc:
        if "failed to connect" in str(exc).lower():
            log.error("the agent could not reach LiveKit: %s", exc)
            bus.publish(events.ERROR,
                        "The agent couldn't reach the LiveKit server. Check that "
                        "it's running and that LIVEKIT_URL is right.")
            return 1
        raise
    return 0
