"""Which brain answers, and the free local path.

The point of this: everyday talk should cost nothing, a code word should pull
in Claude, and a flaky local model must never end the conversation.
"""

from __future__ import annotations

import pytest

from jarvis.core.brain import DEFAULT_ESCALATION_PHRASES, find_escalation
from jarvis.providers.base import AdaptedResponse, TextBlock, ToolUseBlock
from jarvis.providers.ollama_provider import _to_ollama_messages, _to_ollama_tool

class TestEscalationPhrases:
    @pytest.mark.parametrize("text,expected", [
        ("heavy guns, research the competition", "research the competition"),
        ("research the competition - heavy guns", "research the competition"),
        ("USE CLAUDE: summarise the market", "summarise the market"),
        ("full power please, write the plan", "please, write the plan"),
    ])
    def test_phrase_is_detected_and_stripped(self, text, expected):
        escalate, cleaned = find_escalation(text, DEFAULT_ESCALATION_PHRASES)
        assert escalate
        assert cleaned == expected

    @pytest.mark.parametrize("text", [
        "just say hi",
        "the guns were heavy",
        "what's the weather",
    ])
    def test_ordinary_talk_does_not_escalate(self, text):
        escalate, cleaned = find_escalation(text, DEFAULT_ESCALATION_PHRASES)
        assert not escalate
        assert cleaned == text

    def test_custom_phrase_works(self):
        escalate, cleaned = find_escalation("totach, do the thing", ["totach"])
        assert escalate and cleaned == "do the thing"

    def test_stripping_never_empties_the_request(self):
        _, cleaned = find_escalation("heavy guns", DEFAULT_ESCALATION_PHRASES)
        assert cleaned


class TestOllamaFormat:
    def test_tool_definition_conversion(self):
        converted = _to_ollama_tool({
            "name": "open_app", "description": "Open an app",
            "input_schema": {"type": "object", "properties": {"name": {"type": "string"}}},
        })
        assert converted["type"] == "function"
        assert converted["function"]["name"] == "open_app"
        assert converted["function"]["parameters"]["properties"]["name"]

    def test_tool_results_become_tool_role_messages(self):
        messages = _to_ollama_messages([
            {"role": "user", "content": "open chrome"},
            {"role": "assistant",
             "content": [TextBlock("Opening."), ToolUseBlock("open_app",
                                                             {"name": "chrome"}, "t1")]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1",
                                          "content": "Opened chrome"}]},
        ], system="You are JARVIS.")

        assert messages[0]["role"] == "system"
        assistant = next(m for m in messages if m["role"] == "assistant")
        assert assistant["tool_calls"][0]["function"]["name"] == "open_app"
        tool = next(m for m in messages if m["role"] == "tool")
        assert tool["content"] == "Opened chrome"

    def test_plain_conversation_passes_through(self):
        messages = _to_ollama_messages([{"role": "user", "content": "hi"}], None)
        assert messages == [{"role": "user", "content": "hi"}]


class FakeOllama:
    """Stands in for a running Ollama server."""

    name = "ollama"
    supports_tools = True

    def __init__(self, responses=None, fail=False):
        self.responses = list(responses or [])
        self.fail = fail
        self.calls = 0

    def available(self):
        return True

    def default_model(self):
        return "llama3.1"

    async def turn(self, **kwargs):
        self.calls += 1
        if self.fail:
            raise RuntimeError("ollama fell over")
        if self.responses:
            return self.responses.pop(0)
        return AdaptedResponse(content=[TextBlock("local says hi")], model="llama3.1")


class TestBrainRouting:
    @pytest.fixture
    def brain(self, settings, memory, broker, registry):
        from jarvis.config import Config
        from jarvis.core.brain import Brain

        config = Config()
        config.settings = settings
        return Brain(config, memory, broker, registry)

    def _use_claude(self, brain, *responses):
        from tests.test_brain import FakeClaude
        fake = FakeClaude(list(responses))
        type(brain).anthropic = property(lambda self, _f=fake: _f)
        return fake

    async def test_local_model_answers_for_free(self, brain, memory):
        brain.providers["ollama"] = FakeOllama()
        reply = await brain.chat("hi there")
        assert reply.provider == "local"
        assert reply.text == "local says hi"
        assert reply.cost_usd == 0.0
        assert memory.spend_since(24) == 0.0

    async def test_code_word_brings_in_claude(self, brain):
        from tests.test_brain import FakeResponse, FakeText

        brain.providers["ollama"] = FakeOllama()
        fake = self._use_claude(brain, FakeResponse(content=[FakeText("Claude here.")]))
        reply = await brain.chat("heavy guns, plan my quarter")
        assert reply.provider == "claude"
        assert reply.text == "Claude here."
        # The phrase itself must not reach the model.
        assert "heavy guns" not in fake.calls[0]["messages"][-1]["content"].lower()

    async def test_missions_always_use_claude(self, brain):
        from tests.test_brain import FakeResponse, FakeText

        brain.providers["ollama"] = FakeOllama()
        self._use_claude(brain, FakeResponse(content=[FakeText("deep work")]))
        reply = await brain.chat("do the thing", tier="deep")
        assert reply.provider == "claude"

    async def test_claude_used_when_ollama_is_down(self, brain):
        from tests.test_brain import FakeResponse, FakeText

        class Down(FakeOllama):
            def available(self):
                return False

        brain.providers["ollama"] = Down()
        self._use_claude(brain, FakeResponse(content=[FakeText("Claude fallback.")]))
        reply = await brain.chat("hi")
        assert reply.provider == "claude"

    async def test_local_failure_falls_back_mid_turn(self, brain):
        from tests.test_brain import FakeResponse, FakeText

        brain.settings.set("thinking.allow_paid", True)
        brain.providers["ollama"] = FakeOllama(fail=True)
        self._use_claude(brain, FakeResponse(content=[FakeText("Rescued.")]))
        reply = await brain.chat("hi")
        assert reply.text == "Rescued."
        assert reply.provider == "claude"
        assert reply.ok

    async def test_a_local_stumble_is_rescued_for_free(self, brain, monkeypatch):
        """The conversation must survive a wobbly local model without that
        alone turning into a bill. Free brain first, paid only if allowed."""
        brain.settings.set("thinking.allow_paid", False)
        brain.providers["ollama"] = FakeOllama(fail=True)
        monkeypatch.setattr(brain.providers["gemini"], "available", lambda: True)

        rescue = brain._rescue_brain("general")
        assert rescue is not None and rescue[2] == "gemini-free"

    async def test_nothing_free_and_paid_off_means_no_rescue(self, brain, monkeypatch):
        """Better to say the turn failed than to spend money nobody agreed to."""
        brain.settings.set("thinking.allow_paid", False)
        monkeypatch.setattr(brain.providers["gemini"], "available", lambda: False)
        assert brain._rescue_brain("general") is None

    async def test_local_model_can_use_tools(self, brain, workspace):
        brain.providers["ollama"] = FakeOllama([
            AdaptedResponse(
                content=[ToolUseBlock("write_file",
                                      {"path": "local.txt", "content": "made locally"},
                                      "o1")],
                stop_reason="tool_use", model="llama3.1"),
            AdaptedResponse(content=[TextBlock("Written.")], model="llama3.1"),
        ])
        reply = await brain.chat("write a file")
        assert reply.provider == "local"
        assert reply.tool_calls == ["write_file"]
        assert (workspace / "local.txt").read_text() == "made locally"
        assert reply.cost_usd == 0.0

    async def test_local_still_obeys_the_permission_gate(self, brain):
        """A free brain is not a less-supervised brain."""
        brain.providers["ollama"] = FakeOllama([
            AdaptedResponse(content=[ToolUseBlock("allow_app", {"name": "photoshop"}, "o1")],
                            stop_reason="tool_use", model="llama3.1"),
            AdaptedResponse(content=[TextBlock("Blocked, as expected.")], model="llama3.1"),
        ])
        reply = await brain.chat("start using photoshop")
        assert reply.blocked

    async def test_always_local_setting_is_respected(self, brain, settings):
        settings.set("brain.local_first", True)
        brain.providers["ollama"] = FakeOllama()
        reply = await brain.chat("anything")
        assert reply.provider == "local"

    async def test_always_claude_setting_is_respected(self, brain, settings):
        from tests.test_brain import FakeResponse, FakeText

        settings.set("brain.local_first", False)
        brain.providers["ollama"] = FakeOllama()
        self._use_claude(brain, FakeResponse(content=[FakeText("Claude only.")]))
        reply = await brain.chat("anything")
        assert reply.provider == "claude"
