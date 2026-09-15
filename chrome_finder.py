"""Where Chrome lives.

Its own module with no dependencies, so the orb (which needs PyQt6) and the
doctor (which must run even when PyQt6 isn't installed) can both use it.

Chrome specifically, not "the default browser": on a stock Windows machine the
default is Edge, the voice client doesn't work there, and the symptom is a
window that opens and does nothing.
"""

from __future__ import annotations

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


def chrome_profile(root: Path | None = None) -> Path:
    """A profile of its own, so the microphone permission you grant Jarvis
    sticks and doesn't depend on your everyday Chrome window."""
    if root is None:
        try:
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            from jarvis import paths

            paths.ensure_dirs()
            root = paths.ROOT / "chrome-profile"
        except Exception:
            root = Path(__file__).resolve().parent / ".chrome-profile"
    root.mkdir(parents=True, exist_ok=True)
    return root
