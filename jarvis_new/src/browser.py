from __future__ import annotations

import asyncio
import contextlib
import functools
import re
import sys
import time
import uuid
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from playwright.async_api import (
    Locator,
    Page,
)
from playwright.async_api import (
    TimeoutError as PlaywrightTimeoutError,
)

from live_browser import LiveBrowser, LiveBrowserError


class BrowserError(Exception):
    """A user-facing browser operation failure."""


def _focus_window_with_title(title: str, *, timeout_seconds: float = 2.0) -> bool:
    if sys.platform != "win32":
        return False

    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    enum_windows_proc = ctypes.WINFUNCTYPE(
        wintypes.BOOL,
        wintypes.HWND,
        wintypes.LPARAM,
    )

    user32.EnumWindows.argtypes = [enum_windows_proc, wintypes.LPARAM]
    user32.EnumWindows.restype = wintypes.BOOL
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowTextW.restype = ctypes.c_int
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.c_void_p]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
    user32.AttachThreadInput.restype = wintypes.BOOL
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.SetWindowPos.argtypes = [
        wintypes.HWND,
        wintypes.HWND,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.UINT,
    ]
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.SetForegroundWindow.restype = wintypes.BOOL
    kernel32.GetCurrentThreadId.restype = wintypes.DWORD

    def find_window() -> int | None:
        matches: list[int] = []

        @enum_windows_proc
        def collect_window(hwnd: int, _: int) -> bool:
            if not user32.IsWindowVisible(hwnd):
                return True
            length = user32.GetWindowTextLengthW(hwnd)
            if length == 0:
                return True
            buffer = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buffer, length + 1)
            if title in buffer.value:
                matches.append(hwnd)
                return False
            return True

        user32.EnumWindows(collect_window, 0)
        return matches[0] if matches else None

    deadline = time.monotonic() + timeout_seconds
    hwnd = find_window()
    while hwnd is None and time.monotonic() < deadline:
        time.sleep(0.05)
        hwnd = find_window()
    if hwnd is None:
        return False

    foreground = user32.GetForegroundWindow()
    current_thread = kernel32.GetCurrentThreadId()
    foreground_thread = user32.GetWindowThreadProcessId(foreground, None)
    attached = bool(
        foreground_thread
        and foreground_thread != current_thread
        and user32.AttachThreadInput(current_thread, foreground_thread, True)
    )
    try:
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        user32.SetWindowPos(
            hwnd,
            0,
            0,
            0,
            0,
            0,
            0x0001 | 0x0002 | 0x0040,  # NOSIZE | NOMOVE | SHOWWINDOW
        )
        return bool(user32.SetForegroundWindow(hwnd))
    finally:
        if attached:
            user32.AttachThreadInput(current_thread, foreground_thread, False)


async def _bring_page_window_to_front(page: Page) -> None:
    marker = f"Jarvis Browser {uuid.uuid4().hex}"
    original_title = ""
    title_changed = False
    try:
        original_title = await page.title()
        await page.evaluate("title => { document.title = title; }", marker)
        title_changed = True
        await page.bring_to_front()
        await asyncio.to_thread(_focus_window_with_title, marker)
    except Exception:
        # Foreground activation is best-effort and must not prevent browser use.
        return
    finally:
        if title_changed:
            with contextlib.suppress(Exception):
                await page.evaluate(
                    "title => { document.title = title; }",
                    original_title,
                )


def jarvis_profile_dir() -> Path:
    """Where Jarvis's browser keeps your logins.

    A real directory that survives restarts. This is the whole difference
    between "open my Google" and "open a Google": a browser started without one
    is a fresh, signed-out stranger every single time.

    It is deliberately *not* your everyday Chrome profile. Chrome refuses to
    open a profile that another Chrome already has, so pointing at yours would
    fail whenever your browser happened to be open - which is always. You sign
    in here once instead, and it stays signed in.
    """
    try:
        from jarvis import paths

        paths.ensure_dirs()
        root = paths.ROOT / "browser-profile"
    except Exception:
        root = Path.home() / ".jarvis-browser-profile"
    root.mkdir(parents=True, exist_ok=True)
    return root


#: Words a person adds when naming a control out loud that the page never puts
#: in its label. "The play button" is how you say it; "Play Get Lucky by Daft
#: Punk" is how Spotify labels it, and strict matching between those is why the
#: song never started.
_CONTROL_NOUNS = ("button", "link", "icon", "control", "field", "box", "menu",
                  "tab", "option", "item", "thing", "bar", "input", "slider",
                  "toggle", "switch", "gear", "picker", "checkbox", "dropdown",
                  "arrow", "panel", "area", "entry", "textbox", "widget",
                  "tile", "card", "selector")
_LEADING_WORDS = ("the ", "a ", "an ", "that ", "this ", "my ", "its ", "your ")

#: The trailing nouns that mean "somewhere you type" rather than "something you
#: press". Slack calls its composer "Message #general" and also has a "New
#: message" button; "the message box" means the first and "the new message
#: button" means the second, and the only thing telling them apart is the noun.
_ENTRY_NOUNS = frozenset({"box", "bar", "field", "input", "textbox", "area", "entry"})

#: Everything a click can land on. Sliders and switches are here because a
#: volume control and an autoplay toggle are things people ask for by name, and
#: leaving their roles out meant no variant of the phrase could ever match.
_CLICKABLE_ROLES = ("button", "link", "tab", "menuitem", "menuitemcheckbox",
                    "menuitemradio", "checkbox", "radio", "switch", "slider",
                    "option", "combobox", "treeitem")
_ENTRY_ROLES = ("textbox", "searchbox", "combobox")


def _roles_in_order(*groups: tuple[str, ...]) -> tuple[str, ...]:
    """The roles to search, first mention winning and no role listed twice.

    Combobox is both a place you type and a thing you press, so it appears in
    both groups; enumerating it twice would make one control look like two and
    read as an ambiguous tie.
    """
    seen: dict[str, None] = {}
    for group in groups:
        for role in group:
            seen.setdefault(role, None)
    return tuple(seen)

#: A page can carry hundreds of controls and each one read costs a round trip,
#: so both of the scans that read labels stop well before a heavy page would
#: make the assistant sit silent.
_MOST_CONTROLS_WORTH_READING = 200
_MOST_CANDIDATES_WORTH_READING = 25

#: Words too common to identify a control on their own.
_STOPWORDS = frozenset({"the", "a", "an", "to", "of", "and", "or", "in", "on",
                        "for", "with", "by", "at", "is", "it", "all", "my",
                        "this", "that", "your", "me", "about", "from", "new"})


def target_variants(target: str) -> list[str]:
    """The ways to look for this control, most confident first.

    Exactly as said comes first and always wins where it matches; the looser
    forms exist only so that the natural way to say something isn't the one
    thing that fails. Never loosens a target down to nothing - "button" on its
    own stays "button", because matching every button on the page is worse
    than matching none.
    """
    original = " ".join((target or "").split())
    if not original:
        return []

    variants = [original]
    working = original.lower()

    # Peel one word at a time, keeping every step: "the settings gear icon"
    # gives up "icon", then "gear", leaving "settings", and any of the three
    # may be the one the page actually used. Stripping only once, as this did
    # before, meant a single unexpected word sank the whole phrase.
    while True:
        peeled = working
        for word in _LEADING_WORDS:
            if peeled.startswith(word) and peeled[len(word):].strip():
                peeled = peeled[len(word):].strip()
                break
        else:
            for noun in _CONTROL_NOUNS:
                if peeled.endswith(" " + noun) and peeled[: -len(noun)].strip():
                    peeled = peeled[: -len(noun)].strip()
                    break
        if peeled == working:
            break
        working = peeled
        variants.append(working)

    return list(dict.fromkeys(v for v in variants if v.strip()))


def names_a_text_entry(target: str) -> bool:
    """Whether the phrase calls the control somewhere you type.

    "The message box" and "the new message button" name different controls on
    the same Slack screen, and the noun is the only thing that says which.
    """
    words = " ".join((target or "").split()).lower().split()
    return bool(words) and words[-1] in _ENTRY_NOUNS


def _squash(text: str) -> str:
    """Letters and digits only, so spacing and punctuation stop mattering.

    People say "fullscreen"; YouTube labels the control "Full screen (f)".
    Nothing is wrong with either, and squashed they match.
    """
    return "".join(ch for ch in (text or "").lower() if ch.isalnum())


def _content_words(text: str) -> set[str]:
    """The words in a label that could identify it, minus the filler."""
    cleaned = "".join(ch if ch.isalnum() else " " for ch in (text or "").lower())
    return {w for w in cleaned.split() if w and w not in _STOPWORDS}


def _speakable(what: str):
    """Turn any browser failure into something Jarvis can say out loud.

    Only timeouts were being caught. Everything else - above all "Target page,
    context or browser has been closed", which is what you get the moment the
    window is shut - escaped as a raw Playwright error. That sails past
    BrowserTools' `except BrowserError` and surfaces as an unhandled tool
    exception, so one closed window turned into a broken assistant instead of a
    sentence explaining itself.
    """

    def wrap(method):
        @functools.wraps(method)
        async def guarded(*args, **kwargs):
            try:
                return await method(*args, **kwargs)
            except BrowserError:
                raise
            except PlaywrightTimeoutError as exc:
                raise BrowserError(f"{what} took too long.") from exc
            except Exception as exc:
                if "closed" in str(exc).lower():
                    raise BrowserError(
                        "The browser window was closed. Ask me to open "
                        "something and I'll start a new one.") from exc
                raise BrowserError(f"{what} didn't work: {exc}") from exc

        return guarded

    return wrap


class BrowserManager:
    """The window Jarvis works in - the same one you are looking at.

    This used to start a browser of its own. That browser was signed into
    nothing, so anything it opened arrived logged out, and it was a second
    window besides. Now it attaches to the visible Chrome that LiveBrowser
    manages, which means every tool below - clicking, typing, reading, going
    back - acts on the page in front of you, in the tab that is already open.
    """

    def __init__(self, *, headless: bool = False, timeout_ms: int = 15_000,
                 live: LiveBrowser | None = None) -> None:
        # `headless` is kept because the template's tests construct with it.
        # It no longer means anything: the window belongs to the user and is
        # always visible.
        self._timeout_ms = timeout_ms
        self._live = live or LiveBrowser()
        self._page: Page | None = None
        self._lock = asyncio.Lock()

    @property
    def live(self) -> LiveBrowser:
        return self._live

    async def start(self) -> None:
        await self._live.attach()

    async def _get_page(self) -> Page:
        """Whatever tab is in front of the user right now."""
        page = await self._live.page()
        page.set_default_timeout(self._timeout_ms)
        self._page = page
        return page

    async def close(self) -> None:
        """Let go of the window without closing it - it is the user's."""
        async with self._lock:
            await self._live.close()
            self._page = None

    @_speakable('Opening that page')
    async def open_url(self, url: str) -> dict[str, str]:
        """Navigate the tab the user is looking at. Never opens a new one."""
        self._validate_url(url)
        try:
            await self._live.goto(url)
        except LiveBrowserError as exc:
            raise BrowserError(str(exc)) from exc
        except PlaywrightTimeoutError as exc:
            raise BrowserError("The page took too long to load.") from exc
        page = await self._get_page()
        async with self._lock:
            return await self._page_summary(page)

    @_speakable('Reading the page')
    async def read_page(self, *, max_chars: int = 12_000) -> dict[str, str | bool]:
        page = await self._get_page()

        async with self._lock:
            try:
                text = await page.locator("body").inner_text(timeout=self._timeout_ms)
            except PlaywrightTimeoutError as exc:
                raise BrowserError(
                    "The page content was not available in time."
                ) from exc

            text = re.sub(r"\s+", " ", text).strip()
            truncated = len(text) > max_chars
            return {
                "url": page.url,
                "title": await page.title(),
                "text": text[:max_chars],
                "truncated": truncated,
            }

    @_speakable('Looking at the page')
    async def inspect_page(self, *, max_chars: int = 8_000) -> dict[str, object]:
        """Return readable text plus a compact inventory of interactive elements."""
        page = await self._get_page()

        async with self._lock:
            try:
                text = await page.locator("body").inner_text(timeout=self._timeout_ms)
                elements = await page.locator(
                    "button, a, input, textarea, select, [role]"
                ).evaluate_all(
                    """elements => elements
                      .filter(element => {
                        const style = window.getComputedStyle(element);
                        return style.display !== 'none' && style.visibility !== 'hidden';
                      })
                      .slice(0, 80)
                      .map((element, index) => ({
                        index,
                        tag: element.tagName.toLowerCase(),
                        role: element.getAttribute('role') || '',
                        name: (element.getAttribute('aria-label') ||
                          element.getAttribute('title') ||
                          (element.labels && element.labels[0] && element.labels[0].innerText) ||
                          element.getAttribute('placeholder') ||
                          element.innerText || '').trim().replace(/\\s+/g, ' ').slice(0, 120),
                        type: element.getAttribute('type') || '',
                      }))"""
                )
            except PlaywrightTimeoutError as exc:
                raise BrowserError("The page could not be inspected in time.") from exc

            text = re.sub(r"\s+", " ", text).strip()
            return {
                "url": page.url,
                "title": await page.title(),
                "text": text[:max_chars],
                "elements": elements,
            }

    @_speakable('Going back')
    async def go_back(self) -> dict[str, str]:
        page = await self._get_page()

        async with self._lock:
            try:
                await page.go_back(wait_until="domcontentloaded")
                return await self._page_summary(page)
            except PlaywrightTimeoutError as exc:
                raise BrowserError("The previous page took too long to load.") from exc
            except Exception as exc:
                raise BrowserError(f"I could not go back: {exc}") from exc

    @_speakable('Taking a screenshot')
    async def take_screenshot(self) -> dict[str, str | int | bool]:
        page = await self._get_page()

        async with self._lock:
            try:
                image = await page.screenshot(type="png")
                return {
                    "captured": True,
                    "url": page.url,
                    "bytes": len(image),
                }
            except Exception as exc:
                raise BrowserError(f"I could not capture the page: {exc}") from exc

    @_speakable('That click')
    async def click(self, target: str) -> dict[str, str]:
        page = await self._get_page()

        async with self._lock:
            locator = await self._resolve_target(page, target)
            try:
                await locator.click()
            except PlaywrightTimeoutError as exc:
                raise BrowserError(
                    f"The control {target!r} did not become clickable in time."
                ) from exc
            except Exception as exc:
                raise BrowserError(f"I could not click {target!r}: {exc}") from exc

            with contextlib.suppress(PlaywrightTimeoutError):
                await page.wait_for_load_state("domcontentloaded", timeout=5_000)

            return await self._page_summary(page)

    @_speakable('Typing that')
    async def type_text(self, target: str, text: str) -> dict[str, str]:
        page = await self._get_page()

        async with self._lock:
            locator = await self._resolve_textbox(page, target)
            try:
                await locator.fill(text)
            except Exception as exc:
                raise BrowserError(f"I could not type into {target!r}: {exc}") from exc

            return {"target": target, "url": page.url}

    @_speakable('Scrolling')
    async def scroll(self, direction: Literal["up", "down"]) -> dict[str, str]:
        if direction not in {"up", "down"}:
            raise BrowserError("Scroll direction must be 'up' or 'down'.")

        page = await self._get_page()
        amount = -650 if direction == "up" else 650

        async with self._lock:
            await page.mouse.wheel(0, amount)
            return {"direction": direction, "url": page.url}

    @_speakable('That key')
    async def press_key(self, key: str) -> dict[str, str]:
        allowed_keys = {
            "Enter",
            "Escape",
            "Tab",
            "ArrowDown",
            "ArrowLeft",
            "ArrowRight",
            "ArrowUp",
            "Backspace",
        }
        if key not in allowed_keys:
            raise BrowserError("That keyboard key is not allowed.")

        page = await self._get_page()
        async with self._lock:
            await page.keyboard.press(key)
            return {"key": key, "url": page.url}

    @staticmethod
    def _validate_url(url: str) -> None:
        parsed = urlparse(url.strip())
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise BrowserError("Only complete http or https URLs can be opened.")
        if parsed.username or parsed.password:
            raise BrowserError("URLs containing embedded credentials are not allowed.")

    @staticmethod
    async def _page_summary(page: Page) -> dict[str, str]:
        return {"url": page.url, "title": await page.title()}

    async def _resolve_target(self, page: Page, target: str) -> Locator:
        """The control the user meant, matched as loosely as it safely can be.

        Tried in order of confidence: the words as given, then the same words
        with the padding a person adds when speaking taken off. Exact always
        wins where there is one - loosening is a fallback, not a replacement.
        """
        roles = _CLICKABLE_ROLES
        if names_a_text_entry(target):
            # "The message box" is the composer, not the "New message" button
            # sitting next to it, so text entries get looked at first.
            roles = _roles_in_order(_ENTRY_ROLES, _CLICKABLE_ROLES)

        for wanted in target_variants(target):
            # Exactness before role: an exact match anywhere on the page beats
            # a loose match on whichever role happened to be checked first.
            for exact in (True, False):
                for role in roles:
                    locator = page.get_by_role(role, name=wanted, exact=exact)
                    count = await locator.count()
                    if count:
                        return await self._best_of(locator, count, wanted)

            for exact in (True, False):
                locator = page.get_by_text(wanted, exact=exact)
                if await locator.count():
                    return locator.first

        loose = await self._loosely_named(page, target)
        if loose is not None:
            return loose

        raise BrowserError(await self._nothing_named(page, target))

    @staticmethod
    async def _best_of(locator: Locator, count: int, wanted: str) -> Locator:
        """Of several controls containing the words, the one that means them.

        "Like" is inside "Dislike this video", so a plain substring match can
        hand back the opposite of what was asked for. A match on a whole word
        is the one a person meant.
        """
        if count == 1:
            return locator.first
        wanted_words = _content_words(wanted)
        if not wanted_words:
            return locator.first
        for index in range(min(count, _MOST_CANDIDATES_WORTH_READING)):
            candidate = locator.nth(index)
            try:
                label = await candidate.get_attribute("aria-label")
                if not label:
                    label = await candidate.inner_text()
            except Exception:
                continue
            if wanted_words <= _content_words(label):
                return candidate
        return locator.first

    async def _controls(self, page: Page) -> list[tuple[Locator, str]]:
        """Every control on the page with the name a screen reader would read."""
        found: list[tuple[Locator, str]] = []
        for role in _roles_in_order(_ENTRY_ROLES, _CLICKABLE_ROLES):
            if len(found) >= _MOST_CONTROLS_WORTH_READING:
                break
            for handle in await page.get_by_role(role).all():
                if len(found) >= _MOST_CONTROLS_WORTH_READING:
                    break
                try:
                    label = await handle.get_attribute("aria-label")
                    if not label:
                        label = await handle.inner_text()
                except Exception:
                    continue
                label = " ".join((label or "").split())
                if label:
                    found.append((handle, label))
        return found

    async def _loosely_named(self, page: Page, target: str) -> Locator | None:
        """The last resort: the page said it differently than the user did.

        Two things get past strict matching here. Spacing and punctuation, so
        "fullscreen" finds "Full screen (f)". And saying more than the label
        does - "the more options menu" against Gmail's "More email options" -
        where no amount of trimming the phrase makes one contain the other, but
        the words plainly line up.

        Only ever returns an unambiguous winner. Clicking the wrong control is
        worse than admitting the control wasn't found, so a tie returns
        nothing and the caller reports what IS on the page instead.
        """
        try:
            controls = await self._controls(page)
        except Exception:
            return None
        if not controls:
            return None

        variants = target_variants(target)
        squashed = [_squash(v) for v in variants]
        said = _content_words(variants[-1]) if variants else set()

        scored: list[tuple[float, Locator]] = []
        for handle, label in controls:
            label_squashed = _squash(label)
            score = 0.0
            for form in squashed:
                if form and (form in label_squashed or label_squashed in form):
                    # Longer agreement is stronger evidence.
                    score = max(score, 2.0 + len(form) / max(len(label_squashed), 1))
            if not score and said:
                shared = said & _content_words(label)
                if len(shared) >= 2 or any(len(w) >= 5 for w in shared):
                    score = len(shared)
            if score:
                scored.append((score, handle))

        if not scored:
            return None
        scored.sort(key=lambda pair: pair[0], reverse=True)
        if len(scored) > 1 and scored[0][0] == scored[1][0]:
            return None
        return scored[0][1]

    async def _nothing_named(self, page: Page, target: str) -> str:
        """Why the click failed, and what it could have clicked instead.

        A bare "I could not find it" leaves the model guessing blind, and what
        it guesses next is usually worse. Naming what IS on the page lets it
        pick something and carry on.
        """
        message = f"There's no control named {target!r} on this page."
        try:
            names: list[str] = []
            for role in ("button", "link"):
                for handle in await page.get_by_role(role).all():
                    label = ((await handle.get_attribute("aria-label"))
                             or (await handle.inner_text()) or "").strip()
                    label = " ".join(label.split())[:40]
                    if label and label not in names:
                        names.append(label)
                    if len(names) >= 12:
                        break
                if len(names) >= 12:
                    break
        except Exception:
            return message
        if not names:
            return message
        return message + " What's there: " + ", ".join(repr(n) for n in names)

    async def _resolve_textbox(self, page: Page, target: str) -> Locator:
        for wanted in target_variants(target):
            for locator in (
                page.get_by_role("textbox", name=wanted, exact=True),
                page.get_by_role("textbox", name=wanted, exact=False),
                page.get_by_label(wanted, exact=True),
                page.get_by_label(wanted, exact=False),
                page.get_by_placeholder(wanted, exact=True),
                page.get_by_placeholder(wanted, exact=False),
            ):
                if await locator.count():
                    return locator.first

        normalized_target = target.casefold()
        if any(
            word in normalized_target for word in ("search", "query", "input", "text")
        ):
            for selector in (
                "input[type='search']:visible",
                "input[aria-label*='search' i]:visible",
                "input[placeholder*='search' i]:visible",
                "textarea:visible",
                "input:visible",
            ):
                locator = page.locator(selector)
                if await locator.count():
                    return locator.first

        raise BrowserError(f"I could not find a text field named {target!r}.")
