"""The provider interface every LLM backend implements."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


class ProviderError(RuntimeError):
    """The provider was reachable but the call failed."""


class ProviderUnavailable(ProviderError):
    """The provider is not configured (no key, service not running)."""


@dataclass
class Completion:
    text: str
    model: str
    provider: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float = 0.0
    stop_reason: str = ""
    raw: Any = None
    meta: dict[str, Any] = field(default_factory=dict)


class LLMProvider(ABC):
    name: str = "provider"
    #: True when this provider can run JARVIS's tool-using agent loop.
    supports_tools: bool = False

    def __init__(self, config) -> None:
        self.config = config
        self.settings = config.settings

    @abstractmethod
    def available(self) -> bool:
        """True when this provider has what it needs to make a call."""

    @abstractmethod
    async def complete(
        self,
        prompt: str,
        *,
        system: str = "",
        model: str | None = None,
        max_tokens: int = 4096,
        history: list[dict[str, Any]] | None = None,
        **options: Any,
    ) -> Completion:
        """One-shot text completion. Raises ProviderError on failure."""

    def default_model(self) -> str:
        raise NotImplementedError
