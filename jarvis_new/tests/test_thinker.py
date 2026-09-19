"""The thinking half: free by default, the right request shape, no freezing.

Three classes of bug are worth holding still here.

**Cost**, because "free unless I say otherwise" is a promise that has to be
enforced somewhere rather than merely written in a settings file. A paid model
left in settings, an unrecognised model ID, a local model that stumbles - none
of those may quietly turn into a bill.

**The request shape**, because Claude Opus 5 rejects parameters that earlier
models required - `budget_tokens` and `temperature` both return a 400 - and a
stale model ID fails at call time, not at import. A wrong model string has
already cost this project a whole evening once.

And **the threading**, because the SDK call blocks. Blocking the event loop in
a voice agent does not raise anything; it silently stops the audio while the
model thinks, which sounds exactly like the assistant hanging up.
"""

import inspect

import pytest

import thinker


class Recorder:
    """A stand-in Anthropic client that records the request instead of sending it."""

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


class FakeSettings:
    """Just enough of Settings to steer a choice, without touching the disk."""

    def __init__(self, **values):
        self._values = values

    def get(self, dotted, default=None):
        return self._values.get(dotted, default)


def claude_with(recorder: Recorder, model: str = "claude-opus-5") -> thinker.Claude:
    backend = thinker.Claude(model)
    backend._client = recorder
    return backend


@pytest.fixture
def brain(monkeypatch) -> tuple[thinker.Thinker, Recorder]:
    """A Thinker pinned to Claude, so the request shape can be inspected.

    Pinned deliberately: the real default is free, and a test that reached it
    would make a network call to Google every run.
    """
    client = Recorder()
    t = thinker.Thinker()
    monkeypatch.setattr(thinker, "_api_key", lambda settings=None: "test-key")
    monkeypatch.setattr(t, "backend", lambda: (claude_with(client), ""))
    return t, client


class TestFreeByDefault:
    """Nothing costs money until it is switched on, and the switch wins."""

    def test_the_default_brain_is_free(self) -> None:
        assert thinker.DEFAULT_MODEL == "auto"
        assert not thinker.is_paid(thinker.DEFAULT_MODEL)

    def test_paid_is_off_when_nothing_says_otherwise(self) -> None:
        assert thinker.paid_allowed(FakeSettings()) is False

    def test_an_unreadable_settings_file_does_not_unlock_spending(self) -> None:
        class Broken:
            def get(self, *_args, **_kwargs):
                raise RuntimeError("corrupt")

        assert thinker.paid_allowed(Broken()) is False

    def test_an_unknown_model_is_assumed_to_cost_money(self) -> None:
        """The safe direction for a mistake to go in: a model nobody has
        classified is blocked by the switch rather than quietly billed."""
        assert thinker.is_paid("some-new-frontier-model") is True

    def test_the_free_ones_are_known_to_be_free(self) -> None:
        for model in ("auto", "gemini-2.5-flash", "ollama:llama3.1", "local"):
            assert thinker.is_paid(model) is False, model

    def test_every_claude_model_is_marked_paid(self) -> None:
        for model in thinker.PAID_MODELS:
            assert thinker.is_paid(model) is True, model

    def test_a_paid_setting_is_ignored_while_the_switch_is_off(self, monkeypatch) -> None:
        """The regression that matters. A settings file carrying the old paid
        default must not start spending on the strength of that alone."""
        monkeypatch.setattr(thinker.Gemini, "available", lambda self: True)
        monkeypatch.setattr(thinker.Ollama, "available", lambda self: False)

        backend, note = thinker.resolve(
            "claude-opus-5",
            FakeSettings(**{"thinking.allow_paid": False}))

        assert backend.free is True
        assert not isinstance(backend, thinker.Claude)
        assert "costs money" in note, "the swap has to be explainable"

    def test_the_switch_being_on_is_honoured(self, monkeypatch) -> None:
        backend, note = thinker.resolve(
            "claude-opus-5",
            FakeSettings(**{"thinking.allow_paid": True}))
        assert isinstance(backend, thinker.Claude)
        assert note == ""

    def test_your_own_machine_is_preferred_over_the_cloud(self) -> None:
        """Free either way, but one of them never sends your words anywhere."""
        order = [b.id for b in thinker.free_backends()]
        assert order.index("ollama") < order.index("gemini")

    def test_auto_picks_the_first_free_brain_that_is_there(self, monkeypatch) -> None:
        monkeypatch.setattr(thinker.Ollama, "available", lambda self: False)
        monkeypatch.setattr(thinker.Gemini, "available", lambda self: True)
        backend, _ = thinker.resolve("auto", FakeSettings())
        assert isinstance(backend, thinker.Gemini)

        monkeypatch.setattr(thinker.Ollama, "available", lambda self: True)
        backend, _ = thinker.resolve("auto", FakeSettings())
        assert isinstance(backend, thinker.Ollama)

    def test_naming_a_free_model_directly_works(self) -> None:
        backend, note = thinker.resolve("gemini-2.5-flash", FakeSettings())
        assert isinstance(backend, thinker.Gemini)
        assert note == ""

    def test_it_can_think_without_anybody_buying_anything(self, monkeypatch) -> None:
        """The whole point: no Claude key, no credit, still able to think."""
        monkeypatch.setattr(thinker, "_api_key", lambda settings=None: None)
        monkeypatch.setattr(thinker.Gemini, "available", lambda self: True)
        assert thinker.available() is True


class TestTheGeminiHalf:
    def test_a_retired_model_id_is_never_assumed(self) -> None:
        """A model name that has quietly retired does not raise at import - it
        fails at the moment you asked a question. So the list is checked."""
        source = inspect.getsource(thinker.Gemini.model)
        assert "models.list" in source

    def test_the_fallback_is_a_model_that_has_existed_for_years(self) -> None:
        assert thinker.GEMINI_FALLBACK in thinker.GEMINI_PREFERENCE

    def test_it_picks_the_newest_model_google_actually_serves(self, monkeypatch) -> None:
        served = ["models/gemini-2.5-flash", "models/gemini-1.0-pro"]
        entries = [type("M", (), {"name": n, "supported_actions": ["generateContent"]})()
                   for n in served]
        monkeypatch.setattr(thinker.Gemini, "client",
                            lambda self: type("C", (), {"models": type(
                                "MM", (), {"list": lambda self: entries})()})())
        thinker.Gemini._resolved.clear()
        assert thinker.Gemini().model() == "gemini-2.5-flash"
        thinker.Gemini._resolved.clear()

    def test_a_dead_network_still_yields_a_usable_model(self, monkeypatch) -> None:
        def explode(self):
            raise RuntimeError("no network")

        monkeypatch.setattr(thinker.Gemini, "client", explode)
        thinker.Gemini._resolved.clear()
        assert thinker.Gemini().model() == thinker.GEMINI_FALLBACK
        thinker.Gemini._resolved.clear()

    @pytest.mark.parametrize("message,expected", [
        ("API key not valid", "rejected"),
        ("429 RESOURCE_EXHAUSTED: quota", "free allowance"),
        ("404 model not found", "isn't available"),
    ])
    def test_its_errors_become_sayable_sentences(self, message, expected) -> None:
        """Read aloud. A stack trace is useless and a quota message should say
        what to do about it."""
        assert expected in thinker.Gemini.explain(RuntimeError(message))


class TestTheClaudeRequestShape:
    def test_it_asks_for_models_that_exist(self) -> None:
        """A retired or invented model ID fails at call time, not at import -
        which is the worst possible moment to find out."""
        assert set(thinker.PAID_MODELS) == {
            "claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5"}

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
        assert client.sent["output_config"]["effort"]

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

        monkeypatch.setattr(thinker, "_api_key", lambda settings=None: "k")
        backend = claude_with(Recorder(reply="", stop_reason="refusal"))
        with pytest.raises(ToolError):
            backend.ask("system", "something", "medium")

    def test_an_empty_answer_still_says_something(self, monkeypatch) -> None:
        t = thinker.Thinker()
        monkeypatch.setattr(thinker, "_api_key", lambda settings=None: "k")
        monkeypatch.setattr(t, "backend",
                            lambda: (claude_with(Recorder(reply="   ")), ""))
        assert t.ask("something").strip()

    def test_a_missing_claude_key_explains_where_to_put_one(self, monkeypatch) -> None:
        from livekit.agents.llm import ToolError

        monkeypatch.setattr(thinker, "_api_key", lambda settings=None: None)
        with pytest.raises(ToolError, match="settings"):
            thinker.Claude().client()

    @pytest.mark.parametrize("status,expected", [
        (401, "rejected"), (429, "rate limiting"), (503, "trouble"),
    ])
    def test_api_errors_become_sayable_sentences(self, status, expected) -> None:
        """A stack trace read aloud is useless."""
        exc = type("E", (), {"status_code": status, "message": "raw"})()
        assert expected in thinker.Claude.explain(exc)


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


class TestItNeverHijacksADoableTask:
    """The regression this class exists to stop.

    The tool used to advertise itself for "working out how to do something with
    several steps". "Open my Spotify and play a song" reads exactly like that,
    so the voice reached for the slow reasoning path - which also failed
    outright when no Claude key was stored - instead of just calling open_url.
    Simple requests got worse the moment thinking was added.
    """

    def doc(self) -> str:
        return " ".join((thinker.Thinker.think.__doc__ or "").split())

    def test_it_refuses_to_be_used_for_doing(self) -> None:
        doc = self.doc()
        assert "NEVER use this to do something" in doc
        assert "Doing is never thinking" in doc

    def test_it_names_the_verbs_that_are_not_its_job(self) -> None:
        """Naming them beats describing them: the model matches on the word."""
        doc = self.doc().lower()
        for verb in ("open", "play", "find", "search", "close", "start"):
            assert verb in doc

    def test_the_multi_step_bait_is_gone(self) -> None:
        assert "several steps" not in self.doc()

    def test_it_is_not_for_choosing_a_tool(self) -> None:
        """Thinking about which tool to use costs seconds to reach a guess the
        model could have tested in one call."""
        assert "never use it for" in self.doc().lower()
        assert "trying is faster than thinking" in self.doc()

    def test_it_still_warns_that_it_is_slow(self) -> None:
        assert "Takes seconds" in self.doc()

    def test_it_names_tools_that_actually_exist(self) -> None:
        """A docstring pointing at a renamed tool sends the model looking for
        something it cannot call. `fetch_page` survived one rename as dead
        code and was still being advertised in the prompt."""
        import permissions

        for name in ("open_url", "search_on_site", "open_app", "control_music",
                     "window_action"):
            assert name in self.doc()
            assert name in permissions.GOVERNED, f"{name} is not a real tool"

    def test_it_is_governed_by_a_permission_switch(self) -> None:
        import permissions

        assert "think" in permissions.BY_KEY["reasoning"].tools


class TestItDoesNotMakeYouWait:
    """A probe on every question is a pause on every question."""

    def test_the_local_probe_is_cached(self, monkeypatch) -> None:
        calls = {"n": 0}

        def counted(self):
            calls["n"] += 1
            return ["llama3.1:8b"]

        monkeypatch.setattr(thinker.Ollama, "models", counted)
        thinker.Ollama._probe = {"at": 0.0, "model": None}

        assert thinker.Ollama().model() == "llama3.1:8b"
        for _ in range(5):
            thinker.Ollama().model()          # fresh objects, as resolve() makes
        assert calls["n"] == 1, "one round trip, not one per question"
        thinker.Ollama._probe = {"at": 0.0, "model": None}

    def test_a_machine_without_ollama_is_remembered_as_such(self, monkeypatch) -> None:
        """The expensive case: nothing listening, asked on every turn."""
        calls = {"n": 0}

        def counted(self):
            calls["n"] += 1
            return []

        monkeypatch.setattr(thinker.Ollama, "models", counted)
        thinker.Ollama._probe = {"at": 0.0, "model": None}

        assert thinker.Ollama().available() is False
        assert thinker.Ollama().available() is False
        assert calls["n"] == 1
        thinker.Ollama._probe = {"at": 0.0, "model": None}

    def test_the_preferred_model_wins_over_whatever_is_first(self, monkeypatch) -> None:
        monkeypatch.setattr(thinker.Ollama, "models",
                            lambda self: ["something-odd:latest", "llama3.1:8b"])
        thinker.Ollama._probe = {"at": 0.0, "model": None}
        assert thinker.Ollama().model() == "llama3.1:8b"
        thinker.Ollama._probe = {"at": 0.0, "model": None}
