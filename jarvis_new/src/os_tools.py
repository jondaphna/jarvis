"""Letting Jarvis work your actual computer: apps, volume, music, power.

Everything here runs as you, with your privileges, so the line between "handy"
and "regrettable" is drawn deliberately:

* **Opening things and controlling media** happen immediately. Opening Spotify
  or skipping a track is not worth a conversation.
* **Power actions** - shutting down, restarting, sleeping - always need you to
  say yes first, in the same turn. Losing unsaved work because a sentence was
  misheard is exactly the kind of thing that destroys trust in an assistant.
* **Arbitrary commands** are not offered at all. There is no run_shell tool
  here, because a voice assistant that can be talked into running any command
  is a remote code execution bug with a personality.

Volume and media use the keyboard's own media keys through ctypes rather than
a library, which means no extra dependency and it controls whatever is actually
playing - Spotify, YouTube, a game - instead of only one app.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from livekit.agents import RunContext, function_tool
from livekit.agents.llm import ToolError

import launcher

WINDOWS = sys.platform == "win32"

#: Virtual key codes for the media keys every Windows keyboard reports.
_KEYS = {
    "mute": 0xAD, "volume_down": 0xAE, "volume_up": 0xAF,
    "next": 0xB0, "previous": 0xB1, "stop": 0xB2, "play_pause": 0xB3,
}


def _press(vk: int, times: int = 1) -> None:
    """Tap a virtual key. Windows only; a no-op elsewhere."""
    if not WINDOWS:
        raise ToolError("That only works on Windows.")
    import ctypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    for _ in range(max(1, times)):
        user32.keybd_event(vk, 0, 0, 0)        # down
        user32.keybd_event(vk, 0, 2, 0)        # up


class OSTools:
    """Opening things, volume, music, and a guarded set of power actions."""

    def __init__(self) -> None:
        #: Set by confirm_power_action, cleared the moment it is used, so a
        #: yes authorises exactly one shutdown and never the next one.
        self._confirmed: str | None = None

    @property
    def tools(self) -> list:
        return [
            self.open_url,
            self.search_on_site,
            self.open_app,
            self.open_folder,
            self.set_volume,
            self.control_music,
            self.system_status,
            self.lock_screen,
            self.power_action,
            self.confirm_power_action,
        ]

    # ------------------------------------------------------------------ #
    # Opening things
    # ------------------------------------------------------------------ #

    @function_tool()
    async def open_url(self, context: RunContext, site: str) -> str:
        """Open a website for the user. THE tool for "open X" - use it always.

        This opens their own Chrome, already signed into their own accounts, so
        "open my Google" is their Google and "open my Netflix" is their Netflix.
        Nothing to log into and nothing to set up.

        Takes a plain spoken name as well as a URL: "google", "my netflix",
        "youtube", "gmail" all work.

        Do NOT use fetch_page for this. That one loads pages invisibly for you
        to read; the user cannot see it and it is signed into nothing.

        Call it immediately, without announcing it first.

        Args:
            site: A site name like "netflix", or a full http/https URL.
        """
        url = launcher.site_url(site)
        if url is None:
            raise ToolError(
                f"{site!r} doesn't look like a website. If it's a program on "
                f"this computer, use open_app instead.")
        try:
            launcher.open_in_browser(url)
        except Exception as exc:
            raise ToolError(f"Couldn't open {site}: {exc}") from exc
        return f"Opened {url}."

    @function_tool()
    async def search_on_site(self, context: RunContext, site: str,
                             query: str) -> str:
        """Search inside a site, in the user's own browser.

        This is how you play music, find a film, or look something up in their
        own account: it opens the site's own search results directly, already
        signed in as them.

        Use it for "play daft punk on Spotify", "find Inception on Netflix",
        "search YouTube for X", "google Y", "find that email about Z".

        Prefer this over search_the_web whenever they name a site. Call it
        immediately, without announcing it first.

        Args:
            site: Which site - "spotify", "netflix", "youtube", "google",
                "gmail", "amazon", "maps" and others are supported.
            query: What to look for.
        """
        url = launcher.search_url(site, query)
        if url is None:
            raise ToolError(
                f"I can't search {site!r} directly. Open it with open_url, or "
                f"use search_the_web for a general search.")
        try:
            launcher.open_in_browser(url)
        except Exception as exc:
            raise ToolError(f"Couldn't search {site}: {exc}") from exc
        return f"Opened {site} search for {query!r}."

    @function_tool()
    async def open_app(self, context: RunContext, name: str) -> str:
        """Open a program installed on this computer.

        Spotify, Word, Discord, Task Manager. If they name a *website*, use
        open_website instead.

        Call it immediately, without announcing it first.

        Args:
            name: What they called it, like "spotify" or "task manager".
        """
        wanted = (name or "").strip()
        if not wanted:
            raise ToolError("Which application?")

        if not WINDOWS:
            raise ToolError("Opening applications only works on Windows.")

        found = launcher.resolve_app(wanted)
        if found is None:
            # Netflix and the like are websites, not programs. Say so, and let
            # the model open it properly - going to the browser from here would
            # use a different, signed-out one.
            if launcher.site_url(wanted):
                raise ToolError(
                    f"{name} isn't a program on this computer - it's a website. "
                    f"Use open_url instead.")
            raise ToolError(
                f"I can't find {name} on this computer. Tell me the exact name "
                f"it has in the Start menu and I'll use that.")

        kind, target = found
        try:
            launcher.launch(kind, target)
        except Exception as exc:
            raise ToolError(f"Couldn't open {name}: {exc}") from exc
        return f"Opened {name}."

    @function_tool()
    async def open_folder(self, context: RunContext, path: str) -> str:
        """Open a folder in the file manager.

        Args:
            path: A folder path, or a shortcut like "downloads" or "desktop".
        """
        shortcuts = {
            "downloads": Path.home() / "Downloads",
            "desktop": Path.home() / "Desktop",
            "documents": Path.home() / "Documents",
            "pictures": Path.home() / "Pictures",
            "music": Path.home() / "Music",
            "videos": Path.home() / "Videos",
            "home": Path.home(),
        }
        wanted = (path or "").strip()
        target = shortcuts.get(wanted.lower(), Path(wanted).expanduser())
        if not target.exists():
            raise ToolError(f"There's no folder at {target}.")
        try:
            if WINDOWS:
                os.startfile(str(target))
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(target)])
            else:
                subprocess.Popen(["xdg-open", str(target)])
        except Exception as exc:
            raise ToolError(f"Couldn't open that folder: {exc}") from exc
        return f"Opened {target}."

    # ------------------------------------------------------------------ #
    # Sound
    # ------------------------------------------------------------------ #

    @function_tool()
    async def set_volume(self, context: RunContext, direction: str) -> str:
        """Change the system volume.

        Args:
            direction: "up", "down", "mute", or "unmute" (the same as mute).
        """
        wanted = (direction or "").strip().lower()
        if wanted in ("mute", "unmute", "toggle"):
            _press(_KEYS["mute"])
            return "Muted." if wanted == "mute" else "Toggled the mute."
        if wanted in ("up", "louder", "increase"):
            _press(_KEYS["volume_up"], times=4)     # each tap is ~2%
            return "Turned it up."
        if wanted in ("down", "quieter", "lower", "decrease"):
            _press(_KEYS["volume_down"], times=4)
            return "Turned it down."
        raise ToolError("Say up, down, or mute.")

    @function_tool()
    async def control_music(self, context: RunContext, action: str) -> str:
        """Control whatever is playing - Spotify, YouTube, anything.

        This uses the keyboard's media keys, so it works with whichever player
        currently has the audio, without needing to know which one that is.

        Args:
            action: "play", "pause", "next", "previous", or "stop".
        """
        wanted = (action or "").strip().lower()
        mapping = {
            "play": "play_pause", "pause": "play_pause", "resume": "play_pause",
            "toggle": "play_pause", "next": "next", "skip": "next",
            "previous": "previous", "back": "previous", "stop": "stop",
        }
        key = mapping.get(wanted)
        if key is None:
            raise ToolError("Say play, pause, next, previous or stop.")
        _press(_KEYS[key])
        return f"{wanted.capitalize()}."

    # ------------------------------------------------------------------ #
    # How the machine is doing
    # ------------------------------------------------------------------ #

    @function_tool()
    async def system_status(self, context: RunContext) -> str:
        """How this computer is doing: processor, memory and disk.

        Summarise it in a sentence rather than reading the numbers out.
        """
        try:
            import psutil
        except ImportError:
            return ("Detailed stats need the psutil package, which isn't "
                    "installed. Everything else still works.")

        cpu = psutil.cpu_percent(interval=0.4)
        memory = psutil.virtual_memory()
        disk = psutil.disk_usage(str(Path.home().anchor or "/"))
        parts = [
            f"processor {cpu:.0f} percent",
            f"memory {memory.percent:.0f} percent used "
            f"({memory.available / 1e9:.1f} gigabytes free)",
            f"disk {disk.percent:.0f} percent full "
            f"({disk.free / 1e9:.0f} gigabytes free)",
        ]
        try:
            battery = psutil.sensors_battery()
            if battery is not None:
                plugged = "charging" if battery.power_plugged else "on battery"
                parts.append(f"battery {battery.percent:.0f} percent, {plugged}")
        except Exception:
            pass
        return "; ".join(parts)

    # ------------------------------------------------------------------ #
    # Power - the part that needs asking
    # ------------------------------------------------------------------ #

    @function_tool()
    async def lock_screen(self, context: RunContext) -> str:
        """Lock the screen. Safe - nothing is closed and nothing is lost."""
        if not WINDOWS:
            raise ToolError("That only works on Windows.")
        subprocess.Popen(["rundll32.exe", "user32.dll,LockWorkStation"])
        return "Locked."

    @function_tool()
    async def power_action(self, context: RunContext, action: str) -> str:
        """Shut down, restart or sleep the computer.

        This will not work until the user has confirmed out loud and you have
        called confirm_power_action. Tell them what it will do, note that
        unsaved work will be lost, and wait for a clear yes.

        Args:
            action: "shutdown", "restart", or "sleep".
        """
        wanted = (action or "").strip().lower()
        if wanted not in ("shutdown", "restart", "sleep"):
            raise ToolError("Say shutdown, restart or sleep.")
        if self._confirmed != wanted:
            raise ToolError(
                f"Not confirmed. Ask them to confirm you should {wanted} the "
                f"computer, mention unsaved work will be lost, and only then "
                f"call confirm_power_action.")
        self._confirmed = None                # one yes, one action

        if not WINDOWS:
            raise ToolError("That only works on Windows.")
        commands = {
            "shutdown": ["shutdown", "/s", "/t", "20"],
            "restart": ["shutdown", "/r", "/t", "20"],
            "sleep": ["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"],
        }
        subprocess.Popen(commands[wanted])
        if wanted == "sleep":
            return "Going to sleep."
        return (f"{wanted.capitalize()} in twenty seconds. Say cancel and I'll "
                f"stop it.")

    @function_tool()
    async def confirm_power_action(self, context: RunContext, action: str) -> str:
        """Record that the user confirmed a shutdown, restart or sleep.

        Only call this after they have clearly said yes to that exact action.

        Args:
            action: The action they agreed to: "shutdown", "restart" or "sleep".
        """
        wanted = (action or "").strip().lower()
        if wanted not in ("shutdown", "restart", "sleep"):
            raise ToolError("Say shutdown, restart or sleep.")
        self._confirmed = wanted
        return f"Confirmed. Now call power_action with {wanted}."
