"""The Plugin contract. Copy `TEMPLATE.py` to add a new capability.

A plugin is one file that does one thing. Drop it in `jarvis/plugins/` (or in
the user plugin folder) and JARVIS finds it at startup - no registration, no
imports to edit, no existing code touched. That "purely additive" property is
the whole point: adding an income stream should never risk breaking the ones
already running.

Two things a plugin gets for free:
  * It becomes a tool Claude can call by voice.
  * It becomes a step any mission can use, by name.
"""

from __future__ import annotations

from typing import Any, ClassVar

from ..core.tools import ExecContext


class Plugin:
    """Base class for every JARVIS capability."""

    #: Used as the tool name and the mission step's "plugin" value. Keep it a
    #: valid identifier: lowercase, underscores.
    NAME: ClassVar[str] = "unnamed"

    #: One line, written for Claude. This is how it decides when to use you.
    DESCRIPTION: ClassVar[str] = ""

    #: API keys this plugin needs. JARVIS shows these in Settings and hides the
    #: plugin until they're present.
    REQUIRES_KEYS: ClassVar[list[str]] = []

    #: Python packages that must be importable, e.g. ["replicate"].
    REQUIRES_PACKAGES: ClassVar[list[str]] = []

    #: JSON schema for run()'s params. Also what Claude sees.
    SCHEMA: ClassVar[dict[str, Any]] = {"type": "object", "properties": {}}

    #: Permission capability this plugin exercises (jarvis.core.grants.CAP_*).
    #: None means the action is harmless. Get this right - it's what stops the
    #: plugin doing something the user didn't authorise.
    CAPABILITY: ClassVar[str | None] = None

    #: Which SCHEMA property names the thing being acted on (for the audit log
    #: and resource-scoped grants like "*tiktok*").
    RESOURCE_KEY: ClassVar[str | None] = None

    #: Which SCHEMA property carries a dollar amount, for spend-limited grants.
    AMOUNT_KEY: ClassVar[str | None] = None

    #: The service this plugin acts on ("kling", "replicate", "heygen"). Used as
    #: the permission resource, so "you can spend $20 on Kling" actually matches
    #: the Kling plugin - scoping a spend grant to a prompt string never would.
    SERVICE: ClassVar[str] = ""

    #: SCHEMA properties holding filesystem paths. These are resolved to
    #: absolute form before the permission check, and an omitted optional one
    #: resolves to the workspace - so "save it in the usual place" is judged as
    #: the workspace write it actually is.
    PATH_KEYS: ClassVar[tuple[str, ...]] = ()

    #: Set False for plugins that only make sense inside a mission.
    EXPOSE_AS_TOOL: ClassVar[bool] = True

    #: Set True if the plugin needs a human at the keyboard.
    ATTENDED_ONLY: ClassVar[bool] = False

    def __init__(self, config, app: Any = None) -> None:
        self.config = config
        self.settings = config.settings
        #: The Assistant, when one exists - gives access to brain, memory, etc.
        self.app = app

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    def available(self) -> bool:
        """True when this plugin can actually run. Override for custom checks."""
        return not self.missing_requirements()

    def missing_requirements(self) -> list[str]:
        """Human-readable list of what's stopping this plugin from working."""
        missing = [key for key in self.REQUIRES_KEYS if not self.config.key(key)]
        for package in self.REQUIRES_PACKAGES:
            try:
                __import__(package)
            except ImportError:
                missing.append(f"python package '{package}'")
        return missing

    # ------------------------------------------------------------------ #
    # Work
    # ------------------------------------------------------------------ #

    async def run(self, params: dict[str, Any], context: ExecContext) -> Any:
        """Do the thing. Return anything JSON-serialisable (or a file path)."""
        raise NotImplementedError(f"{self.NAME}.run() is not implemented")

    # ------------------------------------------------------------------ #
    # Introspection
    # ------------------------------------------------------------------ #

    def get_llm_tool_spec(self) -> dict[str, Any]:
        """The tool definition Claude sees. Override only for unusual shapes."""
        return {
            "name": self.NAME,
            "description": self.DESCRIPTION,
            "input_schema": self.SCHEMA,
        }

    def key(self, name: str, default: str | None = None) -> str | None:
        return self.config.key(name, default)

    def info(self) -> dict[str, Any]:
        return {
            "name": self.NAME,
            "description": self.DESCRIPTION,
            "available": self.available(),
            "missing": self.missing_requirements(),
            "requires_keys": list(self.REQUIRES_KEYS),
            "capability": self.CAPABILITY,
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Plugin {self.NAME} available={self.available()}>"


class PluginError(RuntimeError):
    """Something went wrong inside a plugin, phrased for the user."""
