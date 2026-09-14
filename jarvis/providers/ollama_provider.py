"""Ollama - fully local models. Free, private, works with the internet down.

Weaker than Claude at long autonomous tool chains, so JARVIS uses it as a
last-resort fallback rather than a primary brain. Perfectly good for a mission
step that just needs text rewritten.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import requests

from .base import (
    AdaptedResponse, AdaptedUsage, Completion, LLMProvider, ProviderError,
    ProviderUnavailable, TextBlock, ToolUseBlock,
)


class OllamaProvider(LLMProvider):
    name = "ollama"
    #: Recent Ollama models (llama3.1+, qwen2.5, mistral-nemo) do support tool
    #: calling, so this provider can drive the full agent loop - for free.
    supports_tools = True

    @property
    def host(self) -> str:
        return str(self.settings.get("ollama_host", "http://127.0.0.1:11434")).rstrip("/")

    def available(self) -> bool:
        try:
            response = requests.get(f"{self.host}/api/tags", timeout=1.5)
            return response.status_code == 200
        except requests.RequestException:
            return False

    def default_model(self) -> str:
        return self.settings.get("ollama_model", "llama3.1")

    def models(self) -> list[str]:
        try:
            response = requests.get(f"{self.host}/api/tags", timeout=3)
            response.raise_for_status()
            return [m["name"] for m in response.json().get("models", [])]
        except requests.RequestException:
            return []


    # ------------------------------------------------------------------ #
    # The agent loop
    #
    # Ollama speaks an OpenAI-flavoured tool format. Everything here converts
    # to and from the Anthropic content-block shape the brain expects, so a
    # local model drives exactly the same loop Claude does.
    # ------------------------------------------------------------------ #

    async def turn(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        system: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int | None = None,
        effort: str | None = None,
        thinking: bool = True,
        on_text: Any = None,
        on_thinking: Any = None,
    ) -> AdaptedResponse:
        model = model or self.default_model()
        payload: dict[str, Any] = {
            "model": model,
            "messages": _to_ollama_messages(messages, system),
            "stream": False,
            "options": {"temperature": 0.6,
                        "num_predict": max_tokens or 2048},
        }
        if tools:
            payload["tools"] = [_to_ollama_tool(tool) for tool in tools]

        def _post() -> dict[str, Any]:
            response = requests.post(f"{self.host}/api/chat", data=json.dumps(payload),
                                     timeout=900)
            if response.status_code == 404:
                raise ProviderError(
                    f"Ollama doesn't have the model '{model}'. Install it with: "
                    f"ollama pull {model}")
            if response.status_code >= 400:
                raise ProviderError(f"Ollama error {response.status_code}: "
                                    f"{response.text[:300]}")
            return response.json()

        try:
            data = await asyncio.to_thread(_post)
        except requests.RequestException as exc:
            raise ProviderUnavailable(
                f"Ollama isn't reachable at {self.host}. Start it with `ollama serve`."
            ) from exc

        message = data.get("message") or {}
        blocks: list[Any] = []

        text = (message.get("content") or "").strip()
        if text:
            blocks.append(TextBlock(text=text))
            if on_text is not None:
                # Ollama is called non-streaming here, so the "stream" is one
                # chunk. Voice still works; it just starts a beat later.
                result = on_text(text)
                if asyncio.iscoroutine(result):
                    await result

        calls = message.get("tool_calls") or []
        for index, call in enumerate(calls):
            function = call.get("function") or {}
            arguments = function.get("arguments")
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except json.JSONDecodeError:
                    arguments = {}
            blocks.append(ToolUseBlock(
                name=function.get("name", ""),
                input=arguments if isinstance(arguments, dict) else {},
                id=f"ollama_{index}_{abs(hash(str(arguments))) % 100000}",
            ))

        return AdaptedResponse(
            content=blocks,
            stop_reason="tool_use" if calls else "end_turn",
            model=model,
            usage=AdaptedUsage(
                input_tokens=int(data.get("prompt_eval_count", 0)),
                output_tokens=int(data.get("eval_count", 0)),
            ),
        )

    async def complete(
        self,
        prompt: str,
        *,
        system: str = "",
        model: str | None = None,
        max_tokens: int = 4096,
        history: list[dict[str, Any]] | None = None,
        temperature: float = 0.7,
        **options: Any,
    ) -> Completion:
        model = model or self.default_model()
        messages: list[dict[str, Any]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.extend(history or [])
        messages.append({"role": "user", "content": prompt})

        payload = {
            "model": model,
            "messages": messages,
            "stream": False,
            "options": {"temperature": temperature, "num_predict": max_tokens},
        }

        def _post() -> dict[str, Any]:
            response = requests.post(f"{self.host}/api/chat", data=json.dumps(payload),
                                     timeout=600)
            if response.status_code >= 400:
                raise ProviderError(f"Ollama error {response.status_code}: {response.text[:300]}")
            return response.json()

        try:
            data = await asyncio.to_thread(_post)
        except requests.RequestException as exc:
            raise ProviderUnavailable(
                f"Ollama isn't reachable at {self.host}. Start it with `ollama serve`."
            ) from exc

        return Completion(
            text=(data.get("message") or {}).get("content", "").strip(),
            model=model,
            provider=self.name,
            input_tokens=int(data.get("prompt_eval_count", 0)),
            output_tokens=int(data.get("eval_count", 0)),
            cost_usd=0.0,  # local inference is free
            stop_reason=data.get("done_reason", ""),
            raw=data,
        )

# --------------------------------------------------------------------------- #
# Format conversion
# --------------------------------------------------------------------------- #

def _to_ollama_tool(tool: dict[str, Any]) -> dict[str, Any]:
    """Anthropic tool definition -> OpenAI/Ollama function definition."""
    return {
        "type": "function",
        "function": {
            "name": tool.get("name", ""),
            "description": tool.get("description", ""),
            "parameters": tool.get("input_schema") or {"type": "object",
                                                       "properties": {}},
        },
    }


def _to_ollama_messages(messages: list[dict[str, Any]],
                        system: str | None) -> list[dict[str, Any]]:
    """Anthropic message list -> Ollama chat messages.

    Tool results arrive from the brain as a user message full of tool_result
    blocks; Ollama wants one message per result with role "tool".
    """
    out: list[dict[str, Any]] = []
    if system:
        out.append({"role": "system", "content": system})

    for message in messages:
        role = message.get("role", "user")
        content = message.get("content")

        if role == "system":
            out.append({"role": "system", "content": _text_of(content)})
            continue

        if isinstance(content, str):
            out.append({"role": role, "content": content})
            continue

        blocks = list(content or [])
        tool_results = [b for b in blocks if _block_type(b) == "tool_result"]
        if tool_results:
            for block in tool_results:
                out.append({"role": "tool",
                            "content": _result_text(block)})
            leftover = _text_of([b for b in blocks if _block_type(b) != "tool_result"])
            if leftover:
                out.append({"role": "user", "content": leftover})
            continue

        tool_uses = [b for b in blocks if _block_type(b) == "tool_use"]
        entry: dict[str, Any] = {"role": role, "content": _text_of(blocks)}
        if tool_uses:
            entry["tool_calls"] = [
                {"function": {"name": _attr(b, "name", ""),
                              "arguments": _attr(b, "input", {})}}
                for b in tool_uses
            ]
        out.append(entry)

    return out


def _block_type(block: Any) -> str:
    if isinstance(block, dict):
        return block.get("type", "")
    return getattr(block, "type", "")


def _attr(block: Any, name: str, default: Any = None) -> Any:
    if isinstance(block, dict):
        return block.get(name, default)
    return getattr(block, name, default)


def _text_of(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    parts = []
    for block in content:
        if _block_type(block) == "text":
            parts.append(str(_attr(block, "text", "")))
    return "\n".join(p for p in parts if p).strip()


def _result_text(block: Any) -> str:
    content = _attr(block, "content", "")
    if isinstance(content, str):
        return content
    return _text_of(content) or str(content)
