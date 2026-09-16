from urllib.parse import urlencode

from livekit.agents import RunContext, function_tool
from livekit.agents.llm import ToolError

from browser import BrowserError, BrowserManager


def duckduckgo_search_url(query: str) -> str:
    query = query.strip()
    if not query:
        raise ValueError("The search query cannot be empty.")
    return f"https://duckduckgo.com/?{urlencode({'q': query})}"


class BrowserTools:
    def __init__(self, browser: BrowserManager) -> None:
        self.browser = browser
        self._confirmed_target: str | None = None

    @property
    def tools(self) -> list:
        return [
            self.open_url,
            self.search_on_site,
            self.read_web_page,
            self.inspect_page,
            self.click,
            self.type_text,
            self.page_action,
            self.confirm_browser_action,
            self.search_the_web,
        ]


    @function_tool()
    async def open_url(self, context: RunContext, site: str) -> dict[str, str]:
        """Open a website. THE tool for "open X" - use it every time.

        It opens in the window the user is watching, reusing the tab that is
        already there rather than piling up new ones. Once it is open you can
        read it, type in it and click in it.

        Takes a spoken name as well as a URL: "netflix", "my spotify",
        "youtube", "gmail" all work.

        Call it immediately, without announcing it first.

        Args:
            site: A site name like "netflix", or a full http/https URL.
        """
        import launcher

        url = launcher.site_url(site)
        if url is None:
            raise ToolError(
                f"{site!r} doesn't look like a website. If it's a program on "
                f"this computer, use open_app instead.")
        try:
            return await self.browser.open_url(url)
        except BrowserError as exc:
            raise ToolError(str(exc)) from exc

    @function_tool()
    async def search_on_site(self, context: RunContext, site: str,
                             query: str) -> dict[str, str]:
        """Go straight to a result inside a site - a song, a film, an email.

        Use it for "play daft punk on Spotify", "find Inception on Netflix",
        "search YouTube for X", "google Y", "find that email about Z". It lands
        on the search results rather than the front page, in the same window.

        If the page then needs a click to actually start something, inspect it
        and click - do not stop at the results.

        Args:
            site: "spotify", "netflix", "youtube", "google", "gmail",
                "amazon", "maps" and others.
            query: What to look for.
        """
        import launcher

        url = launcher.search_url(site, query)
        if url is None:
            raise ToolError(
                f"I can't search {site!r} directly. Open it with open_url and "
                f"use its own search box.")
        try:
            return await self.browser.open_url(url)
        except BrowserError as exc:
            raise ToolError(str(exc)) from exc

    @function_tool()
    async def search_the_web(
        self,
        context: RunContext,
        query: str,
    ) -> dict[str, str]:
        """Search the web invisibly, so YOU can answer a question.

        Only for a general lookup with no site named, where the user wants an
        answer rather than a window. If they named a site, use search_on_site;
        if they said "open", use open_url. Read the results before answering.

        Args:
            query: A concise DuckDuckGo search query containing all relevant context.
        """
        try:
            return await self.browser.open_url(duckduckgo_search_url(query))
        except (BrowserError, ValueError) as exc:
            raise ToolError(str(exc)) from exc

    @function_tool()
    async def read_web_page(self, context: RunContext, url: str = "") -> dict:
        """Read a page so you can answer a question about it.

        Give a url to go there first, or leave it out to read the page already
        open. Use this when they want an answer; use open_url when they want to
        look at something themselves.

        Args:
            url: Optional. A site name or full URL to open before reading.
        """
        if url.strip():
            import launcher

            try:
                await self.browser.open_url(launcher.site_url(url) or url)
            except BrowserError as exc:
                raise ToolError(str(exc)) from exc
        try:
            return await self.browser.read_page()
        except BrowserError as exc:
            raise ToolError(str(exc)) from exc

    @function_tool()
    async def page_action(self, context: RunContext, action: str) -> dict:
        """Move around the page you already have open.

        Args:
            action: "back", "up", "down", "enter", "escape", or "tab".
        """
        wanted = (action or "").strip().lower()
        keys = {"enter": "Enter", "escape": "Escape", "tab": "Tab"}
        try:
            if wanted == "back":
                return await self.browser.go_back()
            if wanted in ("up", "down"):
                return await self.browser.scroll(wanted)
            if wanted in keys:
                return await self.browser.press_key(keys[wanted])
        except BrowserError as exc:
            raise ToolError(str(exc)) from exc
        raise ToolError("Say back, up, down, enter, escape or tab.")

    @function_tool()
    async def inspect_page(self, context: RunContext) -> dict[str, object]:
        """Inspect the current page, including readable text and interactive element names.

        Use this before clicking or typing so you can choose a visible control by its
        returned name or role.
        """
        try:
            return await self.browser.inspect_page()
        except BrowserError as exc:
            raise ToolError(str(exc)) from exc

    @function_tool()
    async def click(self, context: RunContext, target: str) -> dict[str, str]:
        """Click a visible control by its accessible name.

        Args:
            target: The visible or accessible name of the control to click.
        """
        if self._requires_confirmation(target):
            if self._confirmed_target != target.casefold():
                raise ToolError(
                    f"This action may be consequential. Ask the user to confirm clicking {target!r} before retrying."
                )
            self._confirmed_target = None

        try:
            return await self.browser.click(target)
        except BrowserError as exc:
            raise ToolError(str(exc)) from exc

    @function_tool()
    async def confirm_browser_action(self, context: RunContext, target: str) -> str:
        """Authorize one previously discussed consequential browser click.

        Call this only after the user explicitly confirms the exact action.

        Args:
            target: The exact accessible name of the control the user approved.
        """
        self._confirmed_target = target.casefold()
        return f"The user confirmed clicking {target!r}."

    @function_tool()
    async def type_text(
        self,
        context: RunContext,
        target: str,
        text: str,
    ) -> dict[str, str]:
        """Fill a visible text field by its label, placeholder, or accessible name.

        Args:
            target: The label, placeholder, or accessible name of the text field.
            text: The text to enter.
        """
        try:
            return await self.browser.type_text(target, text)
        except BrowserError as exc:
            raise ToolError(str(exc)) from exc

    @staticmethod
    def _requires_confirmation(target: str) -> bool:
        risky_words = {
            "buy",
            "confirm",
            "delete",
            "purchase",
            "remove",
            "send",
            "submit",
        }
        return bool(risky_words.intersection(target.casefold().split()))
