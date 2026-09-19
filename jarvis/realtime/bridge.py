"""Exposing JARVIS's tools to the realtime agent, with the permission gate intact.

The tool registry is dynamic - plugins add to it at startup - so tools are
declared with LiveKit's `raw_schema` form rather than typed function signatures.
The schema JARVIS already keeps for Claude is exactly the shape LiveKit wants.

Nothing here bypasses anything: every call goes through `ToolRegistry.execute`,
which is the same path the desktop app uses, so the permission broker rules on
it before it runs.
"""

from __future__ import annotations

from typing import Any

from ..core.events import log
from ..core.tools import ExecContext

#: Tools that make no sense over a phone connection, or would block the call.
_SKIP_IN_REALTIME = {"type_text", "press_keys", "click"}


def build_tools(app, *, unattended: bool = False) -> list[Any]:
    """Wrap every JARVIS tool as a LiveKit function tool."""
    from livekit.agents import RunContext, function_tool
    from livekit.agents.llm import ToolError

    tools: list[Any] = []

    for spec in app.tools.specs(unattended=unattended):
        name = spec["name"]
        if name in _SKIP_IN_REALTIME:
            continue

        schema = {
            "name": name,
            "description": spec.get("description", ""),
            "parameters": spec.get("input_schema") or {"type": "object",
                                                       "properties": {}},
        }

        def make(tool_name: str):
            async def run(raw_arguments: dict[str, object],
                          context: RunContext) -> str:
                execution = ExecContext(actor="voice", assistant=app,
                                        unattended=False)
                result = await app.tools.execute(tool_name, dict(raw_arguments),
                                                 execution)
                if result.is_error:
                    # ToolError text goes back to the model, which then explains
                    # it out loud - so a refused action is spoken, not silent.
                    raise ToolError(result.content)
                return result.content

            run.__name__ = tool_name
            return run

        tools.append(function_tool(make(name), raw_schema=schema))

    log.info("realtime: exposed %d tool(s)", len(tools))
    return tools


def instructions_for(app) -> str:
    """JARVIS's own system prompt, tuned for a live spoken conversation."""
    base = app.brain.stable_system(tier="voice")
    standing = app.rules.instruction_block() if app.rules else ""

    spoken = """
You are in a live voice call. That changes how you speak:

- Short. One or two sentences unless they ask for more. This is a conversation,
  not a briefing.
- Never read out markdown, lists, URLs, code or long numbers. Say them naturally
  or offer to put them on screen.
- They can interrupt you at any moment, and will. If they do, drop what you were
  saying immediately and follow them - don't finish the old sentence, and don't
  complain about being cut off.
- If a tool takes a moment, say so in a few words first, then do it.
- When something is refused by permissions, say so plainly and tell them the one
  sentence that would authorise it. Never retry it silently.
- If you didn't catch something, say so and ask - don't guess at what they meant.
""".strip()

    parts = [base, spoken]
    if standing:
        parts.append(standing)
    return "\n\n".join(parts)
