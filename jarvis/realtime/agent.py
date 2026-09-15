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
DEFAULT_MODEL = "gemini-2.0-flash-live-001"
DEFAULT_VOICE = "Charon"


class JarvisRealtime:
    """Owns the LiveKit session and keeps it wired to the JARVIS core."""

    def __init__(self, app) -> None:
        self.app = app
        self.session: Any = None

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

        model = google.beta.realtime.RealtimeModel(
            model=settings.get("realtime.model", DEFAULT_MODEL),
            voice=settings.get("realtime.voice", DEFAULT_VOICE),
            api_key=api_key,
            temperature=float(settings.get("realtime.temperature", 0.8)),
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
            asyncio.create_task(self._maybe_answer_locally(session, text))

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
                session.interrupt()
                await session.say(f"Got it. {new_rule.describe()}")
                return

            rule = self.app.rules.find(text)
            if rule is not None and rule.kind == KIND_REPLY:
                self.app.rules.consume(rule)
                session.interrupt()
                await session.say(rule.response)
        except Exception:
            log.debug("local command handling failed", exc_info=True)


# --------------------------------------------------------------------------- #
# Worker entry point
# --------------------------------------------------------------------------- #

def build_server():
    """The LiveKit AgentServer that `jarvis realtime` runs."""
    from livekit.agents import AgentServer, JobContext

    from ..config import Config
    from ..core.assistant import Assistant

    setup_logging()
    server = AgentServer()

    # No agent_name on purpose. Setting one switches LiveKit to *explicit
    # dispatch*, and the agent then sits there never joining the room the
    # browser just opened - which looks exactly like "it doesn't work".
    @server.rtc_session
    async def entrypoint(ctx: JobContext) -> None:
        ctx.log_context_fields = {"room": ctx.room.name}

        app = Assistant(Config())
        # No scheduler in the call worker: missions belong to the daemon, and
        # two schedulers would fire every mission twice.
        await app.start(with_scheduler=False)
        ctx.add_shutdown_callback(_shutdown(app))

        await JarvisRealtime(app).start(ctx)

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


def preflight(config) -> str:
    """Check the LiveKit server answers before starting a worker against it.

    Without this the worker retries in the background forever while the
    terminal cheerfully says "Realtime voice is up" - so the failure is
    invisible until you wonder why nothing happens.
    """
    import urllib.error
    import urllib.request
    from urllib.parse import urlparse

    from .tokens import credentials

    url, _, _ = credentials(config)
    parsed = urlparse(url)
    scheme = "https" if parsed.scheme in ("wss", "https") else "http"
    port = f":{parsed.port}" if parsed.port else ""
    health = f"{scheme}://{parsed.hostname}{port}/"

    # Never through a proxy: this is usually a server on this machine.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(health, timeout=6):
            return ""
    except urllib.error.HTTPError:
        return ""                     # answered, which is all we needed
    except Exception as exc:
        return (f"Couldn't reach the LiveKit server at {url} ({exc}).\n"
                f"  If you're using --local, make sure it started.\n"
                f"  If you're using LiveKit Cloud, check LIVEKIT_URL.")


def run(config=None) -> int:
    """Run the agent worker in the foreground.

    Deliberately NOT livekit's `cli.run_app`: that builds a Click app over
    `sys.argv`, so it tried to parse JARVIS's own arguments and died with
    "No such command 'realtime'". It is also deprecated upstream.
    """
    if config is not None:
        export_credentials(config)

    # AgentServer.run is a coroutine - calling it without awaiting returns a
    # coroutine object and does nothing at all, silently.
    server = build_server()
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
