"""Opening the things you actually use: your apps, in your browser.

There are two browsers in play and confusing them is what made "open YouTube"
feel broken. The agent drives a Playwright Chromium so it can *read* and *click*
pages on your behalf - a robot window, signed into nothing. Your Chrome is where
you're logged into YouTube, Netflix and Gmail.

So: opening something for you to look at goes to *your* Chrome. The robot
browser is only for when Jarvis needs to read or operate a page itself.

Finding installed apps is the other half. `start spotify` is not a test of
whether Spotify exists - Windows reports success and then pops up "cannot find
the file" where a voice assistant can't see it. So apps are resolved properly:
the Start Menu shortcuts first (which is where everything you've installed
actually is), then registered app paths, then PATH, then protocol handlers.
If none of that finds it, Jarvis says so instead of claiming it worked.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

WINDOWS = sys.platform == "win32"

#: Sites people say by name. The value is what to open.
SITES = {
    "youtube": "https://www.youtube.com",
    "email": "https://mail.google.com",
    "inbox": "https://mail.google.com",
    "bbc": "https://www.bbc.co.uk",
    "netflix": "https://www.netflix.com",
    "spotify": "https://open.spotify.com",
    "google": "https://www.google.com",
    "gmail": "https://mail.google.com",
    "google drive": "https://drive.google.com",
    "drive": "https://drive.google.com",
    "google calendar": "https://calendar.google.com",
    "calendar": "https://calendar.google.com",
    "google docs": "https://docs.google.com",
    "docs": "https://docs.google.com",
    "google sheets": "https://sheets.google.com",
    "sheets": "https://sheets.google.com",
    "maps": "https://maps.google.com",
    "google maps": "https://maps.google.com",
    "whatsapp": "https://web.whatsapp.com",
    "instagram": "https://www.instagram.com",
    "facebook": "https://www.facebook.com",
    "x": "https://x.com",
    "twitter": "https://x.com",
    "tiktok": "https://www.tiktok.com",
    "reddit": "https://www.reddit.com",
    "linkedin": "https://www.linkedin.com",
    "github": "https://github.com",
    "amazon": "https://www.amazon.com",
    "ebay": "https://www.ebay.com",
    "chatgpt": "https://chatgpt.com",
    "claude": "https://claude.ai",
    "wikipedia": "https://www.wikipedia.org",
    "twitch": "https://www.twitch.tv",
    "disney plus": "https://www.disneyplus.com",
    "prime video": "https://www.primevideo.com",
    "outlook": "https://outlook.live.com",
    "news": "https://news.google.com",
}

#: Searching *inside* a site, by URL rather than by driving the page.
#:
#: This is what makes "play something on Spotify" or "find Inception on
#: Netflix" work without a password. The link lands in the browser you are
#: already signed into, at the right place - no automation, nothing for Google
#: to block, and no session for Jarvis to hold.
SEARCHES = {
    "google": "https://www.google.com/search?q={q}",
    "youtube": "https://www.youtube.com/results?search_query={q}",
    "netflix": "https://www.netflix.com/search?q={q}",
    "spotify": "https://open.spotify.com/search/{q}",
    "amazon": "https://www.amazon.com/s?k={q}",
    "gmail": "https://mail.google.com/mail/u/0/#search/{q}",
    "drive": "https://drive.google.com/drive/search?q={q}",
    "google drive": "https://drive.google.com/drive/search?q={q}",
    "maps": "https://www.google.com/maps/search/{q}",
    "google maps": "https://www.google.com/maps/search/{q}",
    "reddit": "https://www.reddit.com/search/?q={q}",
    "github": "https://github.com/search?q={q}",
    "wikipedia": "https://en.wikipedia.org/w/index.php?search={q}",
    "x": "https://x.com/search?q={q}",
    "twitter": "https://x.com/search?q={q}",
    "instagram": "https://www.instagram.com/explore/search/keyword/?q={q}",
    "linkedin": "https://www.linkedin.com/search/results/all/?keywords={q}",
    "ebay": "https://www.ebay.com/sch/i.html?_nkw={q}",
    "twitch": "https://www.twitch.tv/search?term={q}",
    "prime video": "https://www.primevideo.com/search/ref=atv_nb_sr?phrase={q}",
    "disney plus": "https://www.disneyplus.com/search?q={q}",
}


def search_url(site: str, query: str) -> str | None:
    """A link straight to a site's own search results, or None."""
    from urllib.parse import quote

    wanted = (site or "").strip().lower()
    wanted = wanted.removeprefix("my ").removeprefix("the ")
    template = SEARCHES.get(wanted)
    if template is None or not (query or "").strip():
        return None
    return template.format(q=quote(query.strip()))


#: Apps that register a protocol. Faster and more reliable than hunting for an
#: executable, and works for Microsoft Store installs that have no plain .exe.
PROTOCOLS = {
    "spotify": "spotify:",
    "discord": "discord:",
    "steam": "steam:",
    "slack": "slack:",
    "zoom": "zoommtg:",
    "settings": "ms-settings:",
    "camera": "microsoft.windows.camera:",
    "photos": "ms-photos:",
    "store": "ms-windows-store:",
    "mail": "ms-mail:",
}

#: Plain executables that are always present on Windows.
BUILTINS = {
    "notepad": "notepad.exe",
    "calculator": "calc.exe", "calc": "calc.exe",
    "paint": "mspaint.exe",
    "explorer": "explorer.exe", "file explorer": "explorer.exe",
    "files": "explorer.exe",
    "task manager": "taskmgr.exe",
    "command prompt": "cmd.exe", "cmd": "cmd.exe",
    "terminal": "wt.exe",
    "control panel": "control.exe",
}

#: Spoken names that differ from what the shortcut is called.
ALIASES = {
    "chrome": "google chrome",
    "vs code": "visual studio code",
    "vscode": "visual studio code",
    "word": "microsoft word",
    "excel": "microsoft excel",
    "powerpoint": "microsoft powerpoint",
    "teams": "microsoft teams",
    "edge": "microsoft edge",
}


# --------------------------------------------------------------------------- #
# Your browser
# --------------------------------------------------------------------------- #

def browser_profile() -> str:
    """The Chrome profile to open things in - yours, with your logins."""
    configured = ""
    try:
        from jarvis.config import Settings

        configured = str(Settings.load().get("browser.profile", "") or "")
    except Exception:
        pass
    try:
        from chrome_finder import preferred_profile

        return preferred_profile(configured)
    except Exception:
        return configured or "Default"


def chrome_command(chrome: str, *args: str) -> list[str]:
    """Chrome, pointed at the signed-in profile.

    Without --profile-directory Chrome uses whichever profile it feels like,
    which on a machine with more than one is how "open my Google" ends up
    showing somebody else's - or nobody's.
    """
    return [chrome, f"--profile-directory={browser_profile()}", *args]


def open_in_browser(url: str) -> str:
    """Open a URL in the user's own Chrome, where they're signed in."""
    try:
        from chrome_finder import find_chrome
        chrome = find_chrome()
    except Exception:
        chrome = None

    if chrome:
        subprocess.Popen(chrome_command(chrome, url))
        return chrome
    # No Chrome: the default browser is better than refusing.
    import webbrowser

    webbrowser.open(url)
    return "default browser"


#: Verbs and padding the model passes straight through from what was said.
#: "Open my Spotify" used to come back as "that doesn't look like a website" -
#: for the most ordinary request there is - because the whole phrase was being
#: looked up rather than the site inside it.
_SPOKEN_PREFIXES = (
    "open up ", "open ", "go to ", "go on ", "pull up ", "bring up ",
    "launch ", "start ", "show me ", "take me to ", "load ", "visit ",
    "my ", "the ", "a ",
)
_SPOKEN_SUFFIXES = (" please", " for me", " now", " website", " site",
                    " dot com", " page", " up")


#: Everyday words that are things on the computer, not brands. Without these
#: "close the window" would open a web search for the word "window", which is
#: a worse failure than saying it isn't a website.
_NOT_SITES = frozenset({
    "window", "windows", "folder", "file", "files", "settings", "setting",
    "volume", "sound", "computer", "screen", "desktop", "program", "programs",
    "app", "apps", "application", "printer", "terminal", "console", "camera",
    "microphone", "mic", "speaker", "speakers", "bluetooth", "wifi", "menu",
    "taskbar", "clipboard", "recycle", "bin", "trash", "everything", "this",
    "that", "it", "one", "thing", "something", "anything", "here", "there",
})


def _spoken_name(name: str) -> str:
    """What was said, with the speaking taken off."""
    wanted = " ".join((name or "").strip().lower().split())
    changed = True
    while changed:                             # "open up my spotify"
        changed = False
        for prefix in _SPOKEN_PREFIXES:
            if wanted.startswith(prefix):
                wanted, changed = wanted[len(prefix):].strip(), True
                break
    for suffix in _SPOKEN_SUFFIXES:
        if wanted.endswith(suffix):
            wanted = wanted[: -len(suffix)].strip()
            break
    return wanted.strip(" ,.!?")


def site_url(name: str) -> str | None:
    """Turn what they said into a URL, if it looks like a website at all."""
    raw = (name or "").strip()
    if raw.lower().startswith(("http://", "https://")):
        return raw
    wanted = _spoken_name(raw)
    if not wanted:
        return None
    if wanted in SITES:
        return SITES[wanted]
    # "netflix.com", "example.co.uk/thing"
    first = wanted.split("/")[0]
    if "." in first and " " not in first:
        return "https://" + wanted

    # A single unknown word is usually a brand - "shopify", "canva", "notion".
    # Searching for it lands them somewhere useful, which is a far better
    # wrong answer than refusing outright. Guessing brand.com is not worth it:
    # one typo takes them to whoever squatted the domain.
    if wanted.isalnum() and len(wanted) > 2 and wanted not in _NOT_SITES:
        return search_url("google", wanted)
    return None


# --------------------------------------------------------------------------- #
# Your apps
# --------------------------------------------------------------------------- #

def _start_menu_dirs() -> list[Path]:
    roots = []
    for env in ("ProgramData", "APPDATA"):
        base = os.environ.get(env)
        if base:
            roots.append(Path(base) / "Microsoft" / "Windows" / "Start Menu" / "Programs")
    return [r for r in roots if r.exists()]


def find_shortcut(name: str) -> Path | None:
    """Find an installed app's Start Menu shortcut.

    This is the reliable way to find what someone has installed: everything
    with an installer puts a shortcut here, under the name the user would
    actually say, regardless of what the executable is called or where it
    ended up.
    """
    wanted = (name or "").strip().lower()
    if not wanted:
        return None
    wanted = ALIASES.get(wanted, wanted)

    exact: list[Path] = []
    partial: list[Path] = []
    for root in _start_menu_dirs():
        for link in root.rglob("*.lnk"):
            stem = link.stem.lower()
            if stem == wanted:
                exact.append(link)
            elif wanted in stem:
                partial.append(link)
    if exact:
        return exact[0]
    if partial:
        # Shortest name wins: "Spotify" beats "Spotify Web Helper".
        return sorted(partial, key=lambda p: len(p.stem))[0]
    return None


def find_registered_exe(name: str) -> str | None:
    """Ask Windows where an app registered itself (the App Paths key)."""
    if not WINDOWS:
        return None
    import winreg

    candidate = (name or "").strip().lower().replace(" ", "")
    if not candidate.endswith(".exe"):
        candidate += ".exe"
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        try:
            key_path = (r"SOFTWARE\Microsoft\Windows\CurrentVersion"
                        r"\App Paths\\" + candidate)
            with winreg.OpenKey(hive, key_path) as key:
                value, _ = winreg.QueryValueEx(key, "")
                if value and Path(value).exists():
                    return str(value)
        except OSError:
            continue
    return None


def resolve_app(name: str) -> tuple[str, str] | None:
    """Work out how to launch an app. Returns (kind, target) or None.

    Ordered by reliability, not by speed: a shortcut that exists beats a guess
    that might, because the whole point is never to claim success for something
    that didn't happen.
    """
    wanted = (name or "").strip().lower()
    if not wanted:
        return None
    wanted = wanted.removeprefix("my ").removeprefix("the ")

    if wanted in BUILTINS:
        return ("exe", BUILTINS[wanted])

    shortcut = find_shortcut(wanted)
    if shortcut is not None:
        return ("shortcut", str(shortcut))

    registered = find_registered_exe(wanted)
    if registered:
        return ("exe", registered)

    if wanted in PROTOCOLS:
        return ("uri", PROTOCOLS[wanted])

    found = shutil.which(wanted) or shutil.which(wanted.replace(" ", ""))
    if found:
        return ("exe", found)

    return None


def launch(kind: str, target: str) -> None:
    """Actually start it."""
    if kind == "shortcut":
        if WINDOWS:
            os.startfile(target)
        else:
            subprocess.Popen([target])
        return
    if kind == "uri":
        if WINDOWS:
            os.startfile(target)
        else:
            subprocess.Popen(["xdg-open", target])
        return
    subprocess.Popen([target], shell=False)
