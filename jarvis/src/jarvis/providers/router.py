"""Model routing.

Phase 1 implements the *abstraction*, not a clever algorithm — per the brief,
the point now is that "which model handles this?" is a decision made in one
place with a stable signature, so Phase 6+ can make it smarter without
touching callers.

The mechanism is the one argued for in the Phase 0 audit: a request declares a
:class:`TaskClass` (and therefore a set of required capabilities); providers
declare what they support; the router filters to eligible providers and then
picks by preference order. A provider that cannot do the work is never
selected, rather than being handed it and silently degrading.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field

from jarvis.config import Settings
from jarvis.errors import NoEligibleProviderError
from jarvis.logging import get_logger
from jarvis.providers.base import AIProvider, ProviderCapability
from jarvis.providers.registry import ProviderRegistry

log = get_logger(__name__)


class TaskClass(str, enum.Enum):
    """What kind of work a model call represents."""

    #: Ordinary chat turn, possibly with tools.
    CONVERSATION = "CONVERSATION"
    #: Planning and hard multi-step reasoning.
    REASONING = "REASONING"
    #: High-volume, low-difficulty: classification, routing, extraction.
    FAST = "FAST"
    #: Anything that must return schema-valid JSON.
    STRUCTURED = "STRUCTURED"


@dataclass(frozen=True, slots=True)
class RoutingRequirements:
    required: frozenset[ProviderCapability]
    #: Consulted in order; the first configured provider that satisfies
    #: ``required`` wins. Empty means "any eligible provider".
    preferred_providers: tuple[str, ...] = ()


TASK_REQUIREMENTS: dict[TaskClass, RoutingRequirements] = {
    TaskClass.CONVERSATION: RoutingRequirements(
        required=frozenset({ProviderCapability.TEXT})
    ),
    TaskClass.REASONING: RoutingRequirements(
        required=frozenset({ProviderCapability.TEXT})
    ),
    TaskClass.FAST: RoutingRequirements(
        required=frozenset({ProviderCapability.TEXT})
    ),
    TaskClass.STRUCTURED: RoutingRequirements(
        required=frozenset(
            {ProviderCapability.TEXT, ProviderCapability.STRUCTURED_OUTPUT}
        )
    ),
}


@dataclass(slots=True)
class RoutingDecision:
    provider: AIProvider
    model: str
    task_class: TaskClass
    reason: str
    considered: list[str] = field(default_factory=list)

    def describe(self) -> dict[str, object]:
        return {
            "provider": self.provider.key,
            "model": self.model,
            "task_class": self.task_class.value,
            "reason": self.reason,
            "considered": self.considered,
        }


class ModelRouter:
    def __init__(self, registry: ProviderRegistry, settings: Settings) -> None:
        self.registry = registry
        self.settings = settings

    def _model_for(self, task_class: TaskClass) -> str:
        return {
            TaskClass.REASONING: self.settings.model_reasoning,
            TaskClass.CONVERSATION: self.settings.model_conversation,
            TaskClass.FAST: self.settings.model_fast,
            TaskClass.STRUCTURED: self.settings.model_conversation,
        }[task_class]

    def select(
        self,
        task_class: TaskClass = TaskClass.CONVERSATION,
        *,
        needs_tools: bool = False,
        needs_streaming: bool = False,
        provider_override: str | None = None,
        model_override: str | None = None,
    ) -> RoutingDecision:
        requirements = TASK_REQUIREMENTS[task_class]
        required = set(requirements.required)
        if needs_tools:
            required.add(ProviderCapability.TOOL_USE)
        if needs_streaming:
            required.add(ProviderCapability.STREAMING)
        required_fs = frozenset(required)

        # An explicit override still has to be able to do the work — silently
        # honouring an impossible override is how you get a confusing failure
        # three layers down instead of a clear one here.
        if provider_override:
            provider = self.registry.try_get(provider_override)
            if provider is None or not provider.is_configured():
                raise NoEligibleProviderError(
                    f"Provider '{provider_override}' is not available"
                )
            if not required_fs <= provider.capabilities:
                missing = sorted(c.value for c in required_fs - provider.capabilities)
                raise NoEligibleProviderError(
                    f"Provider '{provider_override}' lacks {missing}"
                )
            return RoutingDecision(
                provider=provider,
                model=model_override or self._resolve_model(provider, task_class),
                task_class=task_class,
                reason="explicit_override",
                considered=[provider.key],
            )

        eligible = self.registry.with_capabilities(required_fs)
        considered = [p.key for p in eligible]
        if not eligible:
            missing = sorted(c.value for c in required_fs)
            raise NoEligibleProviderError(
                f"No configured provider satisfies {missing}",
                details={"required": missing, "task_class": task_class.value},
            )

        order = (*requirements.preferred_providers, self.settings.default_provider)
        chosen = next(
            (p for key in order for p in eligible if p.key == key),
            eligible[0],
        )
        reason = (
            "preferred" if chosen.key in order else "first_eligible"
        )
        return RoutingDecision(
            provider=chosen,
            model=model_override or self._resolve_model(chosen, task_class),
            task_class=task_class,
            reason=reason,
            considered=considered,
        )

    def _resolve_model(self, provider: AIProvider, task_class: TaskClass) -> str:
        """Prefer the configured model for the class; fall back sensibly.

        The configured names are Anthropic model IDs, so they will not exist on
        another provider. Rather than passing a nonsense model through, fall
        back to that provider's default.
        """
        candidate = self._model_for(task_class)
        if candidate in provider.models:
            return candidate
        if provider.models:
            return provider.default_model
        return candidate
