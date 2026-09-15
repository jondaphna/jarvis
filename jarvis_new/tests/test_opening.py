"""Opening things: the user's own browser, not a stranger's.

The whole history of this bug is in these tests.

First the agent opened everything in a Playwright browser built with
new_context() - an incognito window, signed into nothing - so "open my Google"
could never be his Google. Then I pointed that browser at a persistent profile
so it could hold logins, which works technically and fails in practice: Google
blocks sign-in from an automated browser on purpose, so there was no way to put
the logins there.

The answer is to stop trying. His own Chrome is already signed in. Opening goes
there; a site's own search URL covers playing a song or finding a film without
driving anything; and the hidden browser is kept for the one thing it is good
at, which is reading a page so Jarvis can answer a question.
"""

import inspect

import launcher
import os_tools
import pytest
from tools import BrowserTools


class Recorder:
    """Stands in for the real browser and remembers what it was asked to open."""

    def __init__(self) -> None:
        self.opened: list[str] = []

    async def open_url(self, url: str) -> dict[str, str]:
        self.opened.append(url)
        return {"url": url, "title": "ok"}


@pytest.fixture
def tools(monkeypatch) -> tuple[os_tools.OSTools, list[str]]:
    """OS tools with the real browser launch intercepted."""
    opened: list[str] = []
    monkeypatch.setattr(launcher, "open_in_browser",
                        lambda url: opened.append(url) or "chrome")
    return os_tools.OSTools(), opened


class TestOpeningGoesToYourOwnBrowser:
    async def test_a_bare_site_name_is_resolved(self, tools) -> None:
        tool, opened = tools
        await tool.open_url._func(tool, None, "netflix")
        assert opened == ["https://www.netflix.com"]

    async def test_my_prefix_is_ignored(self, tools) -> None:
        """"Open my Google" is how people actually say it."""
        tool, opened = tools
        await tool.open_url._func(tool, None, "my google")
        assert opened == ["https://www.google.com"]

    async def test_a_real_url_is_left_alone(self, tools) -> None:
        tool, opened = tools
        await tool.open_url._func(tool, None, "https://example.com/watch?v=1")
        assert opened == ["https://example.com/watch?v=1"]

    async def test_a_program_is_refused_with_a_pointer(self, tools) -> None:
        from livekit.agents.llm import ToolError

        tool, _ = tools
        with pytest.raises(ToolError, match="open_app"):
            await tool.open_url._func(tool, None, "task manager")


class TestActingInsideASite:
    """Playing a song and finding a film, without a password anywhere."""

    async def test_it_opens_the_sites_own_search(self, tools) -> None:
        tool, opened = tools
        await tool.search_on_site._func(tool, None, "spotify", "daft punk")
        assert opened == ["https://open.spotify.com/search/daft%20punk"]

    async def test_netflix_by_title(self, tools) -> None:
        tool, opened = tools
        await tool.search_on_site._func(tool, None, "netflix", "Inception")
        assert opened == ["https://www.netflix.com/search?q=Inception"]

    async def test_a_site_with_no_search_link_says_so(self, tools) -> None:
        from livekit.agents.llm import ToolError

        tool, _ = tools
        with pytest.raises(ToolError):
            await tool.search_on_site._func(tool, None, "some-intranet", "x")

    def test_the_sites_he_actually_asked_for(self) -> None:
        for site in ("google", "netflix", "spotify", "youtube", "gmail"):
            assert launcher.search_url(site, "x"), f"{site} should be searchable"


class TestTheHiddenBrowserStaysHidden:
    def test_it_is_headless(self) -> None:
        """Visible, it was a second window showing a logged-out copy of
        whatever had just been asked for."""
        import agent

        source = inspect.getsource(agent.my_agent)
        assert "BrowserManager(headless=True)" in source

    def test_its_tool_is_not_called_open_anything(self) -> None:
        """Two tools whose names both read as "open" is how the model picked
        the wrong one for weeks."""
        assert hasattr(BrowserTools, "fetch_page")
        assert not hasattr(BrowserTools, "open_url")

    def test_it_says_plainly_that_the_user_cannot_see_it(self) -> None:
        # Whitespace-normalised: a docstring wraps, so matching raw text makes
        # the test fail on reflowing rather than on meaning.
        doc = " ".join((BrowserTools.fetch_page.__doc__ or "").split())
        assert "user never sees this" in doc
        assert "signed into nothing" in doc
        assert "use open_url" in doc, "it must point at the right tool"


class TestOnlyOneOpener:
    def test_exactly_one_tool_is_named_open_url(self) -> None:
        names = [t.info.name for t in os_tools.OSTools().tools]
        names += [t.info.name for t in BrowserTools(Recorder()).tools]
        assert names.count("open_url") == 1

    def test_the_opener_promises_the_users_own_accounts(self) -> None:
        doc = os_tools.OSTools.open_url.__doc__ or ""
        assert "their own accounts" in doc
        assert "THE tool" in doc
