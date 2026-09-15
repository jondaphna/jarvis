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
        ("fetch_page", "search_the_web", "read_page", "inspect_page",
         "go_back", "take_screenshot", "scroll")),
    Capability(
        "control_web", "Click and type in pages",
        "Fill boxes and press buttons on the page you are looking at. "
        "Needed to actually play a song rather than just find it.",
        ("click", "type_text", "press_key", "confirm_browser_action"),
        risk="medium"),
    Capability(
        "apps", "Open apps and folders",
        "Start programs on this computer and open folders.",
        ("open_app", "open_folder")),
    Capability(
        "media", "Volume and music",
        "Turn the volume up and down, play, pause and skip.",
        ("set_volume", "control_music")),
    Capability(
        "machine", "See how the machine is doing",
        "Processor, memory, disk and battery.",
        ("system_status",)),
    Capability(
        "memory", "Remember things about you",
        "Save and recall what you tell it, and search past conversations.",
        ("remember", "recall", "forget", "search_memory")),
    Capability(
        "reasoning", "Think hard about things",
        "Hand difficult questions, plans and writing to Claude instead of "
        "answering off the cuff. Slower, and much better.",
        ("think",)),
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
    """Is this capability switched on?"""
    capability = BY_KEY.get(key)
    if capability is None:
        return True
    settings = settings if settings is not None else _settings()
    if settings is None:
        return capability.default
    try:
        return bool(settings.get(f"permissions.{key}", capability.default))
    except Exception:
        return capability.default


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
