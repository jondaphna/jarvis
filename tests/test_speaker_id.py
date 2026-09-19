"""Voiceprint enrolment and verification.

The embedding model is injected, so this tests the decision logic - enrol,
score, threshold, fail-open behaviour and the voice loop integration - without
needing a microphone or a 100MB model download.
"""

from __future__ import annotations

import pytest

from jarvis.core.speaker_id import (
    DEFAULT_THRESHOLD, SpeakerVerifier, cosine_similarity,
)

SAMPLE = b"\x01\x02" * 32000          # ~4 seconds of 16-bit 16kHz audio


def voice(marker: float):
    """A fake embedder: audio whose first byte is `marker` embeds near `marker`."""
    def embed(pcm: bytes):
        first = pcm[0] if pcm else 0
        return [first / 255.0, 1.0 - first / 255.0, 0.5]
    return embed


def clip(byte_value: int, seconds: float = 4.0) -> bytes:
    return bytes([byte_value, 0]) * int(16000 * seconds)


@pytest.fixture
def verifier(settings, tmp_path):
    return SpeakerVerifier(settings, profile_path=tmp_path / "voiceprint.json",
                           embedder=voice(0))


class TestMaths:
    def test_identical_vectors_score_one(self):
        assert cosine_similarity([1, 0, 0], [1, 0, 0]) == pytest.approx(1.0)

    def test_orthogonal_vectors_score_zero(self):
        assert cosine_similarity([1, 0, 0], [0, 1, 0]) == pytest.approx(0.0)

    def test_degenerate_inputs_are_safe(self):
        assert cosine_similarity([], [1, 2]) == 0.0
        assert cosine_similarity([1, 2], [1, 2, 3]) == 0.0
        assert cosine_similarity([0, 0], [0, 0]) == 0.0


class TestEnrolment:
    def test_enrolling_stores_a_profile(self, verifier):
        profile = verifier.enrol([clip(200), clip(201), clip(199)])
        assert profile.sample_count == 3
        assert verifier.enrolled

    def test_the_profile_survives_a_restart(self, verifier, settings, tmp_path):
        verifier.enrol([clip(200), clip(201)])
        reloaded = SpeakerVerifier(settings, profile_path=tmp_path / "voiceprint.json",
                                   embedder=voice(0))
        assert reloaded.enrolled
        assert reloaded.profile.sample_count == 2

    def test_short_clips_are_refused(self, verifier):
        with pytest.raises(ValueError, match="seconds"):
            verifier.enrol([clip(200, seconds=0.5)])

    def test_the_stored_file_contains_no_audio(self, verifier, tmp_path):
        verifier.enrol([clip(200), clip(201)])
        stored = (tmp_path / "voiceprint.json").read_bytes()
        assert b"\x01\x02" not in stored
        assert b"embeddings" in stored

    def test_forget_removes_everything(self, verifier, settings, tmp_path):
        verifier.enrol([clip(200)])
        settings.set("voice.only_my_voice", True)
        assert verifier.forget()
        assert not verifier.enrolled
        assert not (tmp_path / "voiceprint.json").exists()
        assert settings.get("voice.only_my_voice") is False

    def test_extra_samples_reinforce_the_profile(self, verifier):
        verifier.enrol([clip(200)])
        assert verifier.add_sample(clip(202)) == 2


class TestVerification:
    def test_the_enrolled_voice_is_accepted(self, verifier, settings):
        verifier.enrol([clip(200), clip(201)])
        settings.set("voice.only_my_voice", True)
        result = verifier.verify(clip(200))
        assert result.accepted
        assert result.score > DEFAULT_THRESHOLD

    def test_a_different_voice_is_rejected(self, verifier, settings):
        verifier.enrol([clip(250), clip(252)])
        settings.set("voice.only_my_voice", True)
        result = verifier.verify(clip(10))       # a very different voice
        assert not result.accepted
        assert "didn't match" in result.reason

    def test_a_slightly_different_take_still_passes(self, verifier, settings):
        """You don't sound identical at 9am and 9pm."""
        verifier.enrol([clip(200), clip(205)])
        settings.set("voice.only_my_voice", True)
        assert verifier.verify(clip(203)).accepted

    def test_the_threshold_is_configurable(self, verifier, settings):
        verifier.enrol([clip(250)])
        settings.set("voice.only_my_voice", True)
        settings.set("voice.speaker_threshold", 0.999)
        assert not verifier.verify(clip(120)).accepted
        settings.set("voice.speaker_threshold", 0.05)
        assert verifier.verify(clip(120)).accepted


class TestFailsOpen:
    """A half-configured feature must never leave JARVIS deaf."""

    def test_off_by_default(self, verifier):
        assert verifier.verify(clip(10)).accepted

    def test_enabled_but_not_enrolled_still_responds(self, verifier, settings):
        settings.set("voice.only_my_voice", True)
        result = verifier.verify(clip(10))
        assert result.accepted
        assert not result.enrolled

    def test_a_broken_model_does_not_silence_it(self, settings, tmp_path):
        def broken(pcm):
            raise RuntimeError("model exploded")

        verifier = SpeakerVerifier(settings, profile_path=tmp_path / "vp.json",
                                   embedder=voice(0))
        verifier.enrol([clip(200)])
        settings.set("voice.only_my_voice", True)
        verifier._embedder = broken
        result = verifier.verify(clip(200))
        assert result.accepted
        assert "failed" in result.reason


class TestVoiceLoopIntegration:
    async def test_another_persons_voice_is_ignored(self, workspace, monkeypatch):
        """The whole point: someone else talks, JARVIS stays quiet."""
        from jarvis.config import Config
        from jarvis.core import assistant as assistant_module
        from jarvis.core.assistant import Assistant
        from jarvis.core.voice_in import Utterance
        from tests.test_voice import FakeListener

        config = Config()
        config.settings.set("autonomy.workspace", str(workspace))
        app = Assistant(config)
        await app.start(with_scheduler=False)
        monkeypatch.setattr(assistant_module, "ContinuousListener", FakeListener)

        asked: list[str] = []

        async def fake_ask(text, **kwargs):
            from jarvis.core.brain import Reply
            asked.append(text)
            return Reply(text="ok")

        async def fake_say(text):
            pass

        app.ask, app.say = fake_ask, fake_say

        # Enrol "my" voice, then have two different people speak.
        app.speaker = SpeakerVerifier(
            app.settings, profile_path=workspace / "vp.json", embedder=voice(0))
        app.speaker.enrol([clip(200), clip(201)])
        app.settings.set("voice.only_my_voice", True)

        class ScriptedListener(FakeListener):
            async def utterances(self):
                yield Utterance("Jarvis, open chrome", pcm=clip(200))   # me
                yield Utterance("Jarvis, delete everything", pcm=clip(20))  # someone else

        monkeypatch.setattr(assistant_module, "ContinuousListener", ScriptedListener)
        await app.voice_loop()

        assert asked == ["open chrome"]
        app.shutdown()
