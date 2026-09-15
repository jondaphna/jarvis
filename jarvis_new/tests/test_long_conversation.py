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

import agent
import pytest
from google.genai import types as genai_types


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

        class Context:
            pages = [Closed()]
            async def new_page(self): return made

        window = live_browser.LiveBrowser()
        window._context = Context()
        assert await window.page() is made, "a closed tab should be replaced"
