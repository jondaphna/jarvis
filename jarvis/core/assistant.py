"""The Assistant - one object that owns every part of JARVIS.

Construct it, `await start()`, then talk to it. The CLI and the GUI are both
thin layers over this; nothing important lives in either of them.
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable

from . import events
from .actions import register_core_tools
from .assistant_tools import register_mission_tools
from .brain import Brain, Reply, TIER_GENERAL, TIER_VOICE
from .computer import Computer
from .events import bus, log, setup_logging
from .grants import parse_app_allowances
from .memory import Memory
from .permissions import PermissionBroker
from .scheduler import MissionScheduler
from .tools import ToolRegistry
from .voice_in import VoiceListener
from .voice_out import SpeechEngine
from .. import paths
from ..config import Config
from ..plugins import PluginManager


class Assistant:
    """JARVIS itself."""

    def __init__(self, config: Config | None = None) -> None:
        paths.ensure_dirs()
        setup_logging()

        self.config = config or Config()
        self.settings = self.config.settings

        self.memory = Memory()
        self.broker = PermissionBroker(self.settings, self.memory)
        self.computer = Computer(self.settings)
        self.tools = ToolRegistry(self.broker, path_resolver=self.computer.resolve)
        self.plugins = PluginManager(self.config, app=self)
        self.brain = Brain(self.config, self.memory, self.broker, self.tools)
        self.scheduler = MissionScheduler(self)
        self.speech = SpeechEngine(self.config)
        self.listener = VoiceListener(self.config)

        self.conversation_id: int | None = None
        self._started = False

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    async def start(self, *, with_scheduler: bool = True) -> "Assistant":
        if self._started:
            return self

        self.settings.workspace.mkdir(parents=True, exist_ok=True)

        register_core_tools(self.tools, self.computer, self.memory, self.broker)
        register_mission_tools(self.tools, self)

        self.plugins.discover(extra_dirs=[paths.PLUGIN_DIR])
        self.plugins.register_tools(self.tools)

        self.scheduler.load_all()
        if with_scheduler:
            self.scheduler.start()

        self._started = True
        log.info("JARVIS ready - %d tools, %d plugins, %d missions",
                 len(self.tools.names()), len(self.plugins.available()),
                 len(self.scheduler.missions))
        bus.publish(events.INFO, "JARVIS online")
        return self

    def shutdown(self) -> None:
        self.scheduler.shutdown()
        self.speech.stop()
        self.memory.close()
        self._started = False

    async def __aenter__(self) -> "Assistant":
        return await self.start()

    async def __aexit__(self, *_exc: Any) -> None:
        self.shutdown()

    # ------------------------------------------------------------------ #
    # Readiness
    # ------------------------------------------------------------------ #

    def diagnostics(self) -> dict[str, Any]:
        """What's working and what isn't - shown by `jarvis doctor`."""
        return {
            "brain": {
                "ready": self.brain.ready(),
                "providers": self.brain.status(),
                "models": {tier: self.brain.model_for(tier)
                           for tier in ("voice", "general", "deep")},
            },
            "voice": {
                "input": self.listener.describe() if hasattr(self.listener, "describe")
                         else self.listener.transcriber.describe(),
                "input_ready": self.listener.available(),
                "input_problem": self.listener.why_unavailable(),
                "output": self.speech.describe(),
            },
            "plugins": self.plugins.catalogue(),
            "plugin_errors": self.plugins.errors,
            "missions": len(self.scheduler.missions),
            "mission_errors": self.scheduler.load_errors,
            "tools": len(self.tools.names()),
            "workspace": str(self.settings.workspace),
            "permissions": {
                "allowed_apps": self.settings.get("autonomy.allowed_apps", []),
                "pending_approvals": len(self.broker.pending()),
                "active_grants": [g.describe() for g in self.broker.active_grants()],
            },
            "home": str(paths.ROOT),
        }

    # ------------------------------------------------------------------ #
    # Talking
    # ------------------------------------------------------------------ #

    async def ask(
        self,
        text: str,
        *,
        tier: str = TIER_GENERAL,
        speak: bool = False,
        on_text: Callable[[str], Any] | None = None,
        new_conversation: bool = False,
    ) -> Reply:
        """Send JARVIS something to do. The main entry point."""
        if not self._started:
            await self.start()

        if new_conversation or self.conversation_id is None:
            self.conversation_id = self.memory.start_conversation(
                channel="voice" if tier == TIER_VOICE else "chat")

        # "You can use Photoshop" widens the allowlist. Deterministic, and only
        # ever from the user's own words - never from something JARVIS read.
        for app_name in parse_app_allowances(text):
            self.broker.allow_app(app_name)

        stream = None
        sink: Callable[[str], Any] | None = on_text
        if speak and self.speech.choose_engine() != "none":
            stream = await self.speech.stream()

            async def speak_and_forward(delta: str, _stream=stream) -> None:
                # Feed the speech queue as text arrives, so JARVIS starts talking
                # while Claude is still writing.
                if on_text is not None:
                    on_text(delta)
                await _stream.feed(delta)

            sink = speak_and_forward

        try:
            reply = await self.brain.chat(
                text,
                tier=tier,
                conversation_id=self.conversation_id,
                on_text=sink,
                actor="user",
            )
        finally:
            if stream is not None:
                await stream.close()

        return reply

    async def say(self, text: str) -> None:
        """Speak something out loud without involving the model."""
        await self.speech.say(text)

    # ------------------------------------------------------------------ #
    # Voice
    # ------------------------------------------------------------------ #

    async def voice_turn(self, listen_seconds: float = 30.0,
                         on_level: Callable[[float], Any] | None = None) -> Reply | None:
        """Listen once, answer out loud. Returns None if nothing was said."""
        utterance = await self.listener.listen(max_seconds=listen_seconds,
                                               on_level=on_level)
        if not utterance:
            return None
        return await self.ask(utterance.text, tier=TIER_VOICE, speak=True)

    async def voice_loop(self, wake_word: bool = True,
                         should_stop: Callable[[], bool] | None = None) -> None:
        """Keep listening and answering until told to stop."""
        if not self.listener.available():
            raise RuntimeError(self.listener.why_unavailable())

        self.listener.transcriber.warm_up()
        word = self.settings.get("voice.wake_word", "jarvis")
        log.info("voice loop started (%s)",
                 f"say '{word}'" if wake_word else "always listening")

        while not (should_stop and should_stop()):
            try:
                if wake_word:
                    utterance = await self.listener.listen_for_wake_word(word)
                    if utterance is None:
                        continue
                    if not utterance.text.strip():
                        await self.say("Yes?")
                        utterance = await self.listener.listen()
                        if not utterance:
                            continue
                else:
                    utterance = await self.listener.listen()
                    if not utterance:
                        continue

                text = utterance.text.strip()
                if text.lower().rstrip(".!?") in ("stop", "goodbye", "that's all",
                                                  "shut down", "go to sleep"):
                    await self.say("Standing by.")
                    return

                await self.ask(text, tier=TIER_VOICE, speak=True)

            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("voice loop error: %s", exc, exc_info=True)
                bus.publish(events.ERROR, f"Voice error: {exc}")
                await asyncio.sleep(1.0)

    # ------------------------------------------------------------------ #
    # Missions
    # ------------------------------------------------------------------ #

    async def run_mission(self, mission_id: str, trigger: str = "manual",
                          unattended: bool = True):
        if not self._started:
            await self.start()
        return await self.scheduler.run(mission_id, trigger=trigger,
                                        unattended=unattended)

    # ------------------------------------------------------------------ #
    # Approvals
    # ------------------------------------------------------------------ #

    def pending_approvals(self) -> list[dict[str, Any]]:
        return self.broker.pending()

    def approve(self, approval_id: int, always: bool = False) -> bool:
        return self.broker.approve(approval_id, grant_future=always)

    def deny(self, approval_id: int, note: str = "") -> bool:
        return self.broker.deny(approval_id, note)

    # ------------------------------------------------------------------ #
    # Attended mode - ask the user live instead of queueing
    # ------------------------------------------------------------------ #

    def set_approval_handler(self, handler) -> None:
        """Register an interactive approver (the CLI prompt or a UI dialog)."""
        self.broker.approval_callback = handler

    def clear_approval_handler(self) -> None:
        self.broker.approval_callback = None
