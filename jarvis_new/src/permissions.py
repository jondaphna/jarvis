"""What Jarvis is allowed to do, as switches you control.

A permission that only changes the wording of a prompt is theatre: the model
can still call the tool, and one persuasive sentence later it does. So a switch
turned off here removes the tool from the list the model is given. It cannot
choose something it was never offered, and there is nothing to talk it out of.

Capabilities are coarse on purpose. "Can it type into pages" is a question you
can answer; "can it call press_key" is not.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


class Capability:
    """One switch, the tools it governs, and how to explain it."""

    def __init__(self, key: str, label: str, detail: str,
                 tools: tuple[str, ...], default: bool = True,
                 risk: str = "low") -> None:
        self.key = key
        self.label = label
        self.detail = detail
        self.tools = tools
        self.default = default
        self.risk = risk

    def as_dict(self) -> dict[str, Any]:
        return {"key": self.key, "label": self.label, "detail": self.detail,
                "tools": list(self.tools), "default": self.default,
                "risk": self.risk}


CAPABILITIES: tuple[Capability, ...] = (
    Capability(
        "browse", "Open websites",
        "Open sites in your browser and go straight to things in them.",
        ("open_url", "search_on_site")),
    Capability(
        "read_web", "Read web pages",
        "Read a page to answer your question, and search the web.",
        ("read_web_page", "search_the_web", "inspect_page")),
    Capability(
        "control_web", "Click and type in pages",
        "Fill boxes and press buttons on the page you are looking at. "
        "Needed to actually play a song rather than just find it.",
        ("click", "type_text", "page_action", "confirm_browser_action"),
        risk="medium"),
    Capability(
        "apps", "Open apps and manage windows",
        "Start programs and folders, and close, focus or minimise windows.",
        ("open_app", "window_action")),
    Capability(
        "media", "Volume and music",
        "Turn the volume up and down, play, pause and skip.",
        ("set_volume", "control_music")),
    Capability(
        "machine", "See how the machine is doing",
        "Processor, memory, disk and battery.",
        ("system_status",)),
    Capability(
        "files", "Read your own files",
        "Find and read your documents to answer a question about them - "
        "\"where's that invoice\", \"what does my plan say about pricing\". "
        "Reading only: it never changes, moves or deletes anything.",
        ("search_my_files",)),
    Capability(
        "memory", "Remember things about you",
        "Save and recall what you tell it, and search past conversations.",
        ("remember", "forget", "search_memory")),
    Capability(
        "learning", "Learn from you",
        "Let you teach it how you want jobs done, and let it write down a "
        "recipe that worked so the same request lands first time next time.",
        ("remember_how", "how_do_i")),
    Capability(
        "reasoning", "Think hard about things",
        "Hand difficult questions, plans and writing to a second brain "
        "instead of answering off the cuff. Slower, and much better. Free "
        "unless you switch on a paid model in the AI tab.",
        ("think",)),
    Capability(
        "content", "Run the content engine",
        "Write Instagram Reel scripts in the background and tell you how the "
        "content side is doing. Writing only: it never posts anything, and "
        "the script writing uses the same free brain as thinking.",
        ("write_reel_scripts", "content_engine_status", "read_reel_script"),
        # Off until you switch it on, for one measured reason: three more
        # tools in every prompt is three more things competing with "open my
        # Spotify" for the model's attention, on every turn, whether or not
        # you are running a channel that day. The business engine is a mode
        # you turn on, not a tax on the conversation.
        default=False),
    Capability(
        "power", "Lock, sleep, restart, shut down",
        "Power actions. It always asks out loud first, and one yes covers "
        "exactly one action.",
        ("lock_screen", "power_action", "confirm_power_action"),
        default=False, risk="high"),
)

BY_KEY = {capability.key: capability for capability in CAPABILITIES}

#: Every tool that any switch governs. Tools outside this set - ending the
#: call, for instance - are always available.
GOVERNED = {tool for capability in CAPABILITIES for tool in capability.tools}


def _settings() -> Any:
    try:
        from jarvis.config import Settings

        return Settings.load()
    except Exception:
        return None


def allowed(key: str, settings: Any = None) -> bool:
    """Is this capability switched on?

    Everything uncertain here answers no, which is the opposite of how this
    used to read, and each of the three cases was a real way to say yes by
    accident:

    - **A capability nobody declared** used to be allowed, on the grounds
      that switches govern known tools and an unknown name is not one of
      them. But a renamed key, a typo in a caller, or a tool added to the
      registry and forgotten here all arrive as an unknown name, and every
      one of them silently granted itself permission.
    - **A stored value that is not a boolean** went through `bool()`, where
      the string `"false"` - which is what a hand-edited settings file, or a
      form that posted its checkbox as text, actually contains - is true.
      Only a real `True` counts now.
    - **Settings that cannot be read at all** fell back to the shipped
      default, so a corrupt file re-enabled every capability that ships on.
      A policy you cannot read is not a policy that permits things.

    A capability that is simply *absent* from the settings still takes its
    shipped default. That is not a fallback: it is the value in force until
    somebody changes it, and on a machine where nothing has been switched off
    yet there is nothing to honour but the default.
    """
    capability = BY_KEY.get(key)
    if capability is None:
        return False
    settings = settings if settings is not None else _settings()
    if settings is None:
        return False
    # Settings standing in for a file that could not be read are not the
    # policy, they are the shipped defaults wearing its name. See
    # `Settings.unreadable`.
    if getattr(settings, "unreadable", False):
        return False
    try:
        value = settings.get(f"permissions.{key}", capability.default)
    except Exception:
        return False
    if value is None:
        return capability.default
    return value is True


def blocked_tools(settings: Any = None) -> set[str]:
    """Every tool the switches currently forbid."""
    settings = settings if settings is not None else _settings()
    forbidden: set[str] = set()
    for capability in CAPABILITIES:
        if not allowed(capability.key, settings):
            forbidden.update(capability.tools)
    return forbidden


def filter_tools(tools: list[Any], settings: Any = None) -> list[Any]:
    """Drop the tools that are switched off.

    Removing them is the whole point. Leaving them in and refusing at call
    time still lets the model try, announce it is trying, and argue about it.
    """
    forbidden = blocked_tools(settings)
    if not forbidden:
        return tools
    kept = []
    for tool in tools:
        name = getattr(getattr(tool, "info", None), "name", None)
        if name is None or name not in forbidden:
            kept.append(tool)
    return kept


def summary(settings: Any = None) -> str:
    """A line for the prompt about anything switched off.

    The model is told what it cannot do, so it can say so plainly instead of
    being confused about a tool that seems to be missing.
    """
    settings = settings if settings is not None else _settings()
    off = [c for c in CAPABILITIES if not allowed(c.key, settings)]
    if not off:
        return ""
    lines = "\n".join(f"- {c.label.lower()}" for c in off)
    return ("# Switched off\n"
            "You currently cannot do these, because they are turned off in "
            "the settings. If asked, say so plainly and mention the settings "
            "panel - don't look for another way round:\n\n" + lines)


def current(settings: Any = None) -> list[dict[str, Any]]:
    """Every capability with its current state, for the interface."""
    settings = settings if settings is not None else _settings()
    out = []
    for capability in CAPABILITIES:
        entry = capability.as_dict()
        entry["enabled"] = allowed(capability.key, settings)
        out.append(entry)
    return out
