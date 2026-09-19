"""LLM providers.

Claude is the primary brain - it runs the agent loop, uses tools, and does the
overnight work. The others are here so JARVIS keeps answering when Claude is
unreachable, rate-limited, or you simply prefer a different model for a
particular mission step.

Capability is deliberately uneven and that's documented, not accidental:
Anthropic implements the full tool-using agent loop; the rest implement
plain text completion, which is all a mission's `llm` step needs.
"""

from __future__ import annotations

from .base import Completion, LLMProvider, ProviderError, ProviderUnavailable

__all__ = ["Completion", "LLMProvider", "ProviderError", "ProviderUnavailable",
           "build_providers"]


def build_providers(config) -> dict[str, LLMProvider]:
    """Instantiate every provider the user has configured, in preference order."""
    from .anthropic_provider import AnthropicProvider
    from .gemini_provider import GeminiProvider
    from .ollama_provider import OllamaProvider
    from .openai_provider import OpenAIProvider

    classes = {
        "anthropic": AnthropicProvider,
        "openai": OpenAIProvider,
        "gemini": GeminiProvider,
        "ollama": OllamaProvider,
    }
    order = config.settings.get("provider_order", list(classes)) or list(classes)

    providers: dict[str, LLMProvider] = {}
    for name in order:
        cls = classes.get(name)
        if cls is None:
            continue
        try:
            providers[name] = cls(config)
        except Exception:  # a broken optional provider must not stop startup
            continue
    return providers
