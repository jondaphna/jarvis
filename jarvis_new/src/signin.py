"""Open Jarvis's browser so you can sign in to your accounts, once.

Jarvis drives its own browser rather than the one you use day to day, because
Chrome will not let two programs share a profile and yours is always open. That
browser keeps cookies exactly like any other, so signing in here sticks - but it
starts out signed into nothing, which is why "open my Google" showed a
stranger's Google until you do this.

Sign in to whatever you want Jarvis to reach. Close the window when you're done.
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from browser import BrowserManager, jarvis_profile_dir

START_PAGE = "https://accounts.google.com"


async def main() -> int:
    profile = jarvis_profile_dir()
    print(f"  Browser profile: {profile}")
    print("  Opening...\n")

    manager = BrowserManager(headless=False)
    try:
        await manager.start()
    except Exception as exc:
        print(f"  Couldn't open the browser: {exc}")
        print("  Try:  uv run playwright install chromium")
        return 1

    with contextlib.suppress(Exception):
        await manager.open_url(START_PAGE)   # no network yet isn't fatal here

    print("  Sign in to your accounts in that window.")
    print("  Google, Netflix, Spotify, YouTube - whatever you want Jarvis to reach.")
    print("  Say yes to staying signed in.\n")
    print("  Press Enter here when you're finished.")
    with contextlib.suppress(EOFError, KeyboardInterrupt):
        await asyncio.to_thread(input)

    await manager.close()
    print("\n  Saved. Jarvis will stay signed in to those accounts.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
