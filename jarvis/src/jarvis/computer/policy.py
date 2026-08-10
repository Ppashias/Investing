"""Computer policy engine (§13, §15, §16).

Every computer action passes through :meth:`ComputerPolicyEngine.evaluate`.
There is no bypass: :class:`~jarvis.computer.executor.ActionExecutor` is the
only thing that reaches a backend, and it cannot run an action without a
decision object.

## How this relates to the Phase 1 permission engine

It does not replace it — it *narrows* it. The Phase 1 engine answers "may this
user perform this capability on this resource?" and already implements the
fail-safe rules (defaults deny upward, irreversibility floors ALLOW to ASK,
taint escalation). This layer adds the three things computer control needs and
conversation did not:

1. **A mode ceiling** (§15). ``SAFE`` caps everything that changes the machine
   at ``ASK`` no matter what grants exist; ``LOCKDOWN`` denies outright.
2. **Scope gating** (§16). A scope the user has not enabled is denied before
   risk is even considered, so "I turned on screen reading" never
   accidentally means "and typing".
3. **Risk gating** (§12). ``HIGH`` always asks. ``PROHIBITED`` always denies.

The order matters and is deliberately most-restrictive-first: a decision can
only be lowered as it passes through, never raised. Every rule that fires is
recorded on the decision, so the audit log says *why*, not just *what*.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from jarvis.computer.capabilities import CapabilityReport
from jarvis.computer.risk import RiskAssessment, classify_risk
from jarvis.computer.types import (
    ActionRisk,
    ComputerAction,
    ComputerMode,
    ComputerScope,
)
from jarvis.db.models import Capability, PermissionMode, RiskLevel
from jarvis.logging import get_logger

log = get_logger(__name__)

#: Scopes enabled for a fresh install. Observation only — everything that can
#: change the machine starts off, and turning one on is a deliberate act.
DEFAULT_ENABLED_SCOPES: frozenset[ComputerScope] = frozenset(
    {ComputerScope.SCREEN, ComputerScope.WINDOW}
)

#: Scopes that cannot be enabled in Phase 3 at all (§43). Not "off by default"
#: — absent. Autonomous money movement and autonomous outbound communication
#: need safety architecture that does not exist yet, and a config flag is not
#: that architecture.
PHASE3_FORBIDDEN_SCOPES: frozenset[ComputerScope] = frozenset(
    {
        ComputerScope.FINANCIAL,
        ComputerScope.COMMUNICATION,
        ComputerScope.SYSTEM_SETTINGS,
    }
)

#: The highest risk each mode may auto-allow. Anything above becomes ``ASK``.
_MODE_CEILING: dict[ComputerMode, ActionRisk | None] = {
    ComputerMode.LOCKDOWN: None,          # nothing, not even observation
    ComputerMode.SAFE: ActionRisk.LOW,    # observe freely, ask to change
    ComputerMode.ASSISTED: ActionRisk.LOW,
    ComputerMode.AUTONOMOUS: ActionRisk.MEDIUM,
}

#: Capability mapping into the Phase 1 engine, so computer actions are subject
#: to the same grants, expiry and taint rules as every other tool.
_SCOPE_CAPABILITY: dict[ComputerScope, Capability] = {
    ComputerScope.SCREEN: Capability.READ,
    ComputerScope.WINDOW: Capability.READ,
    ComputerScope.MOUSE: Capability.EXECUTE,
    ComputerScope.KEYBOARD: Capability.EXECUTE,
    ComputerScope.APPLICATION: Capability.EXECUTE,
    ComputerScope.FILESYSTEM: Capability.WRITE,
    ComputerScope.TERMINAL: Capability.EXECUTE,
    ComputerScope.CLIPBOARD: Capability.READ,
    ComputerScope.NETWORK: Capability.EXTERNAL_ACTION,
    ComputerScope.BROWSER: Capability.EXECUTE,
    ComputerScope.COMMUNICATION: Capability.SENSITIVE_ACTION,
    ComputerScope.FINANCIAL: Capability.SENSITIVE_ACTION,
    ComputerScope.SYSTEM_SETTINGS: Capability.SENSITIVE_ACTION,
}

_RISK_TO_CORE: dict[ActionRisk, RiskLevel] = {
    ActionRisk.LOW: RiskLevel.LOW,
    ActionRisk.MEDIUM: RiskLevel.MEDIUM,
    ActionRisk.HIGH: RiskLevel.HIGH,
    ActionRisk.PROHIBITED: RiskLevel.CRITICAL,
}


@dataclass(slots=True)
class PolicyDecision:
    mode: PermissionMode
    reason: str
    risk: ActionRisk
    scope: ComputerScope
    irreversible: bool = False
    applied_rules: list[str] = field(default_factory=list)
    #: Set when the action cannot run here at all, regardless of permission.
    unavailable_reason: str | None = None

    @property
    def allowed(self) -> bool:
        return self.mode is PermissionMode.ALLOW

    @property
    def denied(self) -> bool:
        return self.mode is PermissionMode.DENY

    @property
    def needs_confirmation(self) -> bool:
        return self.mode is PermissionMode.ASK

    def describe(self) -> dict[str, Any]:
        return {
            "decision": self.mode.value,
            "reason": self.reason,
            "risk": self.risk.value,
            "scope": self.scope.value,
            "irreversible": self.irreversible,
            "rules": self.applied_rules,
            "unavailable_reason": self.unavailable_reason,
        }


@dataclass(slots=True)
class ComputerPolicy:
    """The operator's configuration. Persisted on the user row."""

    mode: ComputerMode = ComputerMode.SAFE
    enabled_scopes: frozenset[ComputerScope] = DEFAULT_ENABLED_SCOPES
    #: Scopes the user has said may act without confirmation, still subject to
    #: the mode ceiling and the risk rules.
    auto_scopes: frozenset[ComputerScope] = frozenset()

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode.value,
            "enabled_scopes": sorted(s.value for s in self.enabled_scopes),
            "auto_scopes": sorted(s.value for s in self.auto_scopes),
            "available_scopes": sorted(
                s.value for s in ComputerScope if s not in PHASE3_FORBIDDEN_SCOPES
            ),
            "forbidden_scopes": sorted(s.value for s in PHASE3_FORBIDDEN_SCOPES),
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> "ComputerPolicy":
        if not raw:
            return cls()

        def parse(key: str, default: frozenset[ComputerScope]) -> frozenset[ComputerScope]:
            values = raw.get(key)
            if values is None:
                return default
            out = set()
            for value in values:
                try:
                    scope = ComputerScope(value)
                except ValueError:
                    continue
                # A forbidden scope cannot be enabled by editing stored
                # configuration; it is filtered on the way in as well as on
                # the way out.
                if scope not in PHASE3_FORBIDDEN_SCOPES:
                    out.add(scope)
            return frozenset(out)

        try:
            mode = ComputerMode(raw.get("mode", ComputerMode.SAFE.value))
        except ValueError:
            mode = ComputerMode.SAFE

        return cls(
            mode=mode,
            enabled_scopes=parse("enabled_scopes", DEFAULT_ENABLED_SCOPES),
            auto_scopes=parse("auto_scopes", frozenset()),
        )


class ComputerPolicyEngine:
    def __init__(
        self,
        session: AsyncSession,
        *,
        capabilities: CapabilityReport,
        policy: ComputerPolicy | None = None,
    ) -> None:
        self.session = session
        self.capabilities = capabilities
        self.policy = policy or ComputerPolicy()

    async def evaluate(
        self, action: ComputerAction, *, user_id: str
    ) -> PolicyDecision:
        """The single decision point. Most restrictive rule wins."""
        assessment: RiskAssessment = classify_risk(action)
        scope = action.scope
        rules: list[str] = [f"risk({assessment.risk.value})"]

        def decide(mode: PermissionMode, reason: str, rule: str) -> PolicyDecision:
            rules.append(rule)
            return PolicyDecision(
                mode=mode,
                reason=reason,
                risk=assessment.risk,
                scope=scope,
                irreversible=assessment.irreversible,
                applied_rules=rules,
                unavailable_reason=self.capabilities.reason_unavailable(action.kind),
            )

        # 1. Prohibited content. Nothing below can rescue it, so it is first.
        if assessment.risk is ActionRisk.PROHIBITED:
            return decide(PermissionMode.DENY, assessment.reason, "prohibited")

        # 2. Scopes that do not exist in Phase 3 (§43).
        if scope in PHASE3_FORBIDDEN_SCOPES:
            return decide(
                PermissionMode.DENY,
                f"{scope.value} actions are not implemented in this phase and "
                "cannot be enabled.",
                "phase_forbidden_scope",
            )

        # 3. Lockdown (§15).
        if self.policy.mode is ComputerMode.LOCKDOWN:
            return decide(
                PermissionMode.DENY,
                "Computer control is in LOCKDOWN.",
                "lockdown",
            )

        # 4. Scope not enabled by the user (§16).
        if scope not in self.policy.enabled_scopes:
            return decide(
                PermissionMode.DENY,
                f"The {scope.value} scope is not enabled. Enable it in the "
                "Computer panel to allow this.",
                "scope_disabled",
            )

        # 5. Capability unavailable on this machine (§2, §3). Denied rather
        #    than asked: no confirmation makes a missing display appear.
        unavailable = self.capabilities.reason_unavailable(action.kind)
        if unavailable:
            return decide(PermissionMode.DENY, unavailable, "capability_unavailable")

        # 6. The Phase 1 engine — grants, expiry, revocation, taint, and the
        #    irreversibility floor, all of which already work and are tested.
        core = await self._core_decision(action, assessment, user_id)
        rules.extend(core.applied_rules)
        if core.mode is PermissionMode.DENY:
            return decide(PermissionMode.DENY, core.reason, "core_engine_deny")
        if core.mode is PermissionMode.ASK:
            # The core engine's ASK is as binding as its DENY. Dropping it
            # would discard the rules Phase 1 already enforces — grant
            # expiry, risk ceilings on a grant, and taint escalation — and
            # this layer would silently become the only one that matters.
            return decide(PermissionMode.ASK, core.reason, "core_engine_ask")

        # 7. HIGH always meets a human (§12, §14).
        if assessment.risk is ActionRisk.HIGH:
            return decide(
                PermissionMode.ASK,
                assessment.reason or "High-risk action.",
                "high_risk_requires_confirmation",
            )

        # 8. Irreversible actions are never automatic, whatever the mode.
        if assessment.irreversible:
            return decide(
                PermissionMode.ASK,
                "This cannot be undone.",
                "irreversible_floor",
            )

        # 9. Untrusted content in scope (§32). Content from a webpage or a
        #    document must never silently drive the machine.
        if action.tainted:
            return decide(
                PermissionMode.ASK,
                "This action was influenced by untrusted content.",
                "taint_escalation",
            )

        # 10. The mode ceiling (§15).
        ceiling = _MODE_CEILING[self.policy.mode]
        if ceiling is None or assessment.risk.rank > ceiling.rank:
            return decide(
                PermissionMode.ASK,
                f"{self.policy.mode.value} mode confirms anything above "
                f"{ceiling.value if ceiling else 'nothing'}.",
                f"mode_ceiling({self.policy.mode.value})",
            )

        # 11. The scope must also be marked automatic. Enabling a scope means
        #     "may do this"; auto means "may do this without asking me".
        if scope not in self.policy.auto_scopes:
            return decide(
                PermissionMode.ASK,
                f"{scope.value} is enabled but not set to run automatically.",
                "scope_not_automatic",
            )

        return decide(PermissionMode.ALLOW, "Within policy.", "allowed")

    async def _core_decision(
        self, action: ComputerAction, assessment: RiskAssessment, user_id: str
    ):
        from jarvis.permissions.engine import PermissionEngine, PermissionRequest

        return await PermissionEngine(self.session).evaluate(
            PermissionRequest(
                user_id=user_id,
                capability=_SCOPE_CAPABILITY[action.scope],
                resource=f"computer:{action.scope.value.lower()}:{action.kind.value}",
                risk_level=_RISK_TO_CORE[assessment.risk],
                reversible=not assessment.irreversible,
                tainted=action.tainted,
            )
        )


async def load_policy(session: AsyncSession, user_id: str) -> ComputerPolicy:
    """Read the user's stored computer policy."""
    from jarvis.db.models import User

    user = await session.get(User, user_id)
    settings = (user.settings or {}) if user else {}
    return ComputerPolicy.from_dict(settings.get("computer"))


async def save_policy(
    session: AsyncSession, user_id: str, policy: ComputerPolicy
) -> ComputerPolicy:
    from jarvis.db.models import User

    user = await session.get(User, user_id)
    if user is None:
        raise ValueError(f"Unknown user {user_id}")
    # Reassigned rather than mutated: SQLAlchemy does not track in-place
    # changes to a JSON column, so mutation silently fails to persist.
    user.settings = {**(user.settings or {}), "computer": policy.to_dict()}
    await session.flush()
    log.info(
        "computer_policy_updated",
        mode=policy.mode.value,
        enabled=sorted(s.value for s in policy.enabled_scopes),
        auto=sorted(s.value for s in policy.auto_scopes),
    )
    return policy
