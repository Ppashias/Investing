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


@dataclass(frozen=True, slots=True)
class RoutingConstraints:
    """What this call needs *beyond* being possible (Phase D, item 11).

    Capabilities answer "can this provider do the work". These answer "should
    it". Kept separate because the first is a hard filter — a provider without
    tool use cannot be handed tools — while these express preference and, in
    one case, a refusal.

    ``must_stay_local`` is the one that refuses. Everything else degrades to a
    preference, because routing to a slightly costlier model is a worse outcome
    than not answering; sending private text to a vendor is not.
    """

    #: The prompt may not leave this machine. If no local provider is eligible,
    #: the router raises rather than falling back — the entire point of asking
    #: is that the remote answer is unacceptable, so a silent downgrade would
    #: defeat it.
    must_stay_local: bool = False
    #: Prefer the cheapest eligible provider. A preference, not a cap: a
    #: cheaper provider that cannot do the work is still not selected.
    prefer_cheap: bool = False
    #: Roughly how much context this call needs. Providers whose declared
    #: window is smaller are dropped, because the failure is otherwise a
    #: truncation the model never mentions.
    min_context_tokens: int = 0


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
        needs_structured_output: bool = False,
        provider_override: str | None = None,
        model_override: str | None = None,
        constraints: RoutingConstraints | None = None,
    ) -> RoutingDecision:
        requirements = TASK_REQUIREMENTS[task_class]
        required = set(requirements.required)
        if needs_tools:
            required.add(ProviderCapability.TOOL_USE)
        if needs_streaming:
            required.add(ProviderCapability.STREAMING)
        if needs_structured_output:
            # The memory evaluator asks for JSON against a schema. A provider
            # that cannot honour one will return prose, which parses to
            # nothing — better to route elsewhere than to silently capture no
            # memories.
            required.add(ProviderCapability.STRUCTURED_OUTPUT)
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
        eligible = self._apply_constraints(eligible, constraints, task_class)
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

    def _apply_constraints(
        self,
        eligible: list[AIProvider],
        constraints: RoutingConstraints | None,
        task_class: TaskClass,
    ) -> list[AIProvider]:
        """Narrow, and order, but never widen.

        Returning an empty list is a legitimate answer: the caller's
        ``NoEligibleProviderError`` is the right outcome for "you asked for
        something private and nothing private is configured". Quietly ignoring
        the constraint would be the worse failure, and it would be invisible.
        """
        if constraints is None:
            return eligible

        if constraints.must_stay_local:
            eligible = [p for p in eligible if p.runs_locally]

        if constraints.min_context_tokens:
            kept = []
            for provider in eligible:
                model = self._resolve_model(provider, task_class)
                info = provider.models.get(model)
                window = getattr(info, "context_window", 0) or 0
                # An undeclared window is not treated as zero. A provider that
                # does not publish the number is usually fine; dropping it would
                # make this constraint quietly exclude most of the registry.
                if window == 0 or window >= constraints.min_context_tokens:
                    kept.append(provider)
            eligible = kept

        if constraints.prefer_cheap:
            eligible = sorted(eligible, key=self._cost_of)

        return eligible

    def _cost_of(self, provider: AIProvider) -> float:
        """Cheapest declared input price, or 0 for a local provider.

        Local is free at the margin — the electricity is already being spent —
        so it sorts first, which is also the privacy-preferring order. That
        alignment is a happy accident rather than a design goal, and is noted
        so nobody later "fixes" the tie-break and quietly changes where prompts
        go.
        """
        if provider.runs_locally:
            return 0.0
        prices = [
            getattr(info, "input_price_per_mtok", None)
            for info in provider.models.values()
        ]
        # 0.0 is the "not priced" default on ModelInfo, not a free model, so
        # it is excluded rather than treated as the cheapest thing available.
        real = [p for p in prices if isinstance(p, (int, float)) and p > 0]
        # Unknown price sorts last rather than first: guessing cheap about a
        # provider that never said is how a "prefer cheap" flag produces a bill.
        return min(real) if real else float("inf")

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
