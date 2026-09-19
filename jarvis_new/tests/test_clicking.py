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


# --------------------------------------------------------------------------
# One app proved the fix; these are the apps it has to keep working on.
#
# Each page below is shaped like the real one - the accessible names are the
# kind the app actually publishes, with the shortcut hints, counts and product
# names left in, because those are exactly what strict matching trips over.
# --------------------------------------------------------------------------

#: Mail. Toolbar controls are icons with a bare label, the search field says
#: what it searches rather than "search", and "More email options" carries a
#: word in the middle that nobody says out loud.
GMAIL_PAGE = """
<div role="button" tabindex="0">Compose</div>
<input type="text" aria-label="Search mail" placeholder="Search mail">
<div role="checkbox" aria-label="Select all conversations"></div>
<div role="button" aria-label="Archive">&#128230;</div>
<div role="button" aria-label="Delete">&#128465;</div>
<div role="button" aria-label="More email options">&#8942;</div>
<span role="button" aria-label="Not starred">&#9734;</span>
<div role="button" aria-label="Send (Ctrl-Enter)">Send</div>
"""

#: A video page. Every player control hides a keyboard hint in its label, the
#: volume is a slider and autoplay is a switch - neither of which was a role
#: this ever looked at - and "like" sits inside "Dislike" as a substring.
YOUTUBE_PAGE = """
<input id="search" aria-label="Search" placeholder="Search">
<button aria-label="Play (k)">&#9654;</button>
<button aria-label="Next (SHIFT+n)">&#9197;</button>
<button aria-label="Mute (m)">&#128266;</button>
<div role="slider" aria-label="Volume" aria-valuenow="100" tabindex="0"></div>
<button role="switch" aria-label="Autoplay is on" aria-checked="true">A</button>
<button aria-label="Settings">&#9881;</button>
<button aria-label="Full screen (f)">&#9974;</button>
<button aria-label="like this video along with 12,345 other people">&#128077;</button>
<button aria-label="Dislike this video">&#128078;</button>
<button aria-label="Subscribe to Daft Punk.">Subscribe</button>
"""

#: Chat. The composer is a text box named after the channel, and there is a
#: "New message" button on the same screen - so "the message box" and "the new
#: message button" are two different controls that both contain "message".
SLACK_PAGE = """
<button aria-label="Search Acme Corp">Search</button>
<button aria-label="New message">&#9998;</button>
<div role="textbox" aria-label="Message #general" contenteditable="true"></div>
<button aria-label="Emoji">&#128512;</button>
<button aria-label="Send now">&#10148;</button>
<button aria-label="View 3 replies">3 replies</button>
<div role="switch" aria-label="Notify me about replies" tabindex="0"></div>
<button aria-label="Start huddle">&#127911;</button>
"""


def _resolves(markup: str):
    """A fixture that answers "what would a click on this phrase land on?"."""

    @pytest.fixture
    async def resolved(self):
        from playwright.async_api import async_playwright

        manager = browser_module.BrowserManager.__new__(browser_module.BrowserManager)
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(executable_path=CHROMIUM)
            page = await browser.new_page()
            await page.set_content(markup)

            async def resolve(target: str) -> str:
                locator = await manager._resolve_target(page, target)
                return ((await locator.get_attribute("aria-label"))
                        or (await locator.inner_text())).strip()

            try:
                yield resolve
            finally:
                await browser.close()

    return resolved


needs_a_browser = pytest.mark.skipif(
    not __import__("pathlib").Path(CHROMIUM).exists(),
    reason="no browser on this machine")


class TestTheWordsPeopleUseForControls:
    """Pure logic, so these hold even where a browser can't run."""

    @pytest.mark.parametrize("said,wanted", [
        ("the search bar", "search"),          # nobody says "search box" twice
        ("the settings gear", "settings"),     # the icon is a cog, so it's a gear
        ("the volume slider", "volume"),
        ("the autoplay toggle", "autoplay"),
        ("the emoji picker", "emoji"),
        ("the message input", "message"),
        ("the select all checkbox", "select all"),
        ("the account dropdown", "account"),
    ])
    def test_the_nouns_people_add_are_dropped(self, said, wanted) -> None:
        """Every one of these nouns was absent from the list, and each absence
        meant the whole phrase failed rather than just that word."""
        assert wanted in browser_module.target_variants(said), \
            browser_module.target_variants(said)

    def test_more_than_one_noun_comes_off(self) -> None:
        """It stripped one word and stopped, so "the settings gear icon" kept
        "gear" and matched nothing. Each peeled form is worth trying."""
        variants = browser_module.target_variants("the settings gear icon")
        assert "settings" in variants
        assert variants[0] == "the settings gear icon"

    def test_it_still_refuses_to_loosen_into_nothing(self) -> None:
        for target in ("button", "the", "toggle", "slider", "  ", ""):
            assert all(v.strip() for v in browser_module.target_variants(target)), target

    @pytest.mark.parametrize("said,is_entry", [
        ("the message box", True),
        ("the search bar", True),
        ("the username input", True),
        ("the new message button", False),
        ("the play button", False),
    ])
    def test_it_can_tell_a_text_field_from_a_button(self, said, is_entry) -> None:
        """Slack has a "Message #general" box and a "New message" button. The
        trailing noun is the only thing that says which one was meant."""
        assert browser_module.names_a_text_entry(said) is is_entry

    def test_spacing_and_punctuation_stop_mattering(self) -> None:
        """People say "fullscreen"; the page says "Full screen (f)"."""
        assert browser_module._squash("fullscreen") in browser_module._squash("Full screen (f)")


@needs_a_browser
class TestGmail:
    resolved = _resolves(GMAIL_PAGE)

    @pytest.mark.parametrize("target,landed_on", [
        ("the compose button", "Compose"),
        ("the archive button", "Archive"),
        ("the delete button", "Delete"),
        ("the send button", "Send (Ctrl-Enter)"),
        ("the star icon", "Not starred"),
        ("the select all checkbox", "Select all conversations"),
    ])
    async def test_the_toolbar(self, resolved, target, landed_on) -> None:
        assert await resolved(target) == landed_on

    async def test_a_keyboard_hint_in_the_label_is_not_a_mismatch(self, resolved) -> None:
        """Gmail puts the shortcut inside the accessible name. "Send" is still
        what the user said, and still what they meant."""
        assert await resolved("send") == "Send (Ctrl-Enter)"

    async def test_saying_more_than_the_label_does(self, resolved) -> None:
        """"More options" against "More email options" - no amount of trimming
        the phrase makes either contain the other, but the words line up."""
        assert await resolved("the more options menu") == "More email options"

    @pytest.mark.parametrize("target", ["the search bar", "the search box",
                                        "the Gmail search box", "search mail"])
    async def test_every_way_of_naming_the_search_field(self, resolved, target) -> None:
        """The field is labelled "Search mail", so "the search bar" has a noun
        the page never uses and "the Gmail search box" has a word too many."""
        assert await resolved(target) == "Search mail"


@needs_a_browser
class TestYouTube:
    resolved = _resolves(YOUTUBE_PAGE)

    @pytest.mark.parametrize("target,landed_on", [
        ("the play button", "Play (k)"),
        ("the next button", "Next (SHIFT+n)"),
        ("the mute button", "Mute (m)"),
        ("the settings gear", "Settings"),
        ("the subscribe button", "Subscribe to Daft Punk."),
    ])
    async def test_the_player_controls(self, resolved, target, landed_on) -> None:
        assert await resolved(target) == landed_on

    @pytest.mark.parametrize("target", ["the fullscreen button", "the full screen button",
                                        "fullscreen", "full screen"])
    async def test_fullscreen_is_one_word_or_two(self, resolved, target) -> None:
        """The label is "Full screen (f)". Half the people asking will close
        the gap, and that used to be the half that failed."""
        assert await resolved(target) == "Full screen (f)"

    async def test_a_slider_is_a_control_too(self, resolved) -> None:
        """Volume is a slider, and slider was not a role this looked at, so no
        phrasing at all could reach it."""
        assert await resolved("the volume slider") == "Volume"

    async def test_a_switch_is_a_control_too(self, resolved) -> None:
        assert await resolved("the autoplay toggle") == "Autoplay is on"

    async def test_like_does_not_land_on_dislike(self, resolved) -> None:
        """"Like" is a substring of "Dislike this video". Substring matching
        alone would hand back the opposite of what was asked for; matching on
        a whole word is what makes this right rather than lucky."""
        assert (await resolved("the like button")).startswith("like this video")
        assert await resolved("the dislike button") == "Dislike this video"


@needs_a_browser
class TestSlack:
    resolved = _resolves(SLACK_PAGE)

    @pytest.mark.parametrize("target,landed_on", [
        ("the emoji picker", "Emoji"),
        ("the send button", "Send now"),
        ("the huddle button", "Start huddle"),
        ("the new message button", "New message"),
    ])
    async def test_the_composer_controls(self, resolved, target, landed_on) -> None:
        assert await resolved(target) == landed_on

    @pytest.mark.parametrize("target", ["the message box", "the message input"])
    async def test_the_message_box_is_not_the_new_message_button(self, resolved, target) -> None:
        """Both labels contain "message", and the button came first, so asking
        for the box opened a new message instead of typing in the channel.
        The noun the user chose is what tells the two apart."""
        assert await resolved(target) == "Message #general"

    async def test_search_is_a_button_here_not_a_field(self, resolved) -> None:
        """Slack's search opens a dialog, so the thing to click is a button
        labelled with the workspace name - a word the user will never say."""
        assert await resolved("the search bar") == "Search Acme Corp"

    async def test_it_would_rather_ask_than_click_the_wrong_thing(self, resolved) -> None:
        """"The thread replies" matches "View 3 replies" and "Notify me about
        replies" equally well. A wrong click can't be taken back, so a tie
        reports what's on the page instead of guessing between them."""
        with pytest.raises(browser_module.BrowserError) as raised:
            await resolved("the thread replies")
        assert "replies" in str(raised.value)


@needs_a_browser
class TestMatchingDoesNotDependOnPageOrder:
    """The same words, with the page built the other way round."""

    resolved = _resolves("""
        <button aria-label="Dislike this video">&#128078;</button>
        <button aria-label="like this video along with 12,345 other people">&#128077;</button>
    """)

    async def test_like_still_does_not_land_on_dislike(self, resolved) -> None:
        """With dislike first in the DOM, taking the first substring hit gives
        the wrong button. This is the test that would have caught it."""
        assert (await resolved("the like button")).startswith("like this video")


@needs_a_browser
class TestOneControlIsNeverMistakenForTwo:
    """A combobox is both somewhere you type and something you press, so it
    sits in both role lists. Searching the lists back to back found it twice,
    and two identical scores read as an ambiguous tie - so the one control on
    the page that matched was the one the tie-break threw away."""

    resolved = _resolves("""
        <div role="combobox" aria-label="Filter conversations by label"></div>
        <button aria-label="Compose">&#9998;</button>
    """)

    async def test_a_combobox_resolves_rather_than_tying_with_itself(self, resolved) -> None:
        assert await resolved("the conversations filter dropdown") == \
            "Filter conversations by label"
