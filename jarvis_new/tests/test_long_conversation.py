"""Why the conversation used to stop working after a while.

A Gemini Live session has a time limit. As it approaches, the server sends a
`go_away` message, and the plugin's handler for that closes the session - its
own source comment concedes the reconnection "isn't seamless just yet". So
Jarvis would go quiet partway through a conversation and lose the ability to do
anything, with nothing on screen explaining why.

Requesting session resumption is what fixes it: the server then issues a handle,
the plugin stores it, and on reconnect hands it back. Without asking, no handle
is ever issued, so there is nothing to resume from. These tests keep both that
and the context-window guard switched on.
"""

import inspect
import os

import pytest
from google.genai import types as genai_types

import agent

#: Constructing `Assistant()` builds the realtime model, which needs a LiveKit
#: key even though what is asserted below is only its configuration. No key, no
#: fixture - so these skip rather than erroring in collection.
pytestmark = pytest.mark.skipif(
    not os.environ.get("LIVEKIT_API_KEY"),
    reason="needs LIVEKIT_API_KEY to construct the realtime model",
)



class LiveContext:
    """A context that reports itself connected, like a real one."""

    def __init__(self, pages):
        self.pages = pages
        self.browser = type("B", (), {"is_connected": staticmethod(lambda: True)})()


@pytest.fixture
def options():
    """The realtime model's settings, as actually constructed."""
    return agent.Assistant().llm._opts


class TestTheSessionSurvives:
    def test_session_resumption_is_requested(self, options) -> None:
        """Without this the server issues no handle, and a dropped session is
        simply gone rather than resumable."""
        assert options.session_resumption is not None
        assert isinstance(options.session_resumption,
                          genai_types.SessionResumptionConfig)

    def test_the_context_window_cannot_fill_up(self, options) -> None:
        """The other way a long conversation ends. A sliding window drops the
        stalest turns instead of hitting the ceiling."""
        compression = options.context_window_compression
        assert compression is not None
        assert compression.sliding_window is not None

    def test_the_model_is_one_that_still_exists(self, options) -> None:
        """A retired model fails at connect time, not at import - which is the
        worst moment to discover it."""
        from livekit.plugins.google.realtime import realtime_api

        assert options.model in realtime_api.KNOWN_GEMINI_API_MODELS

    def test_the_voice_is_real(self, options) -> None:
        import typing

        from livekit.plugins.google.realtime import realtime_api

        voices = typing.get_args(realtime_api.Voice)
        assert options.voice in voices


class TestTheToolsSurviveToo:
    def test_the_browser_is_reattached_rather_than_assumed(self) -> None:
        """Jarvis attaches to a Chrome it does not own. If that window is
        closed, or Chrome restarts, every tool must recover rather than fail
        for the rest of the conversation."""
        from live_browser import LiveBrowser

        source = inspect.getsource(LiveBrowser.attach)
        assert "self._context is not None" in source, "it should reuse a live one"
        assert "self.launch()" in source, "and start one when there isn't"

    async def test_a_closed_window_does_not_end_the_session(self) -> None:
        """Every page-using tool goes through page(); if that cannot recover
        from a closed tab, one closed window breaks everything after it."""
        import live_browser

        class Closed:
            def is_closed(self): return True

        class Reopened:
            def is_closed(self): return False
            async def bring_to_front(self): pass

        made = Reopened()

        class Context(LiveContext):
            async def new_page(self): return made

        window = live_browser.LiveBrowser()
        window._context = Context([Closed()])
        assert await window.page() is made, "a closed tab should be replaced"


class TestOneClosedWindowDoesNotBreakEverything:
    """The second way "it stopped being able to do anything" happened.

    Closing the browser window leaves a dead connection behind. Every later
    tool call then failed with a raw Playwright error, which escapes the
    handler in BrowserTools and surfaces as a crash rather than a sentence -
    and the cached connection was never cleared, so it stayed broken for the
    rest of the conversation.
    """

    class DeadWindow:
        async def page(self):
            raise Exception("Target page, context or browser has been closed")

        async def goto(self, url, **kwargs):
            raise Exception("Target page, context or browser has been closed")

        async def attach(self): ...
        async def close(self): ...

    @pytest.mark.parametrize("call", ["read_page", "inspect_page", "go_back",
                                      "scroll", "press_key", "take_screenshot"])
    async def test_it_says_so_instead_of_crashing(self, call) -> None:
        from browser import BrowserError, BrowserManager

        manager = BrowserManager(live=self.DeadWindow())
        argument = {"scroll": ("down",), "press_key": ("Enter",)}.get(call, ())
        with pytest.raises(BrowserError, match="closed"):
            await getattr(manager, call)(*argument)

    async def test_clicking_and_typing_too(self) -> None:
        from browser import BrowserError, BrowserManager

        manager = BrowserManager(live=self.DeadWindow())
        with pytest.raises(BrowserError, match="closed"):
            await manager.click("Play")
        with pytest.raises(BrowserError, match="closed"):
            await manager.type_text("Search", "daft punk")

    def test_a_dead_connection_is_dropped_rather_than_reused(self) -> None:
        """Holding on to it is what made the breakage permanent."""
        import inspect

        from live_browser import LiveBrowser

        source = inspect.getsource(LiveBrowser.attach)
        assert "self._usable" in source
        assert "self._context = None" in source
