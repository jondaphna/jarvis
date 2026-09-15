"""Finding Chrome, and finding *your* Chrome profile inside it.

Two separate problems, and the second one is easy to get wrong.

Chrome specifically, not "the default browser": on a stock Windows machine the
default is Edge, the voice client doesn't work there, and the symptom is a
window that opens and does nothing.

And then the profile. An earlier version launched Jarvis with its own
`--user-data-dir`, reasoning that a private profile would keep the microphone
permission tidy. It does - and it is also signed into nothing, so every site it
opened was a stranger's: a logged-out Google, a logged-out Netflix. Your logins
live in your normal Chrome profile, so that is what Jarvis uses. The microphone
gets granted there once, like any other site you trust.

People often have several profiles ("Jonathan", "Work", "Guest"). Chrome keeps
the list in a JSON file, so we can show them by the names you'd recognise and
let you pick, rather than guessing and opening the wrong one.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    str(Path.home() / r"AppData\Local\Google\Chrome\Application\chrome.exe"),
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
]


def find_chrome() -> str | None:
    """The path to Chrome, or None if it isn't installed."""
    for candidate in CANDIDATES:
        if Path(candidate).exists():
            return candidate
    for name in ("chrome", "google-chrome", "chromium", "chromium-browser"):
        found = shutil.which(name)
        if found:
            return found
    return None


def user_data_dir() -> Path | None:
    """Where Chrome keeps your profiles."""
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA")
        if base:
            path = Path(base) / "Google" / "Chrome" / "User Data"
            return path if path.exists() else None
        return None
    if sys.platform == "darwin":
        path = Path.home() / "Library" / "Application Support" / "Google" / "Chrome"
        return path if path.exists() else None
    path = Path.home() / ".config" / "google-chrome"
    return path if path.exists() else None


def list_profiles() -> list[dict[str, str]]:
    """Your Chrome profiles, named the way Chrome names them.

    Returns dicts of {directory, name, email}. The directory is what Chrome
    wants on the command line; the name and email are what you recognise.
    """
    root = user_data_dir()
    if root is None:
        return []
    try:
        state = json.loads((root / "Local State").read_text(encoding="utf-8"))
        cache = state.get("profile", {}).get("info_cache", {})
    except Exception:
        return []

    profiles = []
    for directory, info in cache.items():
        if not (root / directory).exists():
            continue
        profiles.append({
            "directory": directory,
            "name": str(info.get("name") or directory),
            "email": str(info.get("user_name") or ""),
        })
    # Signed-in profiles first: they are almost always the one you meant.
    profiles.sort(key=lambda p: (not p["email"], p["directory"]))
    return profiles


def preferred_profile(configured: str = "") -> str:
    """Which profile to open things in.

    Your explicit choice wins. Otherwise the first signed-in profile, because
    "open my Netflix" means the account that has a Netflix. Only if nothing is
    signed in do we fall back to Chrome's own default.
    """
    profiles = list_profiles()
    names = {p["directory"] for p in profiles}

    if configured and configured in names:
        return configured
    for profile in profiles:
        if profile["email"]:
            return profile["directory"]
    return "Default"
