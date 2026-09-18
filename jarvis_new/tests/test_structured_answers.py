"""Asking a background model for JSON, and saying how long it may be.

The scriptwriter asks for a JSON array and then parses whatever comes back.
Saying so in the prompt works most of the time; saying so in the *request*
works more of the time, and the difference is a wasted model call, thirty
seconds of somebody's evening, and a slice of a free daily quota.

The length is the other half, and the more expensive one. The conversation's
answer budget is four thousand tokens because a spoken answer is short. A
batch of five Reel scripts with beats, captions and hashtags is not short: it
ran to about six thousand, so the fifth script was cut off two thirds of the
way through and the whole array failed to parse. The batch that hits the limit
is always the long one, and the long one is the batch worth having.

None of this is on the voice path. The voice budget is unchanged, and a
backend with no structured mode gets the plain question it always got.
"""

import pytest

import thinker
from content import scriptwriter
from content.scriptwriter import MAX_PER_BATCH, Scriptwriter, _ask, _room


class Plain:
    """What every test fake in this codebase looks like: one `ask` method."""

    def __init__(self, answer="[]"):
        self.answer = answer
        self.calls = []

    def ask(self, system, prompt, effort):
        self.calls.append((system, prompt, effort))
        return self.answer


class Structured(Plain):

    def __init__(self, answer="[]"):
        super().__init__(answer)
        self.json_calls = []

    def ask_json(self, system, prompt, effort="medium", max_tokens=0):
        self.json_calls.append((system, prompt, effort, max_tokens))
        return self.answer


class TestAskingTheBetterWay:

    def test_a_backend_that_can_be_told_the_shape_is_told(self):
        backend = Structured()
        _ask(backend, "system", "prompt", 5000)
        assert backend.json_calls and not backend.calls
        assert backend.json_calls[0][3] == 5000

    def test_a_backend_that_cannot_gets_the_plain_question(self):
        backend = Plain()
        _ask(backend, "system", "prompt", 5000)
        assert backend.calls == [("system", "prompt", "medium")]

    def test_something_else_called_ask_json_is_not_mistaken_for_it(self):
        """Falls back rather than raising: the fakes injected by tests, and
        anything a later build passes in, are plain objects."""
        class Odd(Plain):
            def ask_json(self):
                raise AssertionError("should never be called")

        backend = Odd()
        _ask(backend, "system", "prompt", 5000)
        assert backend.calls == [("system", "prompt", "medium")]

    def test_the_scriptwriter_uses_it(self):
        backend = Structured(_batch(2))
        Scriptwriter(backend=backend).write("a topic", count=2)
        assert len(backend.json_calls) == 1
        assert not backend.calls

    def test_the_old_kind_of_backend_still_works(self):
        backend = Plain(_batch(2))
        scripts = Scriptwriter(backend=backend).write("a topic", count=2)
        assert len(scripts) == 2


class TestHowMuchRoom:

    def test_more_scripts_get_more_room(self):
        assert _room(5) > _room(3) > _room(1)

    def test_a_full_batch_gets_more_than_the_spoken_budget(self):
        """The exact failure this exists for: five scripts did not fit in the
        four thousand tokens a spoken answer gets."""
        assert _room(MAX_PER_BATCH) > thinker.SPOKEN_MAX_TOKENS

    def test_the_spoken_budget_is_untouched(self):
        assert thinker.SPOKEN_MAX_TOKENS == 4096

    def test_nonsense_counts_do_not_produce_a_nonsense_budget(self):
        assert _room(0) > 0
        assert _room(-3) > 0

    def test_the_scriptwriter_asks_for_room_that_fits_the_batch(self):
        backend = Structured(_batch(4))
        Scriptwriter(backend=backend).write("a topic", count=4)
        assert backend.json_calls[0][3] == _room(4)

    def test_the_retry_only_asks_for_room_for_what_is_missing(self):
        """It asks for one more script, so it should not ask for the length
        of a whole batch as well."""
        class Twice(Structured):
            def ask_json(self, system, prompt, effort="medium", max_tokens=0):
                self.json_calls.append((system, prompt, effort, max_tokens))
                return _batch(2) if len(self.json_calls) == 1 else _batch(1)

        backend = Twice()
        Scriptwriter(backend=backend).write("a topic", count=3)
        assert len(backend.json_calls) == 2
        assert backend.json_calls[1][3] < backend.json_calls[0][3]


class TestTheGeminiRequest:

    def config(self, monkeypatch, method, *args, **kwargs):
        """Run one call against a fake client and hand back the config."""
        seen = {}

        class FakeModels:
            def generate_content(self, model, contents, config):
                seen["model"] = model
                seen["config"] = config
                return type("R", (), {"text": "[]"})()

        class FakeClient:
            models = FakeModels()

        brain = thinker.Gemini(model="gemini-test")
        monkeypatch.setattr(brain, "client", lambda: FakeClient())
        getattr(brain, method)(*args, **kwargs)
        return seen["config"]

    def test_a_structured_ask_pins_the_response_type(self, monkeypatch):
        config = self.config(monkeypatch, "ask_json", "system", "prompt")
        assert config.response_mime_type == "application/json"

    def test_a_spoken_ask_does_not(self, monkeypatch):
        config = self.config(monkeypatch, "ask", "system", "prompt", "medium")
        assert not getattr(config, "response_mime_type", None)

    def test_the_spoken_budget_is_what_it_always_was(self, monkeypatch):
        config = self.config(monkeypatch, "ask", "system", "prompt", "medium")
        assert config.max_output_tokens == thinker.SPOKEN_MAX_TOKENS

    def test_a_structured_ask_gets_the_room_it_asked_for(self, monkeypatch):
        config = self.config(monkeypatch, "ask_json", "system", "prompt",
                             "medium", 7777)
        assert config.max_output_tokens == 7777

    def test_asking_for_nothing_in_particular_still_gets_more(self, monkeypatch):
        config = self.config(monkeypatch, "ask_json", "system", "prompt")
        assert config.max_output_tokens == thinker.JSON_MAX_TOKENS
        assert thinker.JSON_MAX_TOKENS > thinker.SPOKEN_MAX_TOKENS

    def test_a_model_that_refuses_the_mime_type_is_asked_plainly(self,
                                                                monkeypatch):
        """A model one release behind must not fail every script job."""
        seen = []

        class FakeModels:
            def generate_content(self, model, contents, config):
                seen.append(config)
                if getattr(config, "response_mime_type", None):
                    raise RuntimeError("response_mime_type is not supported")
                return type("R", (), {"text": "[]"})()

        brain = thinker.Gemini(model="gemini-test")
        monkeypatch.setattr(brain, "client", lambda: type(
            "C", (), {"models": FakeModels()})())
        assert brain.ask_json("system", "prompt") == "[]"
        assert len(seen) == 2
        assert not getattr(seen[1], "response_mime_type", None)

    def test_a_real_failure_is_not_swallowed_by_the_fallback(self, monkeypatch):
        class FakeModels:
            def generate_content(self, model, contents, config):
                raise RuntimeError("quota exhausted")

        brain = thinker.Gemini(model="gemini-test")
        monkeypatch.setattr(brain, "client", lambda: type(
            "C", (), {"models": FakeModels()})())
        with pytest.raises(Exception) as caught:
            brain.ask_json("system", "prompt")
        assert "allowance" in str(caught.value) or "quota" in str(caught.value)


class TestTheOllamaRequest:

    def payload(self, monkeypatch, method, *args, **kwargs):
        seen = {}

        class Response:
            def raise_for_status(self):
                pass

            def json(self):
                return {"message": {"content": "[]"}}

        def fake_post(url, json=None, timeout=None):
            seen.update(json or {})
            return Response()

        import requests
        monkeypatch.setattr(requests, "post", fake_post)
        brain = thinker.Ollama()
        monkeypatch.setattr(brain, "model", lambda: "llama-test")
        getattr(brain, method)(*args, **kwargs)
        return seen

    def test_a_structured_ask_says_json(self, monkeypatch):
        assert self.payload(monkeypatch, "ask_json", "system",
                            "prompt")["format"] == "json"

    def test_a_spoken_ask_does_not(self, monkeypatch):
        assert "format" not in self.payload(monkeypatch, "ask", "system",
                                            "prompt", "medium")

    def test_the_room_is_passed_along(self, monkeypatch):
        sent = self.payload(monkeypatch, "ask_json", "system", "prompt",
                            "medium", 5000)
        assert sent["options"]["num_predict"] == 5000

    def test_the_conversation_is_unchanged(self, monkeypatch):
        sent = self.payload(monkeypatch, "ask", "system", "prompt", "medium")
        assert "options" not in sent
        assert sent["messages"][0]["role"] == "system"
        assert sent["stream"] is False


class TestEveryBackendAnswersIt:

    @pytest.mark.parametrize("brain", [thinker.Gemini(), thinker.Ollama(),
                                       thinker.Claude()])
    def test_it_is_part_of_the_contract(self, brain):
        assert callable(brain.ask_json)

    def test_one_without_a_structured_mode_falls_back_to_the_plain_question(self):
        """Claude's API has no JSON mode. The base implementation is the plain
        question, so a background agent can ask any backend the same way."""
        asked = []

        class Half(thinker.Backend):
            def ask(self, system, prompt, effort):
                asked.append(effort)
                return "[]"

        assert Half().ask_json("system", "prompt", "high", 9000) == "[]"
        assert asked == ["high"]


def _batch(count):
    """A batch of scripts that would actually pass review."""
    import json

    return json.dumps([
        {"title": f"thing {n}",
         "hook": f"Nobody tells you this about thing {n}.",
         "beats": [
             {"at": 2.0, "voiceover": "Here is the part everyone gets wrong.",
              "on_screen": "wrong", "visual": "a close shot of the thing"},
             {"at": 7.0, "voiceover": "It costs you every single time.",
              "on_screen": "every time", "visual": "a screen recording"},
             {"at": 12.0, "voiceover": "So do it this way instead.",
              "on_screen": "do this", "visual": "text on black"},
         ],
         "caption": f"The thing about thing {n}.",
         "hashtags": ["one", "#two"],
         "seconds": 30}
        for n in range(count)])


def test_the_module_still_exposes_what_the_pipeline_imports():
    assert hasattr(scriptwriter, "Scriptwriter")
    assert scriptwriter.MAX_PER_BATCH == 5
