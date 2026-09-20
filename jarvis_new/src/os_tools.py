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

import asyncio
import subprocess
import sys
import threading
import time
from pathlib import Path

from livekit.agents import RunContext, function_tool
from livekit.agents.llm import ToolError

import launcher

#: How long a machine reading stays good enough to answer from. Processor load
#: over the last few seconds is what the question means, so serving a reading
#: taken moments ago is not a stale answer, it is the same answer without the
#: wait.
TELEMETRY_TTL = 5.0

#: How long the sampler spends measuring processor load. `psutil` needs a real
#: interval - it compares two readings - and anything shorter than this is
#: mostly noise.
TELEMETRY_INTERVAL = 0.4

_telemetry_lock = threading.Lock()
_telemetry: tuple[float, str] | None = None


def _sample_machine() -> str:
    """Read the machine and describe it. Blocks; never call this on a loop."""
    import psutil

    cpu = psutil.cpu_percent(interval=TELEMETRY_INTERVAL)
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


def _cached_machine() -> str | None:
    """The last reading, if it is recent enough to still be the answer."""
    with _telemetry_lock:
        if _telemetry is None:
            return None
        taken, reading = _telemetry
    return reading if time.monotonic() - taken < TELEMETRY_TTL else None


def _store_machine(reading: str) -> None:
    global _telemetry

    with _telemetry_lock:
        _telemetry = (time.monotonic(), reading)


def reset_telemetry_cache() -> None:
    """Forget the cached reading. For tests, and for a deliberate refresh."""
    global _telemetry

    with _telemetry_lock:
        _telemetry = None

WINDOWS = sys.platform == "win32"

#: Windows API constants. Uppercase because that is what they are called in
#: every piece of Windows documentation you will check this against.
WM_CLOSE = 0x0010
SW_MINIMIZE = 6
SW_RESTORE = 9
VK_WINDOWS = 0x5B
VK_D = 0x44
KEY_UP = 2

#: Virtual key codes for the media keys every Windows keyboard reports.
_KEYS = {
    "mute": 0xAD, "volume_down": 0xAE, "volume_up": 0xAF,
    "next": 0xB0, "previous": 0xB1, "stop": 0xB2, "play_pause": 0xB3,
}


def _window_action(what: str, name: str) -> bool:
    """Close, focus or minimise a window. True if one was found.

    Closing sends WM_CLOSE - the same message the X button sends - rather than
    killing the process, so an unsaved document prompts instead of vanishing.
    """
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)

    matches: list[int] = []
    wanted = name.lower()

    proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def visit(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buffer, length + 1)
        if not wanted or wanted in buffer.value.lower():
            matches.append(hwnd)
        return True

    if name:
        user32.EnumWindows(proc(visit), 0)
        if not matches:
            return False
        handle = matches[0]
    else:
        handle = user32.GetForegroundWindow()
        if not handle:
            return False

    if what == "close":
        user32.PostMessageW(handle, WM_CLOSE, 0, 0)
    elif what == "minimise":
        user32.ShowWindow(handle, SW_MINIMIZE)
    else:
        user32.ShowWindow(handle, SW_RESTORE)
        user32.SetForegroundWindow(handle)
    return True


def _show_desktop() -> None:
    """Win+D. A combination, so the Windows key has to be held down while D is
    pressed - tapping it on its own just opens the Start menu."""
    import ctypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.keybd_event(VK_WINDOWS, 0, 0, 0)
    user32.keybd_event(VK_D, 0, 0, 0)
    user32.keybd_event(VK_D, 0, KEY_UP, 0)
    user32.keybd_event(VK_WINDOWS, 0, KEY_UP, 0)


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
            self.open_app,
            self.window_action,
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
    async def window_action(self, context: RunContext, action: str,
                            name: str = "") -> str:
        """Close, focus or minimise a window on screen.

        Use this when they say close, shut, switch to, bring up, minimise or
        hide something. With no name it acts on whatever is in front.

        Closing asks the window to close the way clicking its X does, so
        anything with unsaved work will prompt them rather than lose it.

        Args:
            action: "close", "focus", "minimise", or "minimise all".
            name: Part of the window or program name, like "spotify" or
                "chrome". Leave empty for the window in front.
        """
        if not WINDOWS:
            raise ToolError("Managing windows only works on Windows.")

        wanted = (action or "").strip().lower().replace("minimize", "minimise")
        target = (name or "").strip()

        if wanted in ("minimise all", "minimise everything", "show desktop"):
            _show_desktop()
            return "Minimised everything."

        if wanted not in ("close", "focus", "minimise"):
            raise ToolError("Say close, focus, minimise, or minimise all.")

        try:
            found = _window_action(wanted, target)
        except Exception as exc:
            raise ToolError(f"Couldn't {wanted} that window: {exc}") from exc

        if not found:
            raise ToolError(
                f"I can't find a window called {target!r}. Tell me what it says "
                f"in the title bar.")
        where = target or "the front window"
        return {"close": f"Closed {where}.", "focus": f"Switched to {where}.",
                "minimise": f"Minimised {where}."}[wanted]

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

        Two things happen here that are not obvious from the result. The
        reading is taken on a thread, because `psutil.cpu_percent` with an
        interval *blocks for that interval* - measured at 400 milliseconds,
        taken straight out of the audio loop, which is long enough to hear.
        And a recent reading is reused rather than retaken, because "how is
        the machine doing" means the last few seconds either way, so a second
        ask answers instantly instead of stalling for another sample.
        """
        try:
            import psutil  # noqa: F401  - checked here so the message is kind
        except ImportError:
            return ("Detailed stats need the psutil package, which isn't "
                    "installed. Everything else still works.")

        cached = _cached_machine()
        if cached is not None:
            return cached

        reading = await asyncio.to_thread(_sample_machine)
        _store_machine(reading)
        return reading

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
