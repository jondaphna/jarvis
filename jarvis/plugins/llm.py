"""Send a prompt to any configured model. The workhorse of most missions."""

from __future__ import annotations

from typing import Any

from ..core.grants import CAP_LLM_CALL
from ..core.tools import ExecContext
from .base import Plugin, PluginError


class LLMPlugin(Plugin):
    NAME = "llm"
    DESCRIPTION = ("Ask an AI model to write, summarise, rewrite or analyse text. "
                   "Mission steps use this for scripts, captions and summaries.")
    CAPABILITY = CAP_LLM_CALL
    #: Claude already *is* the brain - exposing this as a tool would just let it
    #: call itself in circles. Missions still use it by name.
    EXPOSE_AS_TOOL = False

    SCHEMA = {
        "type": "object",
        "properties": {
            "prompt": {"type": "string", "description": "What to ask"},
            "system": {"type": "string", "description": "Optional role instruction"},
            "model": {"type": "string", "description": "Specific model id (optional)"},
            "provider": {"type": "string",
                         "enum": ["anthropic", "openai", "gemini", "ollama"]},
            "tier": {"type": "string", "enum": ["voice", "general", "deep"]},
            "max_tokens": {"type": "integer"},
            "json": {"type": "boolean", "description": "Ask for JSON and parse it"},
        },
        "required": ["prompt"],
    }

    def available(self) -> bool:
        brain = getattr(self.app, "brain", None)
        return brain.ready() if brain else bool(self.config.key("ANTHROPIC_API_KEY"))

    def missing_requirements(self) -> list[str]:
        return [] if self.available() else ["an AI provider key (ANTHROPIC_API_KEY)"]

    async def run(self, params: dict[str, Any], context: ExecContext) -> Any:
        brain = getattr(self.app, "brain", None)
        if brain is None:
            raise PluginError("The AI brain isn't running.")

        prompt = str(params.get("prompt", "")).strip()
        if not prompt:
            raise PluginError("No prompt given.")

        want_json = bool(params.get("json"))
        system = params.get("system") or ""
        if want_json:
            system = (system + "\n\nReply with valid JSON only. No prose, no code "
                               "fences, no commentary.").strip()

        completion = await brain.complete(
            prompt,
            tier=params.get("tier", "deep"),
            system=system,
            model=params.get("model"),
            provider=params.get("provider"),
            max_tokens=int(params.get("max_tokens", 8000)),
        )

        if not want_json:
            return completion.text
        return _parse_json(completion.text)


def _parse_json(text: str) -> Any:
    """Parse JSON out of a model reply, surviving code fences and stray prose."""
    import json

    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("```")[1] if "```" in cleaned[3:] else cleaned[3:]
        cleaned = cleaned.removeprefix("json").strip()

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    for opener, closer in (("[", "]"), ("{", "}")):
        start, end = cleaned.find(opener), cleaned.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(cleaned[start:end + 1])
            except json.JSONDecodeError:
                continue

    raise PluginError(f"The model didn't return usable JSON. It said: {text[:300]}")
