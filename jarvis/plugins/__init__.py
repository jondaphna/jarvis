"""Plugin auto-discovery.

Scans the built-in plugin folder and the user's personal plugin folder, imports
every module, and instantiates any Plugin subclass it finds. A plugin that
fails to import is reported and skipped - one bad file never stops JARVIS from
starting, which matters when a mission is due to run at 2am.
"""

from __future__ import annotations

import importlib
import importlib.util
import inspect
import pkgutil
import sys
from pathlib import Path
from typing import Any, Iterable

from ..core.events import log
from ..core.tools import ExecContext, ToolRegistry
from .base import Plugin, PluginError

__all__ = ["Plugin", "PluginError", "PluginManager"]

#: Modules in this package that aren't plugins.
_SKIP = {"base", "__init__", "TEMPLATE"}


class PluginManager:
    """Finds plugins, keeps them, and exposes them as tools."""

    def __init__(self, config, app: Any = None) -> None:
        self.config = config
        self.app = app
        self.plugins: dict[str, Plugin] = {}
        self.errors: dict[str, str] = {}

    # ------------------------------------------------------------------ #
    # Discovery
    # ------------------------------------------------------------------ #

    def discover(self, extra_dirs: Iterable[Path] | None = None) -> dict[str, Plugin]:
        self.plugins.clear()
        self.errors.clear()

        for module_name in self._builtin_modules():
            self._load_module(f"{__name__}.{module_name}", label=module_name)

        for directory in extra_dirs or []:
            self._load_directory(Path(directory))

        log.info("loaded %d plugin(s): %s", len(self.plugins),
                 ", ".join(sorted(self.plugins)) or "none")
        if self.errors:
            log.warning("skipped %d plugin(s): %s", len(self.errors),
                        "; ".join(f"{k}: {v}" for k, v in self.errors.items()))
        return self.plugins

    def _builtin_modules(self) -> list[str]:
        package_dir = Path(__file__).parent
        return sorted(
            name for _, name, is_pkg in pkgutil.iter_modules([str(package_dir)])
            if not is_pkg and name not in _SKIP and not name.startswith("_")
        )

    def _load_directory(self, directory: Path) -> None:
        if not directory.is_dir():
            return
        for path in sorted(directory.glob("*.py")):
            if path.stem.startswith("_") or path.stem in _SKIP:
                continue
            module_name = f"jarvis_user_plugins.{path.stem}"
            try:
                spec = importlib.util.spec_from_file_location(module_name, path)
                if spec is None or spec.loader is None:
                    raise PluginError("could not build an import spec")
                module = importlib.util.module_from_spec(spec)
                sys.modules[module_name] = module
                spec.loader.exec_module(module)
                self._register_from(module, label=path.name)
            except Exception as exc:
                self.errors[path.name] = str(exc)
                log.warning("plugin %s failed to load: %s", path.name, exc)

    def _load_module(self, dotted: str, label: str) -> None:
        try:
            module = importlib.import_module(dotted)
            self._register_from(module, label=label)
        except Exception as exc:
            self.errors[label] = str(exc)
            log.warning("plugin %s failed to load: %s", label, exc, exc_info=True)

    def _register_from(self, module: Any, label: str) -> None:
        for _, obj in inspect.getmembers(module, inspect.isclass):
            if not issubclass(obj, Plugin) or obj is Plugin:
                continue
            if obj.__module__ != module.__name__:
                continue          # imported from elsewhere, not defined here
            if getattr(obj, "NAME", "unnamed") == "unnamed":
                continue          # the template itself
            try:
                instance = obj(self.config, self.app)
            except Exception as exc:
                self.errors[label] = f"{obj.__name__}: {exc}"
                continue
            self.plugins[instance.NAME] = instance

    # ------------------------------------------------------------------ #
    # Access
    # ------------------------------------------------------------------ #

    def get(self, name: str) -> Plugin | None:
        return self.plugins.get(name)

    def available(self) -> dict[str, Plugin]:
        return {name: p for name, p in self.plugins.items() if p.available()}

    def catalogue(self) -> list[dict[str, Any]]:
        """Everything known, including what's missing - drives the Settings UI."""
        rows = [plugin.info() for plugin in self.plugins.values()]
        rows.sort(key=lambda row: (not row["available"], row["name"]))
        return rows

    def required_keys(self) -> dict[str, list[str]]:
        """API key -> the plugins that want it."""
        wanted: dict[str, list[str]] = {}
        for plugin in self.plugins.values():
            for key in plugin.REQUIRES_KEYS:
                wanted.setdefault(key, []).append(plugin.NAME)
        return wanted

    # ------------------------------------------------------------------ #
    # Exposure as tools
    # ------------------------------------------------------------------ #

    def register_tools(self, registry: ToolRegistry) -> int:
        """Give Claude a tool for every plugin that's ready to run."""
        count = 0
        for plugin in self.plugins.values():
            if not plugin.EXPOSE_AS_TOOL or not plugin.available():
                continue
            spec = plugin.get_llm_tool_spec()
            if not spec:
                continue

            async def handler(params: dict[str, Any], context: ExecContext,
                              _plugin: Plugin = plugin) -> Any:
                return await _plugin.run(params, context)

            registry.add(
                spec.get("name", plugin.NAME),
                spec.get("description", plugin.DESCRIPTION),
                spec.get("input_schema", plugin.SCHEMA),
                handler,
                capability=plugin.CAPABILITY,
                resource_key=plugin.RESOURCE_KEY,
                amount_key=plugin.AMOUNT_KEY,
                path_keys=tuple(plugin.PATH_KEYS),
                static_resource=plugin.SERVICE,
                attended_only=plugin.ATTENDED_ONLY,
                source=f"plugin:{plugin.NAME}",
            )
            count += 1
        return count

    async def run(self, name: str, params: dict[str, Any],
                  context: ExecContext) -> Any:
        """Run a plugin directly (used by mission steps)."""
        plugin = self.plugins.get(name)
        if plugin is None:
            raise PluginError(f"There's no plugin called {name!r}.")
        missing = plugin.missing_requirements()
        if missing:
            raise PluginError(
                f"The {name} plugin isn't ready - missing: {', '.join(missing)}. "
                f"Add it in Settings or with `jarvis setup`.")
        return await plugin.run(params, context)
