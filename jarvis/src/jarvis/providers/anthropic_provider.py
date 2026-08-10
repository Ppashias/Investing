"""Anthropic / Claude provider.

The only file in JARVIS that imports the Anthropic SDK.

Model IDs and pricing below are the current published values. They are data,
not behaviour — the router reads model choice from settings, so changing model
assignment never requires touching this file.

Notes on the current API surface that this implementation depends on:

* Adaptive thinking is on by default on Claude Opus 5; the ``thinking``
  parameter is therefore omitted rather than set. ``budget_tokens`` was removed
  on this model family and sending it is a 400.
* ``temperature`` / ``top_p`` / ``top_k`` are rejected on this model family, so
  no sampling parameters are sent. Behaviour is steered by prompting.
* Assistant-turn prefill is unsupported; structured output goes through
  ``output_config.format`` instead.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from typing import Any

from jarvis.errors import (
    ProviderAuthError,
    ProviderError,
    ProviderNotConfiguredError,
    ProviderOverloadedError,
    ProviderRateLimitError,
    ProviderRefusalError,
    ProviderResponseError,
    ProviderTimeoutError,
)
from jarvis.logging import get_logger, register_secret_value
from jarvis.providers.base import (
    AIProvider,
    ChatMessage,
    CompletionRequest,
    CompletionResult,
    ContentBlock,
    ImageBlock,
    ModelInfo,
    ProviderCapability,
    StopReason,
    StreamEnd,
    StreamEvent,
    StreamStart,
    TextBlock,
    TextDelta,
    ToolResultBlock,
    ToolUseBlock,
    ToolUseStart,
    Usage,
)
from jarvis.providers.retry import rate_limit_from_headers
from jarvis.secrets import Secret

log = get_logger(__name__)

_ALL = frozenset(
    {
        ProviderCapability.TEXT,
        ProviderCapability.STREAMING,
        ProviderCapability.TOOL_USE,
        ProviderCapability.STRUCTURED_OUTPUT,
        ProviderCapability.VISION,
        ProviderCapability.LONG_CONTEXT,
    }
)

MODELS: dict[str, ModelInfo] = {
    "claude-opus-5": ModelInfo(
        id="claude-opus-5",
        display_name="Claude Opus 5",
        context_window=1_000_000,
        max_output_tokens=128_000,
        capabilities=_ALL,
        input_price_per_mtok=5.0,
        output_price_per_mtok=25.0,
    ),
    "claude-sonnet-5": ModelInfo(
        id="claude-sonnet-5",
        display_name="Claude Sonnet 5",
        context_window=1_000_000,
        max_output_tokens=128_000,
        capabilities=_ALL,
        input_price_per_mtok=3.0,
        output_price_per_mtok=15.0,
    ),
    "claude-haiku-4-5": ModelInfo(
        id="claude-haiku-4-5",
        display_name="Claude Haiku 4.5",
        context_window=200_000,
        max_output_tokens=64_000,
        capabilities=_ALL - {ProviderCapability.LONG_CONTEXT},
        input_price_per_mtok=1.0,
        output_price_per_mtok=5.0,
    ),
}


class AnthropicProvider(AIProvider):
    key = "anthropic"
    display_name = "Anthropic (Claude)"

    def __init__(
        self,
        api_key: Secret | None,
        *,
        default_model: str = "claude-opus-5",
        timeout: float = 120.0,
    ) -> None:
        self._api_key = api_key
        self._default_model = default_model
        self._timeout = timeout
        self._client: Any = None
        if api_key:
            # So the value is scrubbed if it ever surfaces in a log line or an
            # exception message from deeper in the stack.
            register_secret_value(api_key.reveal())

    # ── metadata ─────────────────────────────────────────────────────────────

    @property
    def capabilities(self) -> frozenset[ProviderCapability]:
        return _ALL

    @property
    def models(self) -> dict[str, ModelInfo]:
        return MODELS

    @property
    def default_model(self) -> str:
        return self._default_model

    def is_configured(self) -> bool:
        return bool(self._api_key)

    # ── client ───────────────────────────────────────────────────────────────

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        if not self._api_key:
            raise ProviderNotConfiguredError(
                "Anthropic provider has no API key configured"
            )
        from anthropic import AsyncAnthropic

        self._client = AsyncAnthropic(
            api_key=self._api_key.reveal(),
            timeout=self._timeout,
            # Retry is handled by RetryPolicy so behaviour is uniform across
            # providers and observable in our own logs.
            max_retries=0,
        )
        return self._client

    # ── translation: JARVIS -> vendor ────────────────────────────────────────

    @staticmethod
    def _block_to_api(block: ContentBlock) -> dict[str, Any]:
        if isinstance(block, TextBlock):
            return {"type": "text", "text": block.text}
        if isinstance(block, ImageBlock):
            # Anthropic models in MODELS all declare VISION; the guard is here
            # so a future text-only entry cannot silently drop the image.
            return {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": block.media_type,
                    "data": block.data,
                },
            }
        if isinstance(block, ToolUseBlock):
            return {
                "type": "tool_use",
                "id": block.id,
                "name": block.name,
                "input": block.input,
            }
        if isinstance(block, ToolResultBlock):
            payload: dict[str, Any] = {
                "type": "tool_result",
                "tool_use_id": block.tool_use_id,
                "content": block.content,
            }
            if block.is_error:
                payload["is_error"] = True
            return payload
        raise ProviderError(f"Unsupported content block: {type(block).__name__}")

    def _messages_to_api(self, messages: list[ChatMessage]) -> list[dict[str, Any]]:
        return [
            {"role": m.role, "content": [self._block_to_api(b) for b in m.content]}
            for m in messages
        ]

    def _build_kwargs(self, request: CompletionRequest) -> dict[str, Any]:
        model = request.model or self._default_model
        info = MODELS.get(model)
        max_tokens = request.max_tokens
        if info:
            max_tokens = min(max_tokens, info.max_output_tokens)

        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": self._messages_to_api(request.messages),
        }
        if request.system:
            kwargs["system"] = request.system
        if request.tools:
            kwargs["tools"] = [
                {
                    "name": t.name,
                    "description": t.description,
                    "input_schema": t.input_schema,
                }
                for t in request.tools
            ]
        if request.stop_sequences:
            kwargs["stop_sequences"] = list(request.stop_sequences)
        if request.output_schema:
            kwargs["output_config"] = {
                "format": {"type": "json_schema", "schema": request.output_schema}
            }
        if effort := request.hints.get("effort"):
            kwargs.setdefault("output_config", {})["effort"] = effort
        return kwargs

    # ── translation: vendor -> JARVIS ────────────────────────────────────────

    @staticmethod
    def _api_to_blocks(content: Any) -> list[ContentBlock]:
        blocks: list[ContentBlock] = []
        for raw in content or []:
            btype = getattr(raw, "type", None)
            if btype == "text":
                blocks.append(TextBlock(text=raw.text))
            elif btype == "tool_use":
                blocks.append(
                    ToolUseBlock(
                        id=raw.id,
                        name=raw.name,
                        input=dict(raw.input) if raw.input else {},
                    )
                )
            # thinking / other block types are intentionally dropped: JARVIS
            # does not surface raw reasoning, and replaying them is a
            # same-model-only operation the router cannot guarantee.
        return blocks

    def _usage(self, raw: Any, model: str) -> Usage:
        if raw is None:
            return Usage()
        input_tokens = getattr(raw, "input_tokens", 0) or 0
        output_tokens = getattr(raw, "output_tokens", 0) or 0
        info = MODELS.get(model)
        return Usage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=getattr(raw, "cache_read_input_tokens", 0) or 0,
            cache_write_tokens=getattr(raw, "cache_creation_input_tokens", 0) or 0,
            cost_micros=info.cost_micros(input_tokens, output_tokens) if info else 0,
        )

    @staticmethod
    def _stop_reason(raw: str | None) -> StopReason:
        mapping: dict[str, StopReason] = {
            "end_turn": "end_turn",
            "tool_use": "tool_use",
            "max_tokens": "max_tokens",
            "stop_sequence": "stop_sequence",
            "refusal": "refusal",
        }
        return mapping.get(raw or "", "end_turn")

    # ── error mapping ────────────────────────────────────────────────────────

    def _translate_error(self, exc: Exception) -> Exception:
        """Map vendor exceptions onto the JARVIS taxonomy.

        Imported lazily and matched by class name so the mapping does not break
        if the SDK reorganises its exception module.
        """
        name = type(exc).__name__
        status = getattr(exc, "status_code", None)
        headers = None
        response = getattr(exc, "response", None)
        if response is not None:
            headers = dict(getattr(response, "headers", {}) or {})

        message = str(exc)

        if name in {"AuthenticationError", "PermissionDeniedError"} or status in (401, 403):
            return ProviderAuthError(f"Anthropic auth failed: {message}")
        if name == "RateLimitError" or status == 429:
            return ProviderRateLimitError(
                f"Anthropic rate limited: {message}",
                retry_after=rate_limit_from_headers(headers),
            )
        if name in {"APITimeoutError", "APIConnectionError"}:
            return ProviderTimeoutError(f"Anthropic unreachable: {message}")
        if status == 529 or name == "InternalServerError" or (status and status >= 500):
            return ProviderOverloadedError(f"Anthropic server error: {message}")
        if name == "BadRequestError" or status == 400:
            return ProviderError(f"Anthropic rejected the request: {message}")
        return ProviderError(f"Anthropic call failed ({name}): {message}")

    # ── completion ───────────────────────────────────────────────────────────

    async def complete(self, request: CompletionRequest) -> CompletionResult:
        client = self._get_client()
        kwargs = self._build_kwargs(request)
        started = time.perf_counter()
        try:
            response = await client.messages.create(**kwargs)
        except Exception as exc:
            raise self._translate_error(exc) from exc

        latency_ms = (time.perf_counter() - started) * 1000.0
        stop_reason = self._stop_reason(getattr(response, "stop_reason", None))
        model = getattr(response, "model", kwargs["model"])

        if stop_reason == "refusal":
            details = getattr(response, "stop_details", None)
            raise ProviderRefusalError(
                "Anthropic declined the request",
                details={"category": getattr(details, "category", None)},
            )

        blocks = self._api_to_blocks(getattr(response, "content", None))
        if not blocks and stop_reason != "max_tokens":
            raise ProviderResponseError("Anthropic returned no usable content")

        return CompletionResult(
            content=blocks,
            stop_reason=stop_reason,
            model=model,
            provider=self.key,
            usage=self._usage(getattr(response, "usage", None), model),
            latency_ms=latency_ms,
            raw_meta={"id": getattr(response, "id", None)},
        )

    # ── streaming ────────────────────────────────────────────────────────────

    async def stream(  # type: ignore[override]
        self, request: CompletionRequest
    ) -> AsyncIterator[StreamEvent]:
        client = self._get_client()
        kwargs = self._build_kwargs(request)
        model = kwargs["model"]
        started = time.perf_counter()

        yield StreamStart(model=model, provider=self.key)

        try:
            async with client.messages.stream(**kwargs) as stream:
                async for event in stream:
                    etype = getattr(event, "type", None)
                    if etype == "content_block_delta":
                        delta = getattr(event, "delta", None)
                        if getattr(delta, "type", None) == "text_delta":
                            yield TextDelta(text=delta.text)
                    elif etype == "content_block_start":
                        block = getattr(event, "content_block", None)
                        if getattr(block, "type", None) == "tool_use":
                            yield ToolUseStart(id=block.id, name=block.name)

                final = await stream.get_final_message()
        except Exception as exc:
            raise self._translate_error(exc) from exc

        latency_ms = (time.perf_counter() - started) * 1000.0
        stop_reason = self._stop_reason(getattr(final, "stop_reason", None))
        if stop_reason == "refusal":
            raise ProviderRefusalError("Anthropic declined the request")

        final_model = getattr(final, "model", model)
        yield StreamEnd(
            result=CompletionResult(
                content=self._api_to_blocks(getattr(final, "content", None)),
                stop_reason=stop_reason,
                model=final_model,
                provider=self.key,
                usage=self._usage(getattr(final, "usage", None), final_model),
                latency_ms=latency_ms,
                raw_meta={"id": getattr(final, "id", None)},
            )
        )
