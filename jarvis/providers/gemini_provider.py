"""Google Gemini - optional cheap/fast brain with a generous free tier."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import requests

from .base import Completion, LLMProvider, ProviderError, ProviderUnavailable

BASE_URL = "https://generativelanguage.googleapis.com/v1beta/models"

PRICING = {"gemini-2.5-flash": (0.30, 2.50), "gemini-2.5-pro": (1.25, 10.00)}


class GeminiProvider(LLMProvider):
    name = "gemini"
    supports_tools = False

    def available(self) -> bool:
        return bool(self.config.key("GEMINI_API_KEY"))

    def default_model(self) -> str:
        return self.settings.get("gemini_model", "gemini-2.5-flash")

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
        key = self.config.key("GEMINI_API_KEY")
        if not key:
            raise ProviderUnavailable("No Gemini API key configured.")

        model = model or self.default_model()
        contents: list[dict[str, Any]] = []
        for message in history or []:
            role = "model" if message.get("role") == "assistant" else "user"
            contents.append({"role": role, "parts": [{"text": str(message.get("content", ""))}]})
        contents.append({"role": "user", "parts": [{"text": prompt}]})

        payload: dict[str, Any] = {
            "contents": contents,
            "generationConfig": {"maxOutputTokens": max_tokens, "temperature": temperature},
        }
        if system:
            payload["systemInstruction"] = {"parts": [{"text": system}]}

        url = f"{BASE_URL}/{model}:generateContent"

        def _post() -> dict[str, Any]:
            response = requests.post(
                url,
                headers={"Content-Type": "application/json", "x-goog-api-key": key},
                data=json.dumps(payload),
                timeout=180,
            )
            if response.status_code >= 400:
                raise ProviderError(f"Gemini error {response.status_code}: {response.text[:300]}")
            return response.json()

        try:
            data = await asyncio.to_thread(_post)
        except requests.RequestException as exc:
            raise ProviderError(f"Could not reach Gemini: {exc}") from exc

        candidates = data.get("candidates") or []
        parts = (candidates[0].get("content", {}).get("parts", []) if candidates else [])
        text = "".join(part.get("text", "") for part in parts)

        usage = data.get("usageMetadata") or {}
        pin, pout = PRICING.get(model, (0.30, 2.50))
        input_tokens = int(usage.get("promptTokenCount", 0))
        output_tokens = int(usage.get("candidatesTokenCount", 0))

        return Completion(
            text=text.strip(),
            model=model,
            provider=self.name,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=round(input_tokens / 1e6 * pin + output_tokens / 1e6 * pout, 6),
            stop_reason=(candidates[0].get("finishReason", "") if candidates else ""),
            raw=data,
        )
