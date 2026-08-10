"""Provider abstraction, retry policy, and routing."""

from __future__ import annotations

import asyncio

import pytest

from jarvis.config import Settings
from jarvis.errors import (
    NoEligibleProviderError,
    ProviderError,
    ProviderRateLimitError,
)
from jarvis.providers.base import (
    ChatMessage,
    CompletionRequest,
    ProviderCapability,
    StreamEnd,
    TextDelta,
    Usage,
)
from jarvis.providers.registry import ProviderRegistry
from jarvis.providers.retry import RetryPolicy
from jarvis.providers.router import ModelRouter, TaskClass
from tests.conftest import StubProvider, text_result


# ── the interface ────────────────────────────────────────────────────────────


async def test_provider_completes_and_reports_usage(stub: StubProvider) -> None:
    stub.responses = [text_result("hello")]
    result = await stub.complete(
        CompletionRequest(messages=[ChatMessage.user_text("hi")])
    )
    assert result.text() == "hello"
    assert result.stop_reason == "end_turn"
    assert result.usage.input_tokens == 10
    assert result.usage.cost_micros == 20


async def test_provider_streams_and_ends_with_result(stub: StubProvider) -> None:
    stub.responses = [text_result("streamed")]
    events = [
        e async for e in stub.stream(
            CompletionRequest(messages=[ChatMessage.user_text("hi")])
        )
    ]
    assert isinstance(events[-1], StreamEnd)
    assert "".join(e.text for e in events if isinstance(e, TextDelta)) == "streamed"


def test_usage_addition_accumulates() -> None:
    total = Usage(input_tokens=1, output_tokens=2, cost_micros=3) + Usage(
        input_tokens=10, output_tokens=20, cost_micros=30
    )
    assert (total.input_tokens, total.output_tokens, total.cost_micros) == (11, 22, 33)


def test_model_info_costs_are_integer_micros() -> None:
    from jarvis.providers.anthropic_provider import MODELS

    info = MODELS["claude-opus-5"]
    # 1M in at $5 + 1M out at $25 == $30 == 30_000_000 micros
    assert info.cost_micros(1_000_000, 1_000_000) == 30_000_000


def test_anthropic_provider_reports_unconfigured_without_key() -> None:
    from jarvis.providers.anthropic_provider import AnthropicProvider

    assert AnthropicProvider(api_key=None).is_configured() is False


# ── retry ────────────────────────────────────────────────────────────────────


async def test_retry_retries_retryable_and_succeeds() -> None:
    attempts = {"n": 0}

    async def flaky() -> str:
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise ProviderRateLimitError("slow down")
        return "ok"

    result = await RetryPolicy(max_attempts=3, base_delay=0.001).run(flaky)
    assert result == "ok"
    assert attempts["n"] == 3


async def test_retry_does_not_retry_non_retryable() -> None:
    attempts = {"n": 0}

    async def bad() -> str:
        attempts["n"] += 1
        raise ProviderError("permanent")

    with pytest.raises(ProviderError):
        await RetryPolicy(max_attempts=3, base_delay=0.001).run(bad)
    assert attempts["n"] == 1, "non-retryable errors must fail on the first attempt"


async def test_retry_gives_up_after_max_attempts() -> None:
    async def always_limited() -> str:
        raise ProviderRateLimitError("nope")

    with pytest.raises(ProviderRateLimitError):
        await RetryPolicy(max_attempts=2, base_delay=0.001).run(always_limited)


async def test_retry_does_not_swallow_cancellation() -> None:
    async def cancelled() -> str:
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await RetryPolicy(max_attempts=3, base_delay=0.001).run(cancelled)


def test_retry_honours_retry_after_over_backoff() -> None:
    policy = RetryPolicy(base_delay=10.0, max_delay=30.0, jitter=False)
    assert policy.delay_for(1, retry_after=2.5) == 2.5


# ── routing ──────────────────────────────────────────────────────────────────


def _router(*providers: StubProvider) -> ModelRouter:
    registry = ProviderRegistry()
    for p in providers:
        registry.register(p)
    return ModelRouter(registry, Settings(default_provider="stub"))


def test_router_selects_capable_provider(stub: StubProvider) -> None:
    decision = _router(stub).select(TaskClass.CONVERSATION, needs_tools=True)
    assert decision.provider.key == "stub"


def test_router_rejects_provider_lacking_capability() -> None:
    limited = StubProvider(capabilities=frozenset({ProviderCapability.TEXT}))
    with pytest.raises(NoEligibleProviderError):
        _router(limited).select(TaskClass.CONVERSATION, needs_tools=True)


def test_router_skips_unconfigured_provider() -> None:
    with pytest.raises(NoEligibleProviderError):
        _router(StubProvider(configured=False)).select(TaskClass.CONVERSATION)


def test_router_override_must_still_be_capable() -> None:
    limited = StubProvider(capabilities=frozenset({ProviderCapability.TEXT}))
    with pytest.raises(NoEligibleProviderError):
        _router(limited).select(
            TaskClass.CONVERSATION, needs_tools=True, provider_override="stub"
        )


def test_router_falls_back_to_provider_default_model(stub: StubProvider) -> None:
    """Configured model names are Anthropic ids; another provider must not be
    handed a model it does not have."""
    decision = _router(stub).select(TaskClass.REASONING)
    assert decision.model == "stub-model"


def test_router_structured_output_requires_capability() -> None:
    plain = StubProvider(capabilities=frozenset({ProviderCapability.TEXT}))
    with pytest.raises(NoEligibleProviderError):
        _router(plain).select(TaskClass.STRUCTURED)


def test_registry_reports_configured_only(stub: StubProvider) -> None:
    registry = ProviderRegistry()
    registry.register(stub)
    registry.register(StubProvider(configured=False))
    assert len(registry.all()) == 1  # same key overwrites
    assert registry.has_any_configured() is False  # last registration wins
