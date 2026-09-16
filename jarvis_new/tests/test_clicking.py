"""Finding the thing on the page that the user meant.

This is where "play Daft Punk on Spotify" actually succeeds or fails. Landing
on the search results is the easy half; the song only plays if the click lands.

The problem is that the model describes a control the way a person would - "the
play button" - and the page names it the way a screen reader would - "Play Get
Lucky by Daft Punk". Matching those was strict substring, so the most natural
thing to say was the thing that did not work. Worse, the prompt suggested that
exact wording.

So: the loose forms of a target are tested directly, and the whole resolution
is tested against markup shaped like a real player - with the real browser,
because Playwright's accessible-name matching is the thing under test and a
mock of it would only prove the mock works.
"""

import pytest

import browser as browser_module

CHROMIUM = "/opt/pw-browsers/chromium-1194/chrome-linux/chrome"

#: Shaped like a streaming search page: the play control's accessible name is
#: the track, not the word "play", and there is a list of results.
PLAYER_PAGE = """
<h1>Search results for Daft Punk</h1>
<div role="row"><span>Get Lucky</span>
  <button aria-label="Play Get Lucky by Daft Punk">&#9654;</button></div>
<div role="row"><span>One More Time</span>
  <button aria-label="Play One More Time by Daft Punk">&#9654;</button></div>
<input type="search" placeholder="What do you want to play?">
<a href="#">Liked Songs</a>
<button aria-label="Add to queue">+</button>
"""


class TestTheLooseFormsOfATarget:
    """Pure logic, so this holds even where a browser can't run."""

    def test_a_trailing_noun_is_dropped(self) -> None:
        """"Play button" is how a person says it. "Play" is how the page
        labels it. This one line is the whole Spotify bug."""
        assert "play" in browser_module.target_variants("play button")

    def test_a_leading_article_is_dropped(self) -> None:
        assert "play" in browser_module.target_variants("the play button")

    def test_the_original_is_always_tried_first(self) -> None:
        """Loosening is a fallback, never a replacement: an exact match is
        always the better answer when there is one."""
        assert browser_module.target_variants("Liked Songs")[0] == "Liked Songs"

    def test_it_does_not_loosen_something_into_nothing(self) -> None:
        for target in ("button", "the", "link", "  "):
            assert all(v.strip() for v in browser_module.target_variants(target)), target

    def test_variants_are_not_repeated(self) -> None:
        variants = browser_module.target_variants("the play button")
        assert len(variants) == len(set(variants))

    @pytest.mark.parametrize("said,wanted", [
        ("the search box", "search"),
        ("play icon", "play"),
        ("submit button", "submit"),
        ("a sign in link", "sign in"),
        ("the next control", "next"),
    ])
    def test_the_ways_people_name_controls(self, said, wanted) -> None:
        assert wanted in browser_module.target_variants(said), browser_module.target_variants(said)


@pytest.mark.skipif(not __import__("pathlib").Path(CHROMIUM).exists(),
                    reason="no browser on this machine")
class TestAgainstARealPage:
    """The end-to-end question: does the click land?"""

    async def page(self, playwright):
        browser = await playwright.chromium.launch(executable_path=CHROMIUM)
        page = await browser.new_page()
        await page.set_content(PLAYER_PAGE)
        return browser, page

    @pytest.fixture
    async def resolved(self):
        from playwright.async_api import async_playwright

        manager = browser_module.BrowserManager.__new__(browser_module.BrowserManager)
        async with async_playwright() as playwright:
            browser, page = await self.page(playwright)

            async def resolve(target: str) -> str:
                locator = await manager._resolve_target(page, target)
                return ((await locator.get_attribute("aria-label"))
                        or (await locator.inner_text())).strip()

            yield resolve
            await browser.close()

    @pytest.mark.parametrize("target", [
        "play button", "the play button", "play", "Play Get Lucky by Daft Punk",
        "Get Lucky",
    ])
    async def test_every_way_of_asking_for_play_lands(self, resolved, target) -> None:
        """The model will say one of these. All of them have to work - the
        first two used to fail, which is why the song never started."""
        assert "Play" in await resolved(target)

    async def test_an_exact_name_still_wins(self, resolved) -> None:
        assert await resolved("Liked Songs") == "Liked Songs"
        assert await resolved("Add to queue") == "Add to queue"

    async def test_a_control_that_is_not_there_says_what_is(self, resolved) -> None:
        """A bare "I could not find it" leaves the model guessing blind. Naming
        what IS on the page lets it pick and carry on instead of giving up."""
        with pytest.raises(browser_module.BrowserError) as raised:
            await resolved("the enormous purple submit button")
        message = str(raised.value)
        assert "Liked Songs" in message or "Add to queue" in message, message

    @pytest.mark.parametrize("target", [
        "search", "search box", "the search box", "What do you want to play?",
    ])
    async def test_typing_into_the_search_box(self, resolved, target) -> None:
        from playwright.async_api import async_playwright

        manager = browser_module.BrowserManager.__new__(browser_module.BrowserManager)
        async with async_playwright() as playwright:
            browser, page = await self.page(playwright)
            try:
                await manager._resolve_textbox(page, target)
            finally:
                await browser.close()


class TestThePromptDoesNotSteerItWrong:
    def test_it_no_longer_suggests_a_phrase_that_fails(self) -> None:
        """The prompt used to tell it to click "a play button" - the exact
        wording that did not resolve. Documentation pointing at a bug."""
        import prompts

        assert "a play button" not in prompts.AGENT_INSTRUCTIONS

    def test_it_says_to_look_before_clicking(self) -> None:
        import prompts

        assert "inspect_page" in prompts.AGENT_INSTRUCTIONS
