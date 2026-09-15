"""The one browser Jarvis both shows you and controls.

Every previous attempt traded one half away.

* Your everyday Chrome is signed into everything and cannot be driven: Chrome
  refuses remote debugging on its default profile, deliberately, and only one
  Chrome may hold a profile at a time.
* A browser Playwright *launches* can be driven and is signed into nothing -
  and Google blocks signing in from one on purpose, so it can never be fixed
  from the inside.

The way out is to stop letting Playwright start the browser. Jarvis launches an
ordinary Chrome - its own window, its own profile, no automation switches - and
then attaches to it afterwards over the DevTools protocol. Ordinary Chrome is
what sign-in pages see, so logging in there works normally. Attaching afterwards
is what lets Jarvis read the page, click, and type.

One window, reused. Asking for Spotify and then asking for a song happens in the
same tab, the way it would if you did it yourself.
"""

from __future__ import annotations

import asyncio
import contextlib
import shutil
import socket
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

DEBUG_PORT = 9222


class LiveBrowserError(RuntimeError):
    """Something about the visible browser went wrong, in words."""


def profile_dir() -> Path:
    """Where Jarvis's Chrome window keeps its own logins and history."""
    try:
        from jarvis import paths

        paths.ensure_dirs()
        root = paths.ROOT / "chrome"
    except Exception:
        root = Path.home() / ".jarvis-chrome"
    root.mkdir(parents=True, exist_ok=True)
    return root


def port_is_open(port: int = DEBUG_PORT) -> bool:
    with socket.socket() as probe:
        probe.settimeout(0.4)
        return probe.connect_ex(("127.0.0.1", port)) == 0


#: What gets copied from your Chrome so Jarvis starts signed in - and, more
#: importantly, what does not.
#:
#: Sessions only. Chrome's "Login Data" file is its saved-password database and
#: "Web Data" holds autofill - addresses, card numbers. Neither is needed to
#: stay signed in to a site, and copying them would hand Jarvis's browser the
#: ability to fill in your passwords and card details, which is far more than
#: "open my Netflix" asks for. They are named here so it is obvious they were
#: considered and left out on purpose.
SESSION_FILES = ("Cookies", "Preferences")
SESSION_FOLDERS = ("Local Storage", "Network")
NEVER_COPIED = ("Login Data", "Web Data", "History", "Bookmarks")


def seeding_wanted() -> bool:
    """Whether to copy sessions across. On unless you turn it off."""
    try:
        from jarvis.config import Settings

        return bool(Settings.load().get("browser.copy_sessions", True))
    except Exception:
        return True


def seed_from_your_chrome(target: Path) -> bool:
    """Copy your *sessions* across once, so you start already signed in.

    Cookies and site storage only - never saved passwords, autofill, history or
    bookmarks. See SESSION_FILES and NEVER_COPIED above.

    Best effort, and never fatal. Chrome encrypts cookies with a key tied to
    your Windows account, so a copy made by you, for you, on the same machine
    still opens. If anything about that is not true it simply fails and you
    sign in once instead.

    Only ever reads from your profile. Nothing is written back to it.
    """
    if not seeding_wanted():
        return False
    try:
        from chrome_finder import list_profiles, preferred_profile, user_data_dir

        source_root = user_data_dir()
        if source_root is None:
            return False
        chosen = preferred_profile("")
        source = source_root / chosen
        if not source.exists() or not list_profiles():
            return False

        destination = target / "Default"
        destination.mkdir(parents=True, exist_ok=True)
        if (destination / "Cookies").exists():
            return False                     # already seeded; don't clobber

        # The key lives outside the profile folder, so it has to come too or
        # the cookies are undecryptable noise.
        if (source_root / "Local State").exists():
            shutil.copy2(source_root / "Local State", target / "Local State")

        copied = False
        for name in SESSION_FILES:
            candidate = source / name
            if candidate.exists():
                shutil.copy2(candidate, destination / name)
                copied = True
        for folder in SESSION_FOLDERS:
            candidate = source / folder
            if candidate.is_dir():
                shutil.copytree(candidate, destination / folder,
                                dirs_exist_ok=True)
                copied = True
        return copied
    except Exception:
        return False


class LiveBrowser:
    """A visible Chrome window that Jarvis can also drive."""

    def __init__(self, port: int = DEBUG_PORT,
                 chrome_path: str | None = None,
                 profile: Path | None = None,
                 extra_args: list[str] | None = None) -> None:
        self.port = port
        self.profile = profile or profile_dir()
        self._chrome_path = chrome_path
        self._extra_args = extra_args or []
        self._process: subprocess.Popen | None = None
        self._playwright = None
        self._context = None
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ #
    # Starting and attaching
    # ------------------------------------------------------------------ #

    def chrome(self) -> str:
        if self._chrome_path:
            return self._chrome_path
        from chrome_finder import find_chrome

        found = find_chrome()
        if not found:
            raise LiveBrowserError(
                "Chrome isn't installed. Jarvis needs it to open and control "
                "your sites - get it from google.com/chrome.")
        return found

    def launch(self) -> None:
        """Start an ordinary Chrome window with debugging switched on.

        Deliberately *not* started by Playwright. A browser Playwright launches
        announces itself as automated, and sign-in pages refuse it. This one is
        a normal Chrome that happens to be listening on a local port.
        """
        if port_is_open(self.port):
            return

        seed_from_your_chrome(self.profile)

        args = [
            self.chrome(),
            f"--remote-debugging-port={self.port}",
            # A profile of its own is not a preference: Chrome ignores the
            # debugging port entirely on the default one.
            f"--user-data-dir={self.profile}",
            "--no-first-run",
            "--no-default-browser-check",
            "--restore-last-session",
            *self._extra_args,
        ]
        self._process = subprocess.Popen(args)

    async def attach(self, timeout: float = 25.0):
        """Connect to that window. Starts it first if it isn't up."""
        if self._context is not None:
            return self._context

        from playwright.async_api import async_playwright

        self.launch()

        deadline = asyncio.get_event_loop().time() + timeout
        while not port_is_open(self.port):
            if asyncio.get_event_loop().time() > deadline:
                raise LiveBrowserError(
                    "Chrome didn't start in time. Close any Jarvis Chrome "
                    "windows and try again.")
            await asyncio.sleep(0.3)

        if self._playwright is None:
            self._playwright = await async_playwright().start()
        browser = await self._playwright.chromium.connect_over_cdp(
            f"http://127.0.0.1:{self.port}")
        self._context = (browser.contexts[0] if browser.contexts
                         else await browser.new_context())
        return self._context

    # ------------------------------------------------------------------ #
    # The tab you are looking at
    # ------------------------------------------------------------------ #

    async def page(self):
        """The current tab - reused, not replaced.

        Opening a site and then acting on it should happen in one place. Making
        a new tab each time is how "go to Spotify" then "play something" ended
        up as two Spotifys.
        """
        context = await self.attach()
        pages = [p for p in context.pages if not p.is_closed()]
        if not pages:
            return await context.new_page()
        # The last one is the one most recently used, which is the one the
        # person is looking at.
        page = pages[-1]
        with contextlib.suppress(Exception):
            await page.bring_to_front()
        return page

    async def goto(self, url: str, *, new_tab: bool = False) -> dict[str, str]:
        """Navigate. Same tab unless you explicitly ask for another."""
        async with self._lock:
            context = await self.attach()
            page = await context.new_page() if new_tab else await self.page()
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            except Exception as exc:
                raise LiveBrowserError(f"Couldn't open that page: {exc}") from exc
            with contextlib.suppress(Exception):
                await page.bring_to_front()
            return {"url": page.url, "title": await page.title()}

    async def close(self) -> None:
        """Detach. The window stays open - it is the user's."""
        with contextlib.suppress(Exception):
            if self._playwright is not None:
                await self._playwright.stop()
        self._playwright = None
        self._context = None
