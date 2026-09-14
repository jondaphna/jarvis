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

from .base import Completion, LLMProvider, ProviderError, ProviderUnavailable


class OllamaProvider(LLMProvider):
    name = "ollama"
    supports_tools = False

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
