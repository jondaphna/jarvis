"""Opening things, and then actually doing them.

The long version of this bug, so it cannot come back:

1. The agent opened pages in a Playwright browser built with new_context() -
   an incognito window, signed into nothing - so "open my Google" was never his.
2. Pointing that browser at a saved profile could not fix it: Google blocks
   sign-in from an automated browser on purpose, so the logins could never be
   put there.
3. Opening in his own Chrome by launching it as a normal program fixed the
   logins and lost everything else - Jarvis could not see or touch the page,
   and every request opened another tab.

What works is launching an ordinary Chrome and *attaching* to it afterwards.
Ordinary Chrome is what sign-in pages see; attaching is what lets Jarvis read
the page, type in it and click. One window, one tab, reused.
"""

import inspect

import launcher
import live_browser
import os_tools
import pytest
from browser import BrowserManager
from tools import BrowserTools


class Recorder:
    """Stands in for the window and remembers where it was sent."""

    def __init__(self) -> None:
        self.visited: list[str] = []

    async def open_url(self, url: str) -> dict[str, str]:
        self.visited.append(url)
        return {"url": url, "title": "ok"}


@pytest.fixture
def tools() -> tuple[BrowserTools, Recorder]:
    recorder = Recorder()
    return BrowserTools(recorder), recorder


class TestOpeningBySpokenName:
    async def test_a_bare_site_name_is_resolved(self, tools) -> None:
        tool, seen = tools
        await tool.open_url._func(tool, None, "netflix")
        assert seen.visited == ["https://www.netflix.com"]

    async def test_my_prefix_is_ignored(self, tools) -> None:
        """"Open my Google" is how people actually say it."""
        tool, seen = tools
        await tool.open_url._func(tool, None, "my google")
        assert seen.visited == ["https://www.google.com"]

    async def test_a_real_url_is_left_alone(self, tools) -> None:
        tool, seen = tools
        await tool.open_url._func(tool, None, "https://example.com/watch?v=1")
        assert seen.visited == ["https://example.com/watch?v=1"]

    async def test_a_program_is_refused_with_a_pointer(self, tools) -> None:
        from livekit.agents.llm import ToolError

        tool, _ = tools
        with pytest.raises(ToolError, match="open_app"):
            await tool.open_url._func(tool, None, "task manager")


class TestActingInsideASite:
    """Playing a song and finding a film, landing on the thing itself."""

    async def test_it_goes_to_the_sites_own_search(self, tools) -> None:
        tool, seen = tools
        await tool.search_on_site._func(tool, None, "spotify", "daft punk")
        assert seen.visited == ["https://open.spotify.com/search/daft%20punk"]

    async def test_netflix_by_title(self, tools) -> None:
        tool, seen = tools
        await tool.search_on_site._func(tool, None, "netflix", "Inception")
        assert seen.visited == ["https://www.netflix.com/search?q=Inception"]

    async def test_a_site_with_no_search_link_says_so(self, tools) -> None:
        from livekit.agents.llm import ToolError

        tool, _ = tools
        with pytest.raises(ToolError):
            await tool.search_on_site._func(tool, None, "some-intranet", "x")

    def test_the_sites_he_actually_asked_for(self) -> None:
        for site in ("google", "netflix", "spotify", "youtube", "gmail"):
            assert launcher.search_url(site, "x"), f"{site} should be searchable"


class TestOneWindowReused:
    def test_the_manager_does_not_start_its_own_browser(self) -> None:
        """A browser Playwright launches is signed into nothing and is a
        second window besides."""
        source = inspect.getsource(BrowserManager)
        assert "launch_persistent_context" not in source
        assert "chromium.launch(" not in source
        assert "_live" in source

    def test_every_tool_goes_through_the_one_visible_page(self) -> None:
        """One method feeds them all, so nothing can quietly act on a
        different window."""
        source = inspect.getsource(BrowserManager._get_page)
        assert "self._live.page()" in source

    def test_there_is_exactly_one_get_page(self) -> None:
        """Two definitions in one class body means the later silently wins -
        which is how the rewrite shipped broken the first time."""
        source = inspect.getsource(BrowserManager)
        assert source.count("async def _get_page") == 1

    async def test_the_current_tab_is_reused(self) -> None:
        """Asking for Spotify and then for a song must not leave two Spotifys."""

        class FakePage:
            def __init__(self) -> None:
                self.closed = False
                self.fronted = False

            def is_closed(self) -> bool:
                return self.closed

            async def bring_to_front(self) -> None:
                self.fronted = True

        existing = FakePage()

        class FakeContext:
            pages = [existing]
            # A real context reports whether its browser is still connected;
            # attach() checks that before reusing one, so the fake needs it.
            browser = type("B", (), {"is_connected": staticmethod(lambda: True)})()

            async def new_page(self):
                raise AssertionError("it must reuse the tab, not open another")

        window = live_browser.LiveBrowser()
        window._context = FakeContext()
        page = await window.page()
        assert page is existing
        assert existing.fronted, "the tab should be brought forward"


class TestHowChromeIsStarted:
    def test_chrome_is_not_started_by_playwright(self) -> None:
        """A browser Playwright launches announces itself as automated, and
        sign-in pages refuse it."""
        source = inspect.getsource(live_browser.LiveBrowser.launch)
        assert "subprocess.Popen" in source
        assert "--enable-automation" not in source

    def test_it_uses_its_own_profile(self) -> None:
        """Chrome ignores the debugging port entirely on the default profile,
        so this is required rather than tidy."""
        source = inspect.getsource(live_browser.LiveBrowser.launch)
        assert "--user-data-dir=" in source
        assert "--remote-debugging-port=" in source

    def test_the_profile_is_stable_between_runs(self) -> None:
        assert live_browser.profile_dir() == live_browser.profile_dir()

    def test_seeding_never_writes_to_your_profile(self) -> None:
        """It may read your cookies to save you signing in. It must never
        touch the profile you actually use."""
        source = inspect.getsource(live_browser.seed_from_your_chrome)
        assert "shutil.copy2(source" in source or "shutil.copy2(source_root" in source
        assert "destination" in source


class TestOnlyOneOpener:
    def test_exactly_one_tool_is_named_open_url(self) -> None:
        names = [t.info.name for t in os_tools.OSTools().tools]
        names += [t.info.name for t in BrowserTools(Recorder()).tools]
        assert names.count("open_url") == 1

    def test_the_opener_promises_the_same_window(self) -> None:
        doc = " ".join((BrowserTools.open_url.__doc__ or "").split())
        assert "THE tool" in doc
        assert "reusing the tab" in doc
