"""Provider abstraction. Nothing outside this package imports a vendor SDK."""

from jarvis.providers.base import (
    AIProvider,
    ChatMessage,
    CompletionRequest,
    CompletionResult,
    ContentBlock,
    ModelInfo,
    ProviderCapability,
    StreamEnd,
    StreamEvent,
    StreamStart,
    TextBlock,
    TextDelta,
    ToolResultBlock,
    ToolSpec,
    ToolUseBlock,
    ToolUseStart,
    Usage,
)
from jarvis.providers.registry import ProviderRegistry, build_registry
from jarvis.providers.retry import DEFAULT_RETRY_POLICY, RetryPolicy
from jarvis.providers.router import ModelRouter, RoutingDecision, TaskClass

__all__ = [
    "AIProvider", "ChatMessage", "CompletionRequest", "CompletionResult",
    "ContentBlock", "ModelInfo", "ProviderCapability", "StreamEnd", "StreamEvent",
    "StreamStart", "TextBlock", "TextDelta", "ToolResultBlock", "ToolSpec",
    "ToolUseBlock", "ToolUseStart", "Usage", "ProviderRegistry", "build_registry",
    "RetryPolicy", "DEFAULT_RETRY_POLICY", "ModelRouter", "RoutingDecision", "TaskClass",
]
