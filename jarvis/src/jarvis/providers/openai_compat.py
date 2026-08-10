"""OpenAI-compatible provider.

One implementation covers OpenAI itself and every local runtime that speaks the
same wire format — Ollama, llama.cpp's ``llama-server``, LM Studio, and vLLM
all expose ``/v1/chat/completions``. Only ``base_url`` changes, which is why
"support local models" costs one adapter rather than four.

Implemented against the HTTP API with ``httpx`` rather than the vendor SDK:
the surface used here is small and stable, and it avoids taking a dependency
whose main value would be features this adapter does not use.

Capabilities are constructor-supplied because they genuinely differ by
endpoint — a 3B model on Ollama does not reliably do tool calling, and
declaring otherwise would make the router route work it cannot do.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx

from jarvis.errors import (
    ProviderAuthError,
    ProviderError,
    ProviderNotConfiguredError,
    ProviderOverloadedError,
    ProviderRateLimitError,
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

DEFAULT_CAPABILITIES = frozenset(
    {
        ProviderCapability.TEXT,
        ProviderCapability.STREAMING,
        ProviderCapability.TOOL_USE,
        ProviderCapability.STRUCTURED_OUTPUT,
    }
)


class OpenAICompatProvider(AIProvider):
    def __init__(
        self,
        *,
        key: str = "openai",
        display_name: str = "OpenAI-compatible",
        base_url: str = "https://api.openai.com/v1",
        api_key: Secret | None = None,
        models: dict[str, ModelInfo] | None = None,
        default_model: str = "gpt-4o-mini",
        capabilities: frozenset[ProviderCapability] = DEFAULT_CAPABILITIES,
        timeout: float = 120.0,
        #: Local runtimes accept any key (or none). Marking a provider as not
        #: requiring auth lets it report itself configured without a secret.
        requires_api_key: bool = True,
    ) -> None:
        self.key = key
        self.display_name = display_name
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._models = models or {}
        self._default_model = default_model
        self._capabilities = capabilities
        self._timeout = timeout
        self._requires_api_key = requires_api_key
        self._client: httpx.AsyncClient | None = None
        if api_key:
            register_secret_value(api_key.reveal())

    # ── metadata ─────────────────────────────────────────────────────────────

    @property
    def capabilities(self) -> frozenset[ProviderCapability]:
        return self._capabilities

    @property
    def models(self) -> dict[str, ModelInfo]:
        return self._models

    @property
    def default_model(self) -> str:
        return self._default_model

    def is_configured(self) -> bool:
        if not self._requires_api_key:
            return bool(self._base_url)
        return bool(self._api_key)

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is not None:
            return self._client
        if not self.is_configured():
            raise ProviderNotConfiguredError(f"{self.display_name} is not configured")
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key.reveal()}"
        self._client = httpx.AsyncClient(
            base_url=self._base_url, headers=headers, timeout=self._timeout
        )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ── translation: JARVIS -> wire ──────────────────────────────────────────

    def _messages_to_api(
        self, messages: list[ChatMessage], system: str | None
    ) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        if system:
            out.append({"role": "system", "content": system})

        for msg in messages:
            text_parts: list[str] = []
            tool_calls: list[dict[str, Any]] = []
            # Tool results are separate top-level messages in this format, not
            # blocks inside a user turn — so they are emitted before the turn
            # they belong to.
            tool_results: list[dict[str, Any]] = []

            for block in msg.content:
                if isinstance(block, TextBlock):
                    text_parts.append(block.text)
                elif isinstance(block, ToolUseBlock):
                    tool_calls.append(
                        {
                            "id": block.id,
                            "type": "function",
                            "function": {
                                "name": block.name,
                                "arguments": json.dumps(block.input),
                            },
                        }
                    )
                elif isinstance(block, ToolResultBlock):
                    tool_results.append(
                        {
                            "role": "tool",
                            "tool_call_id": block.tool_use_id,
                            "content": block.content,
                        }
                    )

            out.extend(tool_results)
            if text_parts or tool_calls:
                entry: dict[str, Any] = {"role": msg.role}
                entry["content"] = "".join(text_parts) if text_parts else None
                if tool_calls:
                    entry["tool_calls"] = tool_calls
                out.append(entry)
        return out

    def _build_payload(self, request: CompletionRequest, *, stream: bool) -> dict[str, Any]:
        model = request.model or self._default_model
        payload: dict[str, Any] = {
            "model": model,
            "messages": self._messages_to_api(request.messages, request.system),
            "max_tokens": request.max_tokens,
            "stream": stream,
        }
        if request.tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.input_schema,
                    },
                }
                for t in request.tools
            ]
        if request.stop_sequences:
            payload["stop"] = list(request.stop_sequences)
        if request.output_schema:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "response",
                    "schema": request.output_schema,
                    "strict": True,
                },
            }
        if stream:
            payload["stream_options"] = {"include_usage": True}
        return payload

    # ── translation: wire -> JARVIS ──────────────────────────────────────────

    @staticmethod
    def _parse_tool_arguments(raw: str | None) -> dict[str, Any]:
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            # Surface as a tool input error downstream rather than crashing the
            # provider; the executor's schema validation reports it usefully.
            return {"__unparsed_arguments__": raw}
        return parsed if isinstance(parsed, dict) else {"value": parsed}

    def _choice_to_blocks(self, message: dict[str, Any]) -> list[ContentBlock]:
        blocks: list[ContentBlock] = []
        if content := message.get("content"):
            blocks.append(TextBlock(text=content))
        for call in message.get("tool_calls") or []:
            fn = call.get("function", {})
            blocks.append(
                ToolUseBlock(
                    id=call.get("id") or f"call_{uuid.uuid4().hex[:12]}",
                    name=fn.get("name", ""),
                    input=self._parse_tool_arguments(fn.get("arguments")),
                )
            )
        return blocks

    @staticmethod
    def _stop_reason(raw: str | None) -> StopReason:
        return {
            "stop": "end_turn",
            "tool_calls": "tool_use",
            "function_call": "tool_use",
            "length": "max_tokens",
            "content_filter": "refusal",
        }.get(raw or "", "end_turn")

    def _usage(self, raw: dict[str, Any] | None, model: str) -> Usage:
        if not raw:
            return Usage()
        input_tokens = raw.get("prompt_tokens", 0) or 0
        output_tokens = raw.get("completion_tokens", 0) or 0
        info = self._models.get(model)
        return Usage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_micros=info.cost_micros(input_tokens, output_tokens) if info else 0,
        )

    # ── error mapping ────────────────────────────────────────────────────────

    def _translate_http_error(self, exc: Exception) -> Exception:
        if isinstance(exc, httpx.TimeoutException):
            return ProviderTimeoutError(f"{self.display_name} timed out")
        if isinstance(exc, httpx.HTTPStatusError):
            status = exc.response.status_code
            body = exc.response.text[:500]
            if status in (401, 403):
                return ProviderAuthError(f"{self.display_name} rejected credentials")
            if status == 429:
                return ProviderRateLimitError(
                    f"{self.display_name} rate limited",
                    retry_after=rate_limit_from_headers(dict(exc.response.headers)),
                )
            if status >= 500:
                return ProviderOverloadedError(
                    f"{self.display_name} server error {status}: {body}"
                )
            return ProviderError(f"{self.display_name} error {status}: {body}")
        if isinstance(exc, httpx.HTTPError):
            return ProviderTimeoutError(f"{self.display_name} unreachable: {exc}")
        return ProviderError(f"{self.display_name} call failed: {exc}")

    # ── completion ───────────────────────────────────────────────────────────

    async def complete(self, request: CompletionRequest) -> CompletionResult:
        client = self._get_client()
        payload = self._build_payload(request, stream=False)
        started = time.perf_counter()
        try:
            response = await client.post("/chat/completions", json=payload)
            response.raise_for_status()
            data = response.json()
        except Exception as exc:
            raise self._translate_http_error(exc) from exc

        latency_ms = (time.perf_counter() - started) * 1000.0
        choices = data.get("choices") or []
        if not choices:
            raise ProviderResponseError(f"{self.display_name} returned no choices")

        choice = choices[0]
        model = data.get("model", payload["model"])
        return CompletionResult(
            content=self._choice_to_blocks(choice.get("message", {})),
            stop_reason=self._stop_reason(choice.get("finish_reason")),
            model=model,
            provider=self.key,
            usage=self._usage(data.get("usage"), model),
            latency_ms=latency_ms,
            raw_meta={"id": data.get("id")},
        )

    # ── streaming ────────────────────────────────────────────────────────────

    async def stream(  # type: ignore[override]
        self, request: CompletionRequest
    ) -> AsyncIterator[StreamEvent]:
        client = self._get_client()
        payload = self._build_payload(request, stream=True)
        model = payload["model"]
        started = time.perf_counter()

        yield StreamStart(model=model, provider=self.key)

        text_parts: list[str] = []
        # Tool call fragments arrive spread across deltas, keyed by index.
        tool_acc: dict[int, dict[str, Any]] = {}
        announced: set[int] = set()
        finish_reason: str | None = None
        usage_raw: dict[str, Any] | None = None

        try:
            async with client.stream("POST", "/chat/completions", json=payload) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    data_str = line[5:].strip()
                    if data_str == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue

                    if chunk.get("usage"):
                        usage_raw = chunk["usage"]
                    for choice in chunk.get("choices") or []:
                        if choice.get("finish_reason"):
                            finish_reason = choice["finish_reason"]
                        delta = choice.get("delta") or {}
                        if content := delta.get("content"):
                            text_parts.append(content)
                            yield TextDelta(text=content)
                        for call in delta.get("tool_calls") or []:
                            idx = call.get("index", 0)
                            slot = tool_acc.setdefault(
                                idx, {"id": None, "name": "", "arguments": ""}
                            )
                            if call.get("id"):
                                slot["id"] = call["id"]
                            fn = call.get("function") or {}
                            if fn.get("name"):
                                slot["name"] = fn["name"]
                            if fn.get("arguments"):
                                slot["arguments"] += fn["arguments"]
                            if slot["name"] and idx not in announced:
                                announced.add(idx)
                                yield ToolUseStart(
                                    id=slot["id"] or f"call_{idx}", name=slot["name"]
                                )
        except Exception as exc:
            raise self._translate_http_error(exc) from exc

        blocks: list[ContentBlock] = []
        if text_parts:
            blocks.append(TextBlock(text="".join(text_parts)))
        for idx in sorted(tool_acc):
            slot = tool_acc[idx]
            blocks.append(
                ToolUseBlock(
                    id=slot["id"] or f"call_{idx}",
                    name=slot["name"],
                    input=self._parse_tool_arguments(slot["arguments"]),
                )
            )

        yield StreamEnd(
            result=CompletionResult(
                content=blocks,
                stop_reason=self._stop_reason(finish_reason),
                model=model,
                provider=self.key,
                usage=self._usage(usage_raw, model),
                latency_ms=(time.perf_counter() - started) * 1000.0,
            )
        )


def local_provider(
    *,
    base_url: str,
    default_model: str,
    key: str = "local",
    display_name: str = "Local model",
    capabilities: frozenset[ProviderCapability] = frozenset(
        {ProviderCapability.TEXT, ProviderCapability.STREAMING}
    ),
) -> OpenAICompatProvider:
    """Factory for a local runtime.

    Defaults to text + streaming only. Small local models are unreliable at
    tool calling, and the router must not send them work they will mangle;
    pass a wider capability set explicitly when the deployed model earns it.
    """
    return OpenAICompatProvider(
        key=key,
        display_name=display_name,
        base_url=base_url,
        api_key=None,
        default_model=default_model,
        capabilities=capabilities,
        requires_api_key=False,
    )
