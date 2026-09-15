"""The thing that kept failing: "open my Google" opening a stranger's Google.

Two separate causes, both represented here.

1. The browser was started with launch() + new_context(), which is an incognito
   window - no cookies, no logins, fresh every time. Whatever you asked it to
   open, you arrived signed out.
2. open_url's own description told the model to prefer it "whenever the user
   names a website", while the system prompt said to use a different tool.
   A tool description wins that argument, so the second tool was never called.
"""

import inspect

import browser as browser_module
import launcher
import pytest
from browser import BrowserManager
from tools import BrowserTools


class Recorder:
    """Stands in for the real browser and remembers what it was asked to open."""

    def __init__(self) -> None:
        self.opened: list[str] = []

    async def open_url(self, url: str) -> dict[str, str]:
        self.opened.append(url)
        return {"url": url, "title": "ok"}


@pytest.fixture
def tools() -> tuple[BrowserTools, Recorder]:
    recorder = Recorder()
    return BrowserTools(recorder), recorder


class TestOpeningBySpokenName:
    """You say "open Netflix", not "open https://www.netflix.com"."""

    async def test_a_bare_site_name_is_resolved(self, tools) -> None:
        tool, recorder = tools
        await tool.open_url._func(tool, None, "netflix")
        assert recorder.opened == ["https://www.netflix.com"]

    async def test_my_prefix_is_ignored(self, tools) -> None:
        tool, recorder = tools
        await tool.open_url._func(tool, None, "my spotify")
        assert recorder.opened == ["https://open.spotify.com"]

    async def test_a_real_url_is_left_alone(self, tools) -> None:
        tool, recorder = tools
        await tool.open_url._func(tool, None, "https://example.com/watch?v=1")
        assert recorder.opened == ["https://example.com/watch?v=1"]

    async def test_something_unknown_is_passed_through(self, tools) -> None:
        """Rather than swallowed - the browser gives a better error than we would."""
        tool, recorder = tools
        await tool.open_url._func(tool, None, "some-internal-tool.local")
        assert recorder.opened == ["https://some-internal-tool.local"]


class TestTheBrowserKeepsYouSignedIn:
    def test_it_uses_a_persistent_profile(self) -> None:
        """launch() + new_context() is an incognito window. Everything opened in
        one is signed into nothing, which is the whole bug."""
        source = inspect.getsource(BrowserManager.start)
        assert "launch_persistent_context" in source
        # Checked as a call, not as a mention: the comment above it explains
        # what new_context() does wrong, and matching on the bare name would
        # catch the explanation rather than the code.
        assert "self._browser.new_context()" not in source
        assert "chromium.launch(" not in source

    def test_it_asks_for_real_chrome(self) -> None:
        """Bundled Chromium has no DRM, so Netflix and Spotify load and then
        refuse to play anything."""
        assert 'channel="chrome"' in inspect.getsource(BrowserManager.start)

    def test_the_profile_is_a_stable_directory(self, tmp_path, monkeypatch) -> None:
        first = browser_module.jarvis_profile_dir()
        second = browser_module.jarvis_profile_dir()
        assert first == second, "the profile must not move between runs"
        assert first.exists()

    def test_it_is_not_your_everyday_chrome_profile(self) -> None:
        """Chrome refuses to open a profile another Chrome already has, and
        yours is always open."""
        assert "Google" not in str(browser_module.jarvis_profile_dir())


class TestOnlyOneOpener:
    def test_open_url_claims_the_job_without_qualification(self) -> None:
        doc = BrowserTools.open_url.__doc__ or ""
        assert "THE tool" in doc
        assert "signed into their own accounts" in doc

    def test_no_second_opening_tool_exists(self) -> None:
        """Two tools for one job is how the model ends up choosing the wrong
        one, and the description it reads is not the prompt you wrote."""
        import os_tools

        names = {t.info.name for t in os_tools.OSTools().tools}
        assert "open_website" not in names

    def test_open_app_sends_websites_back_to_open_url(self) -> None:
        doc = None
        import os_tools

        for tool in os_tools.OSTools().tools:
            if tool.info.name == "open_app":
                doc = tool.info.description
        assert doc and "website" in doc.lower()


class TestSiteList:
    def test_the_sites_he_actually_named(self) -> None:
        for name in ("google", "netflix", "spotify", "youtube"):
            assert launcher.site_url(name), f"{name} should be openable by name"
