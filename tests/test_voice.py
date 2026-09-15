"""Voice: wake-word matching, speech segmentation, and the conversation loop.

None of this needs a microphone. The audio source and the transcriber are both
injectable, so the logic that decides "was that the wake word, and what do I do
about it" is tested directly.
"""

from __future__ import annotations

import asyncio
import math
import struct

import pytest

from jarvis.core.voice_in import (
    FRAME_SAMPLES, ContinuousListener, Utterance, contains_wake_word,
)


# --------------------------------------------------------------------------- #
# Synthetic audio
# --------------------------------------------------------------------------- #

def _frame(amplitude: float) -> bytes:
    return struct.pack(f"<{FRAME_SAMPLES}h",
                       *[int(amplitude * 32767 * math.sin(i * 0.1))
                         for i in range(FRAME_SAMPLES)])


QUIET = _frame(0.001)
LOUD = _frame(0.35)


class TestWakeWord:
    @pytest.mark.parametrize("said,expected", [
        ("Jarvis, open chrome", "open chrome"),
        ("jarvis open chrome", "open chrome"),
        ("Hey Jarvis, what time is it", "what time is it"),
        ("JARVIS! play some music", "play some music"),
    ])
    def test_command_after_the_name(self, said, expected):
        heard, remainder = contains_wake_word(said, "jarvis")
        assert heard
        assert remainder == expected

    @pytest.mark.parametrize("said", ["jarvis", "Jarvis.", "  Jarvis?  "])
    def test_name_on_its_own(self, said):
        heard, remainder = contains_wake_word(said, "jarvis")
        assert heard
        assert remainder == ""

    @pytest.mark.parametrize("said", [
        "Service, open the browser",     # what Whisper often hears
        "Travis, play music",
        "Jarvus open chrome",
    ])
    def test_mishearings_still_wake_it(self, said):
        """A missed wake word feels broken; a false one costs one 'yes?'."""
        heard, _ = contains_wake_word(said, "jarvis")
        assert heard

    @pytest.mark.parametrize("said", [
        "just talking normally",
        "I was driving my car",
        "",
        "   ",
    ])
    def test_ordinary_speech_is_ignored(self, said):
        heard, _ = contains_wake_word(said, "jarvis")
        assert not heard

    def test_a_custom_wake_word_works(self):
        heard, remainder = contains_wake_word("computer, lights on", "computer")
        assert heard and remainder == "lights on"


class TestSegmentation:
    """The microphone stays open; silence is what ends an utterance."""

    async def _collect(self, frames, expected, settings=None):
        from jarvis.config import Config

        config = Config()
        if settings:
            for key, value in settings.items():
                config.settings.set(key, value)

        listener = ContinuousListener(config, source=iter(frames))
        listener.transcriber.warm_up = lambda: None

        sizes: list[int] = []

        async def fake_transcribe(pcm, engine=None):
            sizes.append(len(pcm))
            return Utterance(f"phrase {len(sizes)}", engine="fake")

        listener.transcriber.transcribe = fake_transcribe

        await listener.start()
        heard: list[str] = []

        async def pump():
            async for utterance in listener.utterances():
                heard.append(utterance.text)
                if len(heard) >= expected:
                    return

        try:
            await asyncio.wait_for(pump(), timeout=10)
        except asyncio.TimeoutError:
            pass
        await listener.stop()
        return heard, sizes

    async def test_silence_splits_two_phrases(self):
        frames = ([QUIET] * 10 + [LOUD] * 40 + [QUIET] * 30
                  + [LOUD] * 40 + [QUIET] * 40)
        heard, _ = await self._collect(frames, expected=2)
        assert len(heard) == 2

    async def test_a_single_phrase_is_one_utterance(self):
        frames = [QUIET] * 10 + [LOUD] * 50 + [QUIET] * 40
        heard, _ = await self._collect(frames, expected=1)
        assert len(heard) == 1

    async def test_silence_alone_produces_nothing(self):
        heard, _ = await self._collect([QUIET] * 60, expected=1)
        assert heard == []

    async def test_a_brief_blip_is_not_a_word(self):
        """A door closing shouldn't wake anything up."""
        frames = [QUIET] * 10 + [LOUD] * 3 + [QUIET] * 40
        heard, _ = await self._collect(frames, expected=1)
        assert heard == []

    async def test_the_start_of_speech_is_not_clipped(self):
        """Pre-roll: without it the 'J' of 'Jarvis' goes missing."""
        frames = [QUIET] * 10 + [LOUD] * 40 + [QUIET] * 40
        _, sizes = await self._collect(frames, expected=1)
        speech_bytes = 40 * FRAME_SAMPLES * 2
        assert sizes[0] > speech_bytes      # captured more than just the loud part


# --------------------------------------------------------------------------- #
# The conversation loop
# --------------------------------------------------------------------------- #

class FakeListener:
    """Replays a scripted list of things the user said."""

    def __init__(self, config, source=None):
        self.said = list(getattr(config, "_script", []))
        self.level = 0.0
        self.started = False
        self.stopped = False

    async def start(self):
        self.started = True

    async def stop(self):
        self.stopped = True

    async def utterances(self):
        for text in self.said:
            yield Utterance(text, engine="fake")
            await asyncio.sleep(0)


class TestConversationLoop:
    @pytest.fixture
    async def app(self, workspace, monkeypatch):
        from jarvis.config import Config
        from jarvis.core import assistant as assistant_module
        from jarvis.core.assistant import Assistant

        config = Config()
        config.settings.set("autonomy.workspace", str(workspace))
        app = Assistant(config)
        await app.start(with_scheduler=False)

        monkeypatch.setattr(assistant_module, "ContinuousListener", FakeListener)

        app.spoken: list[str] = []
        app.asked: list[str] = []

        async def fake_say(text):
            app.spoken.append(text)

        async def fake_ask(text, **kwargs):
            app.asked.append(text)
            from jarvis.core.brain import Reply
            return Reply(text="done")

        app.say = fake_say
        app.ask = fake_ask
        yield app
        app.shutdown()

    def _script(self, app, *lines):
        app.config._script = list(lines)

    async def test_name_alone_gets_a_friendly_answer(self, app):
        from jarvis.core.assistant import WAKE_REPLIES

        self._script(app, "Jarvis")
        await app.voice_loop()
        assert app.spoken
        assert app.spoken[0] in WAKE_REPLIES
        assert app.asked == []          # nothing to do yet, just a greeting

    async def test_name_plus_command_runs_it(self, app):
        self._script(app, "Jarvis, open chrome")
        await app.voice_loop()
        assert app.asked == ["open chrome"]

    async def test_speech_without_the_wake_word_is_ignored(self, app):
        self._script(app, "so anyway I told him it was fine")
        await app.voice_loop()
        assert app.asked == []
        assert app.spoken == []

    async def test_you_can_keep_talking_without_repeating_the_name(self, app):
        """The follow-up window - this is what makes it a conversation."""
        self._script(app, "Jarvis, open chrome", "now search for flights")
        await app.voice_loop()
        assert app.asked == ["open chrome", "now search for flights"]

    async def test_the_command_after_a_greeting_needs_no_wake_word(self, app):
        self._script(app, "Jarvis", "what's the time")
        await app.voice_loop()
        assert app.asked == ["what's the time"]

    async def test_goodbye_stops_the_loop(self, app):
        from jarvis.core.assistant import GOODBYES

        self._script(app, "Jarvis", "stop", "open chrome")
        await app.voice_loop()
        assert app.spoken[-1] in GOODBYES
        assert "open chrome" not in app.asked    # loop ended before this

    async def test_it_does_not_answer_its_own_voice(self, app):
        """Speakers feed the microphone; without this it talks to itself."""
        app.speech._speaking = True
        self._script(app, "Jarvis, open chrome")
        await app.voice_loop()
        assert app.asked == []

    async def test_stop_signal_is_honoured(self, app):
        self._script(app, "Jarvis, open chrome", "and another thing")
        await app.voice_loop(should_stop=lambda: True)
        assert app.asked == []

    async def test_the_microphone_is_released_at_the_end(self, app):
        self._script(app, "Jarvis")
        await app.voice_loop()
        assert app._listener_handle is None
