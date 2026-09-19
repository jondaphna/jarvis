"""OpenAI - optional backup brain and an alternate voice for mission steps."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import requests

from .base import Completion, LLMProvider, ProviderError, ProviderUnavailable

API_URL = "https://api.openai.com/v1/chat/completions"

PRICING = {  # USD per 1M tokens (input, output)
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4.1": (2.00, 8.00),
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (2.50, 10.00),
}


class OpenAIProvider(LLMProvider):
    name = "openai"
    supports_tools = False

    def available(self) -> bool:
        return bool(self.config.key("OPENAI_API_KEY"))

    def default_model(self) -> str:
        return self.settings.get("openai_model", "gpt-4.1-mini")

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
        key = self.config.key("OPENAI_API_KEY")
        if not key:
            raise ProviderUnavailable("No OpenAI API key configured.")

        model = model or self.default_model()
        messages: list[dict[str, Any]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.extend(history or [])
        messages.append({"role": "user", "content": prompt})

        payload = {"model": model, "messages": messages,
                   "max_completion_tokens": max_tokens, "temperature": temperature}

        def _post() -> dict[str, Any]:
            response = requests.post(
                API_URL,
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                data=json.dumps(payload),
                timeout=180,
            )
            if response.status_code >= 400:
                raise ProviderError(f"OpenAI error {response.status_code}: {response.text[:300]}")
            return response.json()

        try:
            data = await asyncio.to_thread(_post)
        except requests.RequestException as exc:
            raise ProviderError(f"Could not reach OpenAI: {exc}") from exc

        text = (data.get("choices") or [{}])[0].get("message", {}).get("content", "") or ""
        usage = data.get("usage") or {}
        pin, pout = PRICING.get(model, (0.40, 1.60))
        input_tokens = int(usage.get("prompt_tokens", 0))
        output_tokens = int(usage.get("completion_tokens", 0))

        return Completion(
            text=text.strip(),
            model=model,
            provider=self.name,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=round(input_tokens / 1e6 * pin + output_tokens / 1e6 * pout, 6),
            stop_reason=(data.get("choices") or [{}])[0].get("finish_reason", ""),
            raw=data,
        )
