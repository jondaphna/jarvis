"""The tool surface JARVIS can act through, and the gate every call passes.

A tool is a function Claude may call. Each one declares the capability it
exercises (`fs.write`, `payment.spend`, ...) and which of its parameters names
the resource being touched. That declaration is what lets the permission broker
judge a call *before* it runs, rather than trusting the model to police itself.

Tools come from two places: the core set defined in `jarvis/core/actions.py`,
and every plugin discovered under `jarvis/plugins/`. Both land in one registry.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from . import events
from .events import bus, log
from .permissions import Decision, PermissionBroker, PermissionDenied, Request


@dataclass
class ExecContext:
    """Who is running this tool and on whose behalf."""

    actor: str = "jarvis"
    run_id: int | None = None
    assistant: Any = None           # the Assistant, for tools that need the whole app
    unattended: bool = False

    def child(self, **overrides: Any) -> "ExecContext":
        data = {"actor": self.actor, "run_id": self.run_id,
                "assistant": self.assistant, "unattended": self.unattended}
        data.update(overrides)
        return ExecContext(**data)


Handler = Callable[..., Awaitable[Any]]


@dataclass
class Tool:
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Handler
    #: Permission capability this tool exercises. None = no permission needed.
    capability: str | None = None
    #: Which input parameter names the thing being acted on (for the audit log
    #: and for resource-scoped grants, e.g. "*tiktok*").
    resource_key: str | None = None
    #: Which input parameter carries a dollar amount, for spend-limited grants.
    amount_key: str | None = None
    #: A fixed resource name (a service, not a parameter). Takes precedence over
    #: resource_key, because a grant is scoped to "Kling", not to a prompt.
    static_resource: str = ""
    #: Extra (capability, parameter) pairs that must ALSO be cleared. A move
    #: deletes from the source and writes to the destination; both get judged.
    also_requires: tuple[tuple[str, str], ...] = ()
    #: Parameters holding filesystem paths. These are resolved to absolute form
    #: *before* the permission check, so the broker judges exactly the path the
    #: handler will touch - a bare "notes.txt" means the workspace, to both.
    path_keys: tuple[str, ...] = ()
    source: str = "core"
    #: Tools marked unsafe are hidden from missions running with nobody watching.
    attended_only: bool = False

    def spec(self) -> dict[str, Any]:
        """The Claude tool definition."""
        schema = dict(self.input_schema)
        schema.setdefault("type", "object")
        schema.setdefault("properties", {})
        return {"name": self.name, "description": self.description, "input_schema": schema}

    def resource_from(self, params: dict[str, Any]) -> str | None:
        if self.static_resource:
            return self.static_resource
        if not self.resource_key:
            return None
        value = params.get(self.resource_key)
        return None if value is None else str(value)

    def amount_from(self, params: dict[str, Any]) -> float | None:
        if not self.amount_key:
            return None
        try:
            return float(params.get(self.amount_key))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None


@dataclass
class ToolResult:
    content: str
    is_error: bool = False
    data: Any = None
    decision: Decision | None = None

    def block(self, tool_use_id: str) -> dict[str, Any]:
        """As a tool_result content block for the Messages API."""
        block: dict[str, Any] = {
            "type": "tool_result",
            "tool_use_id": tool_use_id,
            "content": self.content or "(no output)",
        }
        if self.is_error:
            block["is_error"] = True
        return block


class ToolRegistry:
    """Everything JARVIS can do, in one place."""

    def __init__(self, broker: PermissionBroker,
                 path_resolver: Callable[[str], Any] | None = None) -> None:
        self.broker = broker
        #: Turns a user-supplied path into the absolute path that will be used.
        #: Normally Computer.resolve; identity when running without a Computer.
        self.path_resolver = path_resolver or (lambda value: value)
        self._tools: dict[str, Tool] = {}

    # ------------------------------------------------------------------ #

    def register(self, tool: Tool) -> Tool:
        if tool.name in self._tools:
            log.debug("tool %s re-registered by %s", tool.name, tool.source)
        self._tools[tool.name] = tool
        return tool

    def add(
        self,
        name: str,
        description: str,
        input_schema: dict[str, Any],
        handler: Handler,
        **kwargs: Any,
    ) -> Tool:
        return self.register(Tool(name=name, description=description,
                                  input_schema=input_schema, handler=handler, **kwargs))

    def remove(self, name: str) -> None:
        self._tools.pop(name, None)

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def all(self) -> list[Tool]:
        return [self._tools[name] for name in sorted(self._tools)]

    def specs(self, unattended: bool = False) -> list[dict[str, Any]]:
        """Tool definitions for the API, in a stable order so the cache holds.

        Tool definitions are the first thing in the cached prefix - shuffling
        them between turns silently throws away every cache hit.
        """
        tools = [t for t in self.all() if not (unattended and t.attended_only)]
        return [tool.spec() for tool in tools]

    # ------------------------------------------------------------------ #
    # Execution - the permission gate lives here
    # ------------------------------------------------------------------ #

    async def execute(self, name: str, params: dict[str, Any],
                      context: ExecContext) -> ToolResult:
        tool = self._tools.get(name)
        if tool is None:
            return ToolResult(f"There is no tool called {name!r}.", is_error=True)

        if context.unattended and tool.attended_only:
            return ToolResult(
                f"{name} only runs when you're at the keyboard, so I skipped it.",
                is_error=True)

        params = params if isinstance(params, dict) else {}
        params = self._resolve_paths(tool, params)

        # --- permission ------------------------------------------------- #
        decision: Decision | None = None
        checks: list[tuple[str, str | None]] = []
        if tool.capability:
            checks.append((tool.capability, tool.resource_from(params)))
        for capability, key in tool.also_requires:
            value = params.get(key)
            checks.append((capability, None if value is None else str(value)))

        for capability, resource in checks:
            request = Request(
                capability=capability,
                resource=resource,
                summary=_summarise(tool, params),
                actor=context.actor,
                amount_usd=tool.amount_from(params),
                run_id=context.run_id,
                details={"tool": name, "params": _redact(params)},
            )
            try:
                decision = await self.broker.require(request)
            except PermissionDenied as denied:
                return ToolResult(denied.decision.user_message(), is_error=True,
                                  decision=denied.decision)

        # --- run ---------------------------------------------------------- #
        bus.publish(events.TOOL_CALL, f"{name}({_preview(params)})",
                    tool=name, params=_redact(params), run_id=context.run_id)
        try:
            output = await _invoke(tool.handler, params, context)
        except PermissionDenied as denied:
            return ToolResult(denied.decision.user_message(), is_error=True,
                              decision=denied.decision)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("tool %s failed", name, exc_info=True)
            bus.publish(events.TOOL_RESULT, f"{name} failed: {exc}", tool=name, error=True)
            return ToolResult(f"{name} failed: {exc}", is_error=True, decision=decision)

        text = _stringify(output)
        bus.publish(events.TOOL_RESULT, f"{name} -> {text[:160]}", tool=name,
                    run_id=context.run_id)
        return ToolResult(text, data=output, decision=decision)


    def _resolve_paths(self, tool: Tool, params: dict[str, Any]) -> dict[str, Any]:
        """Make every path argument absolute before anyone looks at it.

        Without this the broker could clear a bare filename as workspace-relative
        while the handler wrote somewhere else entirely (or the reverse, blocking
        a perfectly ordinary workspace write). Gate and action must agree on the
        exact target.
        """
        if not tool.path_keys:
            return params
        return self.resolve_paths_for(
            tool.path_keys, params,
            required=tuple(tool.input_schema.get("required") or ()))

    def resolve_paths_for(self, path_keys: tuple[str, ...], params: dict[str, Any],
                          required: tuple[str, ...] = ()) -> dict[str, Any]:
        """Absolute-ise the named path parameters. Shared with mission steps."""
        if not path_keys:
            return params
        required = set(required)
        resolved = dict(params)
        for key in path_keys:
            value = resolved.get(key)
            blank = not isinstance(value, str) or not value.strip()
            # An omitted optional path means "the workspace" - resolve it so the
            # broker judges that, rather than falling through as "no path given".
            if blank and key in required:
                continue
            try:
                resolved[key] = str(self.path_resolver(value if not blank else ""))
            except Exception:
                pass  # leave it alone; the handler will report the real error
        return resolved


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

async def _invoke(handler: Handler, params: dict[str, Any], context: ExecContext) -> Any:
    """Call a handler with whichever of (params, context) it actually accepts."""
    try:
        signature = inspect.signature(handler)
        accepts_context = "context" in signature.parameters or any(
            p.kind is inspect.Parameter.VAR_KEYWORD for p in signature.parameters.values())
    except (TypeError, ValueError):
        accepts_context = False

    result = handler(params, context=context) if accepts_context else handler(params)
    if inspect.isawaitable(result):
        return await result
    return result


_SECRET_HINTS = ("key", "token", "secret", "password", "passwd", "credential", "auth")


def _redact(params: dict[str, Any]) -> dict[str, Any]:
    """Keep secrets out of the audit log and the event stream."""
    clean: dict[str, Any] = {}
    for key, value in params.items():
        if any(hint in key.lower() for hint in _SECRET_HINTS):
            clean[key] = "***"
        elif isinstance(value, str) and len(value) > 500:
            clean[key] = value[:500] + f"... (+{len(value) - 500} chars)"
        else:
            clean[key] = value
    return clean


def _preview(params: dict[str, Any]) -> str:
    items = []
    for key, value in _redact(params).items():
        text = str(value)
        items.append(f"{key}={text[:60]}" + ("..." if len(text) > 60 else ""))
    return ", ".join(items)[:200]


def _summarise(tool: Tool, params: dict[str, Any]) -> str:
    resource = tool.resource_from(params)
    base = tool.name.replace("_", " ")
    return f"{base}: {resource}" if resource else base


def _stringify(output: Any) -> str:
    if output is None:
        return "Done."
    if isinstance(output, str):
        return output
    try:
        return json.dumps(output, indent=2, default=str, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(output)
