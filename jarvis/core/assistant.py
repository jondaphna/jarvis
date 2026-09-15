"""The Assistant - one object that owns every part of JARVIS.

Construct it, `await start()`, then talk to it. The CLI and the GUI are both
thin layers over this; nothing important lives in either of them.
"""

from __future__ import annotations

import asyncio
import random
import time
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
from .people import Authority, PeopleRegistry
from .rules import KIND_REPLY, KIND_RUN, RuleBook, parse_rule_command
from .scheduler import MissionScheduler
from .speaker_id import SpeakerVerifier
from .tools import ToolRegistry
from .voice_in import ContinuousListener, VoiceListener, contains_wake_word
from .voice_out import SpeechEngine
from .. import paths
from ..config import Config
from ..plugins import PluginManager


#: Said when you call JARVIS by name without giving it anything to do. Varied on
#: purpose - hearing the identical sentence every time is what makes an
#: assistant feel like a machine.
WAKE_REPLIES = [
    "Yeah? What's up?",
    "I'm here - what do you need?",
    "Go ahead.",
    "Yes? How can I help?",
    "Listening.",
    "What can I do for you?",
    "Right here. What's up?",
]

GOODBYES = [
    "Alright, I'll be here.",
    "Catch you later.",
    "Standing by.",
    "No problem - shout if you need me.",
]

_GOODBYE_WORDS = {
    "stop", "goodbye", "bye", "that's all", "thats all", "shut down", "shutdown",
    "go to sleep", "sleep", "nevermind", "never mind", "thanks that's all",
    "stop listening", "quiet",
}


def _is_goodbye(text: str) -> bool:
    cleaned = (text or "").lower().strip().rstrip(".!?,")
    return cleaned in _GOODBYE_WORDS


class Assistant:
    """JARVIS itself."""

    def __init__(self, config: Config | None = None) -> None:
        paths.ensure_dirs()
        setup_logging()

        self.config = config or Config()
        self.settings = self.config.settings

        self.memory = Memory()
        self.broker = PermissionBroker(self.settings, self.memory)
        # Identity and custom commands are built early: the brain reads standing
        # instructions from the rulebook, and the broker consults the registry.
        self.people = PeopleRegistry(self.settings)
        self.rules = RuleBook(self.settings)
        self.broker.people = self.people
        self.computer = Computer(self.settings)
        self.tools = ToolRegistry(self.broker, path_resolver=self.computer.resolve)
        self.plugins = PluginManager(self.config, app=self)
        self.brain = Brain(self.config, self.memory, self.broker, self.tools)
        self.brain.rules = self.rules
        self.scheduler = MissionScheduler(self)
        self.speech = SpeechEngine(self.config)
        self.speaker = SpeakerVerifier(self.settings)
        self.listener = VoiceListener(self.config)

        self.conversation_id: int | None = None
        self._listener_handle: Any = None
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
                "speaker_id": self.speaker.describe(),
                "speaker_id_ready": self.speaker.available(),
            },
            "plugins": self.plugins.catalogue(),
            "plugin_errors": self.plugins.errors,
            "people": self.people.summary(),
            "commands": self.rules.summary(),
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

        speaker = self.broker.actor_name or None

        # "From now on, when I say X, say Y" - remembered, no model needed.
        if actor_is_owner := (self.broker.actor_authority is Authority.OWNER):
            new_rule = parse_rule_command(text)
            if new_rule is not None:
                new_rule.scope = speaker or new_rule.scope
                self.rules.add(new_rule)
                confirmation = f"Got it. {new_rule.describe()}"
                if speak:
                    await self.say(confirmation)
                return Reply(text=confirmation, model="rule", provider="local",
                             conversation_id=self.conversation_id)

        # A matching command answers instantly, for free, in your exact words.
        rule = self.rules.find(text, speaker)
        if rule is not None and rule.kind == KIND_REPLY:
            self.rules.consume(rule)
            bus.publish(events.REPLY, rule.response, model="rule", brain="local")
            if speak:
                await self.say(rule.response)
            return Reply(text=rule.response, model="rule", provider="local",
                         conversation_id=self.conversation_id)

        # A shorthand expands into the fuller request before the model sees it.
        if rule is not None and rule.kind == KIND_RUN:
            self.rules.consume(rule)
            extra = rule.remainder(text)
            text = f"{rule.response} {extra}".strip() if extra else rule.response

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
        """Listen continuously and answer out loud until told to stop.

        Three things make this feel like talking to someone rather than
        operating a device:

        * The microphone never closes, so the wake word can't land in a gap.
        * Saying just "Jarvis" gets an immediate friendly answer, then the next
          thing you say is the command.
        * After any reply there's a short window where you can carry on talking
          without saying the wake word again.
        """
        listener = ContinuousListener(self.config)
        word = self.settings.get("voice.wake_word", "jarvis")
        follow_up_seconds = float(self.settings.get("voice.follow_up_seconds", 12))

        try:
            await listener.start()
        except Exception as exc:
            bus.publish(events.ERROR, f"Couldn't open the microphone: {exc}")
            raise

        self._listener_handle = listener
        log.info("listening continuously (%s)",
                 f"say '{word}'" if wake_word else "no wake word needed")
        bus.publish(events.INFO,
                    f"Listening - say “{word}”" if wake_word else "Listening")

        open_until = 0.0        # while now < this, no wake word is needed

        try:
            async for utterance in listener.utterances():
                if should_stop is not None and should_stop():
                    return

                text = (utterance.text or "").strip()
                if not text:
                    continue

                # Don't let JARVIS answer its own voice coming back through the
                # speakers - that loops forever.
                if self.speech.speaking:
                    continue

                # Who is speaking? Several people can be enrolled, each with
                # their own level of authority.
                if not self._identify_speaker(utterance):
                    continue

                now = time.monotonic()
                command = text

                if wake_word and now >= open_until:
                    heard, remainder = contains_wake_word(text, word)
                    if not heard:
                        continue
                    if not remainder:
                        # Just the name: answer straight away, then listen.
                        await self.say(random.choice(WAKE_REPLIES))
                        open_until = time.monotonic() + follow_up_seconds
                        continue
                    command = remainder

                if _is_goodbye(command):
                    await self.say(random.choice(GOODBYES))
                    return

                await self.ask(command, tier=TIER_VOICE, speak=True)
                # Carry on the conversation without repeating the wake word.
                open_until = time.monotonic() + follow_up_seconds

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("voice loop error: %s", exc, exc_info=True)
            bus.publish(events.ERROR, f"Voice stopped: {exc}")
        finally:
            self._listener_handle = None
            await listener.stop()


    def _identify_speaker(self, utterance) -> bool:
        """Decide whether to answer this voice, and as whom. True = go ahead."""
        # Several people enrolled: identify, then serve at their level.
        if self.people.active and self.speaker.available():
            try:
                embedding = self.speaker.embed(utterance.pcm)
            except Exception as exc:
                log.warning("couldn't embed the voice: %s", exc)
                self.broker.reset_actor()
                return True                      # fail open, as everywhere else
            found = self.people.identify_voice(embedding)
            authority = self.people.authority_for(found)
            if authority is Authority.BLOCKED:
                log.info("ignoring %s (blocked)", found.name)
                return False
            self.broker.acting_as(authority, found.name if found.known else "")
            if found.known:
                bus.publish(events.INFO,
                            f"{found.name} ({authority.label})",
                            person=found.name, authority=authority.label,
                            score=round(found.score, 2))
            return True

        # Just one voice enrolled: the simple owner-only check.
        check = self.speaker.verify(utterance.pcm)
        if not check.accepted:
            log.info("ignored another voice (%.2f)", check.score)
            bus.publish(events.INFO,
                        f"Ignored - that wasn't your voice ({check.score:.2f})",
                        score=check.score)
            return False
        self.broker.reset_actor()
        return True

    @property
    def mic_level(self) -> float:
        """Live input level, for the orb to pulse with."""
        listener = getattr(self, "_listener_handle", None)
        return listener.level if listener is not None else 0.0

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
