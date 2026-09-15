"""The thinking half: the request shape, and not freezing the conversation.

Two classes of bug are worth holding still here.

The request shape, because Claude Opus 5 rejects parameters that earlier models
required - `budget_tokens` and `temperature` both return a 400 - and a stale
model ID fails at call time, not at import. A wrong model string has already
cost this project a whole evening once.

And the threading, because the Anthropic SDK call blocks. Blocking the event
loop in a voice agent does not raise anything; it silently stops the audio
while the model thinks, which sounds exactly like the assistant hanging up.
"""

import inspect

import pytest
import thinker


class Recorder:
    """A stand-in client that records the request instead of sending it."""

    def __init__(self, reply: str = "the answer", stop_reason: str = "end_turn"):
        self.sent: dict = {}
        self._reply = reply
        self._stop = stop_reason
        self.messages = self._Surface(self)
        self.beta = type("Beta", (), {"messages": self._Beta(self)})()

    class _Surface:
        def __init__(self, outer): self.outer = outer
        def create(self, **kwargs):
            self.outer.sent = kwargs
            block = type("Block", (), {"type": "text", "text": self.outer._reply})()
            return type("Response", (), {"content": [block],
                                         "stop_reason": self.outer._stop})()

    class _Beta:
        def __init__(self, outer): self.outer = outer
        def create(self, **kwargs):
            # Pretend this build has no fallbacks beta, so the plain path runs.
            raise RuntimeError("beta unavailable")


@pytest.fixture
def brain(monkeypatch) -> tuple[thinker.Thinker, Recorder]:
    client = Recorder()
    t = thinker.Thinker()
    t._client = client
    monkeypatch.setattr(thinker, "_api_key", lambda settings=None: "test-key")
    return t, client


class TestTheRequestShape:
    def test_it_asks_for_a_model_that_exists(self) -> None:
        """A retired or invented model ID fails at call time, not at import -
        which is the worst possible moment to find out."""
        assert thinker.DEFAULT_MODEL == "claude-opus-5"

    def test_no_budget_tokens(self, brain) -> None:
        """Rejected with a 400 on this model; effort is the dial now."""
        t, client = brain
        t.ask("why is the sky blue")
        assert "budget_tokens" not in str(client.sent.get("thinking", {}))

    def test_no_temperature(self, brain) -> None:
        """Also a 400 on this model."""
        t, client = brain
        t.ask("why is the sky blue")
        assert "temperature" not in client.sent

    def test_thinking_and_effort_are_set(self, brain) -> None:
        t, client = brain
        t.ask("why is the sky blue")
        assert client.sent["thinking"] == {"type": "adaptive"}
        assert client.sent["output_config"]["effort"] == thinker.DEFAULT_EFFORT

    def test_context_is_passed_along(self, brain) -> None:
        """It cannot see the conversation or the screen, so anything that
        matters has to be said."""
        t, client = brain
        t.ask("what should I click", context="Spotify search results are open")
        prompt = client.sent["messages"][0]["content"]
        assert "Spotify search results" in prompt


class TestModes:
    def test_every_mode_has_its_own_brief(self) -> None:
        briefs = [thinker.MODES[m] for m in thinker.MODES]
        assert len(set(briefs)) == len(briefs), "modes must actually differ"

    def test_an_unknown_mode_falls_back_rather_than_failing(self, brain) -> None:
        t, client = brain
        t.ask("hello", mode="nonsense")
        assert client.sent["system"] == thinker.MODES["general"]

    def test_spoken_modes_say_not_to_use_markdown(self) -> None:
        """These answers are read out loud. Bullet points get spoken as
        'asterisk'."""
        for mode in ("general", "plan", "research"):
            assert "markdown" in thinker.MODES[mode].lower()


class TestWhenThingsGoWrong:
    def test_a_refusal_is_not_read_out_as_an_answer(self, monkeypatch) -> None:
        from livekit.agents.llm import ToolError

        t = thinker.Thinker()
        t._client = Recorder(reply="", stop_reason="refusal")
        monkeypatch.setattr(thinker, "_api_key", lambda settings=None: "k")
        with pytest.raises(ToolError):
            t.ask("something")

    def test_an_empty_answer_still_says_something(self, monkeypatch) -> None:
        t = thinker.Thinker()
        t._client = Recorder(reply="   ")
        monkeypatch.setattr(thinker, "_api_key", lambda settings=None: "k")
        assert t.ask("something").strip()

    def test_a_missing_key_explains_where_to_put_one(self, monkeypatch) -> None:
        from livekit.agents.llm import ToolError

        monkeypatch.setattr(thinker, "_api_key", lambda settings=None: None)
        t = thinker.Thinker()
        with pytest.raises(ToolError, match="ANTHROPIC_API_KEY"):
            t.client()

    @pytest.mark.parametrize("status,expected", [
        (401, "rejected"), (429, "rate limiting"), (503, "trouble"),
    ])
    def test_api_errors_become_sayable_sentences(self, status, expected) -> None:
        """A stack trace read aloud is useless."""
        exc = type("E", (), {"status_code": status, "message": "raw"})()
        assert expected in thinker.Thinker._explain(exc)


class TestItDoesNotFreezeTheConversation:
    async def test_the_blocking_call_runs_off_the_event_loop(self, brain) -> None:
        """The SDK call blocks. Awaiting it directly would stop the audio
        while it thinks, which sounds like the assistant hanging up."""
        t, _ = brain
        source = inspect.getsource(type(t).think._func)
        assert "asyncio.to_thread" in source

        answer = await t.think._func(t, None, "think about this")
        assert answer == "the answer"

    async def test_an_empty_task_is_refused_before_a_call_is_made(self, brain) -> None:
        from livekit.agents.llm import ToolError

        t, client = brain
        with pytest.raises(ToolError):
            await t.think._func(t, None, "   ")
        assert client.sent == {}, "it should not have called anything"


class TestTheVoiceKnowsWhenToUseIt:
    def test_the_tool_warns_that_it_is_slow(self) -> None:
        doc = " ".join((thinker.Thinker.think.__doc__ or "").split())
        assert "Takes a few seconds" in doc
        assert "Tell them you're thinking" in doc

    def test_it_says_what_not_to_use_it_for(self) -> None:
        """Without this it thinks about "what's the time" and takes five
        seconds to say half past two."""
        doc = " ".join((thinker.Thinker.think.__doc__ or "").split())
        assert "Don't use it for small talk" in doc
        assert "opening a site is doing, not thinking" in doc.lower()

    def test_it_is_governed_by_a_permission_switch(self) -> None:
        import permissions

        assert "think" in permissions.BY_KEY["reasoning"].tools
