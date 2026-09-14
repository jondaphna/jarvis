"""Claude - JARVIS's primary brain and the only provider that runs the agent loop.

Model-specific request shapes are handled by `MODEL_PROFILES` rather than
scattered `if model ==` checks, because the parameter rules genuinely differ
between families: Opus 5 rejects `temperature` and `budget_tokens` outright and
thinks by default, while Haiku 4.5 accepts `temperature` and has no `effort`
setting. Getting this wrong is a 400, not a degraded answer.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, AsyncIterator, Callable

from .base import Completion, LLMProvider, ProviderError, ProviderUnavailable

try:
    import anthropic
    from anthropic import AsyncAnthropic
except ImportError:  # pragma: no cover - dependency is declared in requirements
    anthropic = None
    AsyncAnthropic = None  # type: ignore[assignment]


@dataclass(frozen=True)
class ModelProfile:
    """What a given Claude model will and won't accept."""

    adaptive_thinking: bool = True      # thinking={"type": "adaptive"}
    thinking_budget: bool = False       # thinking={"type": "enabled", budget_tokens: N}
    supports_effort: bool = True        # output_config={"effort": ...}
    supports_temperature: bool = False  # 400 on the 4.6+ family
    supports_fallbacks: bool = False    # server-side refusal fallbacks
    mid_conversation_system: bool = False  # {'role':'system'} inside messages[]
    input_per_mtok: float = 5.0
    output_per_mtok: float = 25.0
    max_output: int = 64000


MODEL_PROFILES: dict[str, ModelProfile] = {
    "claude-opus-5": ModelProfile(supports_fallbacks=True, mid_conversation_system=True),
    "claude-opus-4-8": ModelProfile(mid_conversation_system=True),
    "claude-opus-4-7": ModelProfile(),
    "claude-opus-4-6": ModelProfile(),
    "claude-sonnet-5": ModelProfile(input_per_mtok=2.0, output_per_mtok=10.0),
    "claude-sonnet-4-6": ModelProfile(input_per_mtok=3.0, output_per_mtok=15.0,
                                      supports_temperature=True),
    "claude-haiku-4-5": ModelProfile(
        adaptive_thinking=False, thinking_budget=True, supports_effort=False,
        supports_temperature=True, input_per_mtok=1.0, output_per_mtok=5.0,
        max_output=32000),
    "claude-fable-5-1": ModelProfile(supports_fallbacks=True, input_per_mtok=10.0,
                                     output_per_mtok=50.0, max_output=128000,
                                     mid_conversation_system=True),
    "claude-fable-5": ModelProfile(supports_fallbacks=True, input_per_mtok=10.0,
                                   output_per_mtok=50.0, max_output=128000,
                                   mid_conversation_system=True),
}

DEFAULT_PROFILE = ModelProfile()

#: Cache reads bill at ~0.1x input, writes at ~1.25x.
CACHE_READ_MULTIPLIER = 0.1
CACHE_WRITE_MULTIPLIER = 1.25

FALLBACK_BETA = "server-side-fallback-2026-07-01"


def profile_for(model: str) -> ModelProfile:
    if model in MODEL_PROFILES:
        return MODEL_PROFILES[model]
    # Unknown//future model: assume the modern shape, which is the safe default.
    for known, prof in MODEL_PROFILES.items():
        if model.startswith(known):
            return prof
    return DEFAULT_PROFILE


def estimate_cost(model: str, input_tokens: int, output_tokens: int,
                  cache_read: int = 0, cache_write: int = 0) -> float:
    prof = profile_for(model)
    cost = (input_tokens / 1_000_000) * prof.input_per_mtok
    cost += (output_tokens / 1_000_000) * prof.output_per_mtok
    cost += (cache_read / 1_000_000) * prof.input_per_mtok * CACHE_READ_MULTIPLIER
    cost += (cache_write / 1_000_000) * prof.input_per_mtok * CACHE_WRITE_MULTIPLIER
    return round(cost, 6)


class AnthropicProvider(LLMProvider):
    name = "anthropic"
    supports_tools = True

    def __init__(self, config) -> None:
        super().__init__(config)
        self._client: Any = None
        #: Flipped off permanently if the installed SDK/API rejects the beta.
        self._fallbacks_ok = bool(self.settings.get("server_side_fallbacks", True))

    # ------------------------------------------------------------------ #
    # Client
    # ------------------------------------------------------------------ #

    def available(self) -> bool:
        return anthropic is not None and bool(self.config.key("ANTHROPIC_API_KEY"))

    @property
    def client(self):
        if self._client is None:
            if anthropic is None:
                raise ProviderUnavailable(
                    "The `anthropic` package isn't installed. Run: pip install anthropic")
            key = self.config.key("ANTHROPIC_API_KEY")
            if not key:
                raise ProviderUnavailable(
                    "No Claude API key yet. Run `jarvis setup` and paste the key from "
                    "console.anthropic.com - it's the one thing JARVIS genuinely needs.")
            # Long timeout: overnight agent turns legitimately run for minutes.
            self._client = AsyncAnthropic(api_key=key, timeout=600.0, max_retries=3)
        return self._client

    def default_model(self) -> str:
        return self.settings.model_for("general")

    # ------------------------------------------------------------------ #
    # Request construction
    # ------------------------------------------------------------------ #

    def build_kwargs(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        system: str | list[dict[str, Any]] | None = None,
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int | None = None,
        effort: str | None = None,
        thinking: bool = True,
        temperature: float | None = None,
        cache_system: bool = True,
    ) -> dict[str, Any]:
        """Assemble a request body this specific model will accept."""
        prof = profile_for(model)
        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": min(max_tokens or prof.max_output, prof.max_output),
            "messages": messages,
        }

        if system:
            if isinstance(system, str) and cache_system:
                # Cache the stable prefix: identity, policy and tool list rarely
                # change between turns, so this is the cheap 90% of the prompt.
                kwargs["system"] = [{"type": "text", "text": system,
                                     "cache_control": {"type": "ephemeral"}}]
            else:
                kwargs["system"] = system

        if tools:
            kwargs["tools"] = tools

        if thinking:
            if prof.adaptive_thinking:
                kwargs["thinking"] = {"type": "adaptive", "display": "summarized"}
            elif prof.thinking_budget and (max_tokens or prof.max_output) > 2048:
                kwargs["thinking"] = {"type": "enabled", "budget_tokens": 2048}

        if effort and prof.supports_effort:
            kwargs["output_config"] = {"effort": effort}

        if temperature is not None and prof.supports_temperature and not thinking:
            kwargs["temperature"] = temperature

        return kwargs

    def _fallback_kwargs(self, model: str) -> dict[str, Any]:
        """Server-side refusal fallbacks, when the model supports them.

        If Claude declines a request on safety grounds, the API reroutes to
        another model instead of handing JARVIS an empty answer at 3am.
        """
        if not self._fallbacks_ok or not profile_for(model).supports_fallbacks:
            return {}
        return {"betas": [FALLBACK_BETA], "fallbacks": "default"}

    # ------------------------------------------------------------------ #
    # Calls
    # ------------------------------------------------------------------ #

    async def create(self, **kwargs: Any):
        """Non-streaming create, with graceful degradation off the beta path."""
        model = kwargs.get("model", "")
        extra = self._fallback_kwargs(model)
        if extra:
            try:
                return await self.client.beta.messages.create(**kwargs, **extra)
            except Exception as exc:
                if not _is_unsupported_param(exc):
                    raise
                self._fallbacks_ok = False
        return await self.client.messages.create(**kwargs)

    @asynccontextmanager
    async def stream(self, **kwargs: Any) -> AsyncIterator[Any]:
        """Streaming create, with the same beta degradation."""
        model = kwargs.get("model", "")
        extra = self._fallback_kwargs(model)
        if extra:
            try:
                async with self.client.beta.messages.stream(**kwargs, **extra) as s:
                    yield s
                return
            except Exception as exc:
                if not _is_unsupported_param(exc):
                    raise
                self._fallbacks_ok = False
        async with self.client.messages.stream(**kwargs) as s:
            yield s

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
        on_text: Callable[[str], Any] | None = None,
        on_thinking: Callable[[str], Any] | None = None,
    ):
        """One assistant turn, streamed. Returns the final message object.

        Streaming is not just for the UI: it's what lets the voice pipeline start
        speaking the first sentence while Claude is still writing the third.
        """
        kwargs = self.build_kwargs(
            model=model, messages=messages, system=system, tools=tools,
            max_tokens=max_tokens, effort=effort, thinking=thinking)

        async with self.stream(**kwargs) as stream:
            async for event in stream:
                etype = getattr(event, "type", "")
                if etype != "content_block_delta":
                    continue
                delta = getattr(event, "delta", None)
                dtype = getattr(delta, "type", "")
                if dtype == "text_delta" and on_text is not None:
                    await _maybe_await(on_text(delta.text))
                elif dtype == "thinking_delta" and on_thinking is not None:
                    await _maybe_await(on_thinking(getattr(delta, "thinking", "")))
            return await stream.get_final_message()

    async def complete(
        self,
        prompt: str,
        *,
        system: str = "",
        model: str | None = None,
        max_tokens: int = 4096,
        history: list[dict[str, Any]] | None = None,
        effort: str | None = None,
        thinking: bool = True,
        **options: Any,
    ) -> Completion:
        model = model or self.default_model()
        messages = list(history or []) + [{"role": "user", "content": prompt}]
        kwargs = self.build_kwargs(
            model=model, messages=messages, system=system or None,
            max_tokens=max_tokens, effort=effort, thinking=thinking)

        try:
            response = await self.create(**kwargs)
        except Exception as exc:
            raise ProviderError(_friendly_error(exc)) from exc

        return completion_from(response, self.name)


def completion_from(response: Any, provider: str = "anthropic") -> Completion:
    """Turn an SDK message into our Completion, honouring refusals."""
    stop_reason = getattr(response, "stop_reason", "") or ""
    model = getattr(response, "model", "") or ""

    # Always check stop_reason before reading content - a refusal has none.
    if stop_reason == "refusal":
        details = getattr(response, "stop_details", None)
        category = getattr(details, "category", None) or "unspecified"
        text = ("I had to decline that one (safety category: "
                f"{category}). Rephrase it or tell me what you're really after.")
    else:
        text = "".join(
            block.text for block in getattr(response, "content", [])
            if getattr(block, "type", "") == "text"
        ).strip()

    usage = getattr(response, "usage", None)
    input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
    output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
    cache_read = int(getattr(usage, "cache_read_input_tokens", 0) or 0)
    cache_write = int(getattr(usage, "cache_creation_input_tokens", 0) or 0)

    return Completion(
        text=text,
        model=model,
        provider=provider,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read,
        cache_write_tokens=cache_write,
        cost_usd=estimate_cost(model, input_tokens, output_tokens, cache_read, cache_write),
        stop_reason=stop_reason,
        raw=response,
    )


def _is_unsupported_param(exc: Exception) -> bool:
    """True when the SDK or API rejected an optional beta parameter."""
    if isinstance(exc, TypeError):
        return True
    text = str(exc).lower()
    markers = ("unexpected keyword", "unknown parameter", "unsupported", "fallbacks",
               "beta", "not supported", "invalid_request_error")
    return any(marker in text for marker in markers)


def _friendly_error(exc: Exception) -> str:
    """Plain-language API errors - JARVIS has to explain these out loud."""
    if anthropic is None:
        return str(exc)
    if isinstance(exc, anthropic.AuthenticationError):
        return ("My Claude API key was rejected. Check it in Settings, or run "
                "`jarvis setup` to paste a new one.")
    if isinstance(exc, anthropic.RateLimitError):
        return "Claude is rate-limiting me. I'll back off and retry shortly."
    if isinstance(exc, anthropic.NotFoundError):
        return ("That model name doesn't exist on your account. Check the model "
                "settings - `claude-opus-5` is the safe default.")
    if isinstance(exc, anthropic.APIConnectionError):
        return "I can't reach Claude - looks like a network problem on this machine."
    if isinstance(exc, anthropic.APIStatusError):
        return f"Claude returned an error ({exc.status_code}): {exc.message}"
    return f"Unexpected problem talking to Claude: {exc}"


async def _maybe_await(value: Any) -> None:
    if asyncio.iscoroutine(value):
        await value
