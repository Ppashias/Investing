"""Provider-neutral AI interface.

Nothing outside ``jarvis.providers`` may import a vendor SDK. The rest of
JARVIS speaks only the types in this module, so swapping or adding a provider
touches one package.

The abstraction is over **task requirements**, not over a lowest common
denominator of vendor features (Phase 0 audit §14.6). A provider *declares*
what it supports via :class:`ProviderCapability`; the router filters to
providers that satisfy a request's requirements. A provider that cannot stream
or cannot call tools is simply never selected for work that needs those,
rather than being papered over with a degraded shim.
"""

from __future__ import annotations

import abc
import enum
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

# ── capabilities ─────────────────────────────────────────────────────────────


class ProviderCapability(str, enum.Enum):
    TEXT = "TEXT"
    STREAMING = "STREAMING"
    TOOL_USE = "TOOL_USE"
    STRUCTURED_OUTPUT = "STRUCTURED_OUTPUT"
    VISION = "VISION"
    LONG_CONTEXT = "LONG_CONTEXT"
    #: Reserved for Phase 4. Declared here so the router can already filter on
    #: it and Phase 4 does not need to reshape the interface.
    COMPUTER_USE = "COMPUTER_USE"


# ── content blocks ───────────────────────────────────────────────────────────


@dataclass(slots=True)
class TextBlock:
    text: str
    type: Literal["text"] = "text"


@dataclass(slots=True)
class ToolUseBlock:
    id: str
    name: str
    input: dict[str, Any]
    type: Literal["tool_use"] = "tool_use"


@dataclass(slots=True)
class ToolResultBlock:
    tool_use_id: str
    content: str
    is_error: bool = False
    type: Literal["tool_result"] = "tool_result"


ContentBlock = TextBlock | ToolUseBlock | ToolResultBlock


@dataclass(slots=True)
class ChatMessage:
    role: Literal["user", "assistant"]
    content: list[ContentBlock]

    @classmethod
    def user_text(cls, text: str) -> "ChatMessage":
        return cls(role="user", content=[TextBlock(text=text)])

    @classmethod
    def assistant_text(cls, text: str) -> "ChatMessage":
        return cls(role="assistant", content=[TextBlock(text=text)])

    def text(self) -> str:
        return "".join(b.text for b in self.content if isinstance(b, TextBlock))

    def tool_uses(self) -> list[ToolUseBlock]:
        return [b for b in self.content if isinstance(b, ToolUseBlock)]


# ── tool schema ──────────────────────────────────────────────────────────────


@dataclass(slots=True)
class ToolSpec:
    """The provider-facing view of a tool: name, description, JSON Schema.

    Deliberately decoupled from :class:`jarvis.tools.base.Tool`, which also
    carries permission metadata and a handler. Providers must never see those.
    """

    name: str
    description: str
    input_schema: dict[str, Any]


# ── requests and results ─────────────────────────────────────────────────────


@dataclass(slots=True)
class CompletionRequest:
    messages: list[ChatMessage]
    system: str | None = None
    model: str | None = None
    max_tokens: int = 4096
    tools: Sequence[ToolSpec] = field(default_factory=tuple)
    #: JSON Schema for enforced structured output, when the provider supports it.
    output_schema: dict[str, Any] | None = None
    stop_sequences: Sequence[str] = field(default_factory=tuple)
    #: Opaque per-provider hints (effort, thinking config, ...). Providers
    #: ignore keys they do not understand rather than erroring, so a hint
    #: meant for one vendor does not break another.
    hints: dict[str, Any] = field(default_factory=dict)
    request_id: str | None = None


@dataclass(slots=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    #: Millionths of a currency unit — integer arithmetic avoids float drift
    #: when these are summed across thousands of calls.
    cost_micros: int = 0

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
            cache_write_tokens=self.cache_write_tokens + other.cache_write_tokens,
            cost_micros=self.cost_micros + other.cost_micros,
        )


StopReason = Literal[
    "end_turn", "tool_use", "max_tokens", "stop_sequence", "refusal", "error"
]


@dataclass(slots=True)
class CompletionResult:
    content: list[ContentBlock]
    stop_reason: StopReason
    model: str
    provider: str
    usage: Usage = field(default_factory=Usage)
    latency_ms: float = 0.0
    raw_meta: dict[str, Any] = field(default_factory=dict)

    def text(self) -> str:
        return "".join(b.text for b in self.content if isinstance(b, TextBlock))

    def tool_uses(self) -> list[ToolUseBlock]:
        return [b for b in self.content if isinstance(b, ToolUseBlock)]

    def to_message(self) -> ChatMessage:
        return ChatMessage(role="assistant", content=list(self.content))


# ── streaming ────────────────────────────────────────────────────────────────


@dataclass(slots=True)
class StreamStart:
    model: str
    provider: str
    type: Literal["start"] = "start"


@dataclass(slots=True)
class TextDelta:
    text: str
    type: Literal["text_delta"] = "text_delta"


@dataclass(slots=True)
class ToolUseStart:
    id: str
    name: str
    type: Literal["tool_use_start"] = "tool_use_start"


@dataclass(slots=True)
class StreamEnd:
    result: CompletionResult
    type: Literal["end"] = "end"


StreamEvent = StreamStart | TextDelta | ToolUseStart | StreamEnd


# ── model metadata ───────────────────────────────────────────────────────────


@dataclass(slots=True)
class ModelInfo:
    id: str
    display_name: str
    context_window: int
    max_output_tokens: int
    capabilities: frozenset[ProviderCapability]
    #: Currency units per million tokens; used to derive ``cost_micros``.
    input_price_per_mtok: float = 0.0
    output_price_per_mtok: float = 0.0

    def cost_micros(self, input_tokens: int, output_tokens: int) -> int:
        dollars = (
            input_tokens * self.input_price_per_mtok
            + output_tokens * self.output_price_per_mtok
        ) / 1_000_000
        return int(round(dollars * 1_000_000))


# ── the interface ────────────────────────────────────────────────────────────


class AIProvider(abc.ABC):
    """What every provider implements.

    Implementations are responsible for translating vendor errors into the
    :mod:`jarvis.errors` taxonomy. Retry policy lives in
    :class:`jarvis.providers.retry.RetryPolicy` and wraps the provider, so an
    implementation should raise rather than retry internally.
    """

    #: Stable key used in config, logs, and the database.
    key: str = "abstract"
    display_name: str = "Abstract provider"

    @property
    @abc.abstractmethod
    def capabilities(self) -> frozenset[ProviderCapability]: ...

    @property
    @abc.abstractmethod
    def models(self) -> dict[str, ModelInfo]: ...

    @property
    @abc.abstractmethod
    def default_model(self) -> str: ...

    @abc.abstractmethod
    def is_configured(self) -> bool:
        """True when credentials are present. Never raises, never logs a key."""

    @abc.abstractmethod
    async def complete(self, request: CompletionRequest) -> CompletionResult: ...

    @abc.abstractmethod
    def stream(self, request: CompletionRequest) -> AsyncIterator[StreamEvent]:
        """Yield incremental events, terminating with a :class:`StreamEnd`.

        Providers without :attr:`ProviderCapability.STREAMING` should still
        implement this by emitting one ``StreamEnd`` around :meth:`complete`,
        so callers need no special case; the router simply will not choose them
        when true incremental output is required.
        """

    def supports(self, capability: ProviderCapability) -> bool:
        return capability in self.capabilities

    def model_info(self, model: str | None) -> ModelInfo | None:
        return self.models.get(model or self.default_model)

    def describe(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "display_name": self.display_name,
            "configured": self.is_configured(),
            "default_model": self.default_model,
            "capabilities": sorted(c.value for c in self.capabilities),
            "models": sorted(self.models),
        }
