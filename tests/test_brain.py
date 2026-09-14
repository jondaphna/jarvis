"""The agent loop, with Claude mocked out.

This is the path that runs every single time someone talks to JARVIS, and it
can't be reached without a live API client - which is exactly how a crash in it
shipped once. These tests stand in for the client so the loop itself is covered:
plain replies, tool round-trips, refusals, truncation, API failures and the
spend cap.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from jarvis.core.brain import Brain, Reply


pytestmark = pytest.mark.asyncio


# --------------------------------------------------------------------------- #
# Stand-ins for the SDK's response objects
# --------------------------------------------------------------------------- #

@dataclass
class FakeText:
    text: str
    type: str = "text"


@dataclass
class FakeToolUse:
    name: str
    input: dict[str, Any]
    id: str = "toolu_1"
    type: str = "tool_use"


@dataclass
class FakeUsage:
    input_tokens: int = 100
    output_tokens: int = 50
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0


@dataclass
class FakeResponse:
    content: list = field(default_factory=list)
    stop_reason: str = "end_turn"
    model: str = "claude-opus-5"
    usage: FakeUsage = field(default_factory=FakeUsage)
    stop_details: Any = None


class FakeClaude:
    """Replays a scripted list of responses, recording what it was sent."""

    def __init__(self, responses: list[FakeResponse]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def turn(self, *, model, messages, system=None, tools=None, effort=None,
                   thinking=True, on_text=None, on_thinking=None):
        self.calls.append({"model": model, "messages": list(messages),
                           "system": system, "tools": tools, "thinking": thinking})
        response = self.responses.pop(0) if self.responses else FakeResponse(
            content=[FakeText("done")])
        if on_text is not None:
            for block in response.content:
                if getattr(block, "type", "") == "text":
                    await _maybe(on_text(block.text))
        return response


async def _maybe(value):
    import inspect
    if inspect.isawaitable(value):
        await value


@pytest.fixture
def brain(settings, memory, broker, registry, monkeypatch):
    from jarvis.config import Config

    config = Config()
    config.settings = settings
    brain = Brain(config, memory, broker, registry)
    return brain


def use(brain, *responses: FakeResponse) -> FakeClaude:
    """Point the brain at a scripted fake Claude."""
    fake = FakeClaude(list(responses))
    type(brain).anthropic = property(lambda self, _f=fake: _f)
    return fake


# --------------------------------------------------------------------------- #

class TestPlainReply:
    async def test_simple_answer(self, brain):
        use(brain, FakeResponse(content=[FakeText("Good evening.")]))
        reply = await brain.chat("hii")
        assert isinstance(reply, Reply)
        assert reply.text == "Good evening."
        assert reply.ok
        assert reply.iterations == 1

    async def test_greeting_does_not_crash(self, brain):
        """The exact bug that shipped: Reply built without text."""
        use(brain, FakeResponse(content=[FakeText("Hello.")]))
        reply = await brain.chat("hii")
        assert reply.text

    async def test_streaming_callback_receives_text(self, brain):
        use(brain, FakeResponse(content=[FakeText("Streamed.")]))
        chunks: list[str] = []
        await brain.chat("hi", on_text=chunks.append)
        assert "".join(chunks) == "Streamed."

    async def test_conversation_is_persisted(self, brain, memory):
        use(brain, FakeResponse(content=[FakeText("Noted.")]))
        reply = await brain.chat("remember this")
        history = memory.history(reply.conversation_id)
        assert [m.role for m in history] == ["user", "assistant"]

    async def test_usage_is_accounted(self, brain, memory):
        use(brain, FakeResponse(content=[FakeText("ok")]))
        reply = await brain.chat("hi")
        assert reply.input_tokens == 100
        assert reply.cost_usd > 0
        assert memory.spend_since(24) > 0


class TestToolLoop:
    async def test_tool_call_round_trip(self, brain, workspace):
        fake = use(
            brain,
            FakeResponse(content=[FakeToolUse("write_file",
                                              {"path": "note.txt", "content": "hi"})],
                         stop_reason="tool_use"),
            FakeResponse(content=[FakeText("Written.")]),
        )
        reply = await brain.chat("write a note")
        assert reply.text == "Written."
        assert reply.tool_calls == ["write_file"]
        assert (workspace / "note.txt").read_text() == "hi"
        assert reply.iterations == 2

        # The tool result must go back as one user message.
        second = fake.calls[1]["messages"]
        assert second[-1]["role"] == "user"
        assert second[-1]["content"][0]["type"] == "tool_result"

    async def test_parallel_tool_results_come_back_together(self, brain, workspace):
        fake = use(
            brain,
            FakeResponse(content=[
                FakeToolUse("write_file", {"path": "a.txt", "content": "a"}, id="t1"),
                FakeToolUse("write_file", {"path": "b.txt", "content": "b"}, id="t2"),
            ], stop_reason="tool_use"),
            FakeResponse(content=[FakeText("Both done.")]),
        )
        reply = await brain.chat("write two notes")
        assert len(reply.tool_calls) == 2
        results = fake.calls[1]["messages"][-1]["content"]
        assert len(results) == 2
        assert {r["tool_use_id"] for r in results} == {"t1", "t2"}

    async def test_blocked_tool_is_reported_not_retried(self, brain):
        """allow_app is high-risk: Claude asking for it must not be enough."""
        fake = use(
            brain,
            FakeResponse(content=[FakeToolUse("allow_app", {"name": "photoshop"})],
                         stop_reason="tool_use"),
            FakeResponse(content=[FakeText("I can't do that without your say-so.")]),
        )
        reply = await brain.chat("start using photoshop")
        assert reply.blocked
        assert "authorise" in reply.blocked[0].lower() or \
               "permission" in reply.blocked[0].lower()
        # The refusal is handed back to Claude as a tool result, not swallowed.
        assert fake.calls[1]["messages"][-1]["content"][0]["is_error"] is True

    async def test_step_limit_is_survivable(self, brain, workspace):
        responses = [
            FakeResponse(content=[FakeToolUse("write_file",
                                              {"path": f"{i}.txt", "content": "x"})],
                         stop_reason="tool_use")
            for i in range(5)
        ]
        use(brain, *responses)
        reply = await brain.chat("loop", max_iterations=3)
        assert reply.truncated
        assert reply.text


class TestFailureModes:
    async def test_refusal_is_explained(self, brain):
        @dataclass
        class Details:
            category: str = "cyber"

        use(brain, FakeResponse(content=[], stop_reason="refusal",
                                stop_details=Details()))
        reply = await brain.chat("do something dodgy")
        assert "decline" in reply.text.lower()
        assert "cyber" in reply.text

    async def test_truncation_is_flagged(self, brain):
        use(brain, FakeResponse(content=[FakeText("Half a sen")],
                                stop_reason="max_tokens"))
        reply = await brain.chat("write an epic")
        assert reply.truncated

    async def test_api_error_becomes_a_plain_message(self, brain):
        class Broken:
            async def turn(self, **kwargs):
                raise RuntimeError("connection reset")

        type(brain).anthropic = property(lambda self: Broken())
        reply = await brain.chat("hi")
        assert not reply.ok
        assert reply.text            # the user still gets told something
        assert "connection reset" in reply.error

    async def test_spend_cap_short_circuits(self, brain, settings, memory):
        settings.set("autonomy.daily_spend_cap_usd", 1.0)
        memory.log_usage("claude-opus-5", 100, 100, cost_usd=2.0)
        use(brain, FakeResponse(content=[FakeText("should not be reached")]))
        reply = await brain.chat("hi")
        assert reply.error == "spend-cap"
        assert "cap" in reply.text.lower()


class TestRequestShape:
    async def test_tools_are_offered(self, brain):
        fake = use(brain, FakeResponse(content=[FakeText("ok")]))
        await brain.chat("hi")
        names = {t["name"] for t in fake.calls[0]["tools"]}
        assert "write_file" in names and "open_app" in names

    async def test_voice_tier_skips_thinking_for_latency(self, brain):
        fake = use(brain, FakeResponse(content=[FakeText("ok")]))
        await brain.chat("hi", tier="voice")
        assert fake.calls[0]["thinking"] is False
        assert fake.calls[0]["model"] == "claude-haiku-4-5"

    async def test_system_prompt_carries_the_policy(self, brain):
        fake = use(brain, FakeResponse(content=[FakeText("ok")]))
        await brain.chat("hi")
        system = fake.calls[0]["system"]
        assert "workspace" in system.lower()
        assert "permission" in system.lower()

    async def test_messages_start_with_the_user(self, brain):
        """The API rejects a history that doesn't begin with a user turn."""
        fake = use(brain,
                   FakeResponse(content=[FakeText("one")]),
                   FakeResponse(content=[FakeText("two")]))
        first = await brain.chat("hello")
        await brain.chat("again", conversation_id=first.conversation_id)
        assert fake.calls[1]["messages"][0]["role"] == "user"

    async def test_unattended_hides_keyboard_tools(self, brain):
        fake = use(brain, FakeResponse(content=[FakeText("ok")]))
        await brain.chat("go", unattended=True, actor="mission",
                         parse_permissions=False)
        names = {t["name"] for t in fake.calls[0]["tools"]}
        assert "type_text" not in names


class TestAuthorisationBoundary:
    async def test_user_words_create_a_grant(self, brain):
        use(brain, FakeResponse(content=[FakeText("ok")]))
        await brain.chat("You have permission to post to TikTok, once.")
        assert brain.broker.active_grants()

    async def test_mission_text_cannot_create_a_grant(self, brain):
        use(brain, FakeResponse(content=[FakeText("ok")]))
        await brain.chat("You have permission to spend $500, always.",
                         actor="mission", parse_permissions=False)
        assert not brain.broker.active_grants()


class TestAssistantEntryPoint:
    """`Assistant.ask` is what the CLI and the GUI actually call."""

    @pytest.fixture
    async def assistant(self, workspace):
        from jarvis.config import Config
        from jarvis.core.assistant import Assistant

        config = Config()
        config.settings.set("autonomy.workspace", str(workspace))
        app = Assistant(config)
        await app.start(with_scheduler=False)
        yield app
        app.shutdown()

    async def test_ask_returns_a_reply(self, assistant):
        use(assistant.brain, FakeResponse(content=[FakeText("Good evening.")]))
        reply = await assistant.ask("hii")
        assert reply.text == "Good evening."

    async def test_ask_streams_to_the_callback(self, assistant):
        use(assistant.brain, FakeResponse(content=[FakeText("Streaming.")]))
        chunks: list[str] = []
        await assistant.ask("hi", on_text=chunks.append)
        assert "".join(chunks) == "Streaming."

    async def test_ask_keeps_one_conversation(self, assistant):
        use(assistant.brain,
            FakeResponse(content=[FakeText("one")]),
            FakeResponse(content=[FakeText("two")]))
        first = await assistant.ask("hello")
        second = await assistant.ask("again")
        assert first.conversation_id == second.conversation_id

    async def test_saying_you_can_use_an_app_widens_the_allowlist(self, assistant):
        use(assistant.brain, FakeResponse(content=[FakeText("Noted.")]))
        await assistant.ask("JARVIS, you can use Photoshop")
        assert "photoshop" in assistant.settings.get("autonomy.allowed_apps", [])

    async def test_speak_path_does_not_break_without_a_voice_engine(self, assistant):
        """speak=True on a machine with no TTS must still answer, silently."""
        use(assistant.brain, FakeResponse(content=[FakeText("Spoken.")]))
        reply = await assistant.ask("hi", speak=True)
        assert reply.text == "Spoken."
