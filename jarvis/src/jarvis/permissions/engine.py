"""Permission engine.

Every tool execution passes through :meth:`PermissionEngine.evaluate`. There is
no bypass path — the tool executor cannot run a handler without a decision.

The model follows the Phase 0 audit rather than a linear 0-7 ladder: a grant is
``(capability, resource_scope, mode)`` and evaluation is most-specific-wins.
The audit's argument was that the levels are not actually ordered — browser
access is not a superset of file access — so a scalar cannot express the rules
you actually want.

Three rules make the system fail safe rather than fail open:

1. **Defaults deny upward.** Absent any grant, ``READ`` is allowed and
   everything else asks or denies. A capability nobody has reasoned about does
   not execute silently.
2. **Irreversibility floors the decision.** An irreversible action is never
   auto-allowed regardless of grants — the best it can get is ``ASK``. This is
   the reversibility gate from the audit, and it is why destructive tools in
   later phases cannot be made silent by a broad grant.
3. **Risk ceilings downgrade, never upgrade.** A grant's ``max_risk``
   condition can turn ``ALLOW`` into ``ASK``; nothing can turn ``DENY`` into
   ``ALLOW``.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from jarvis.db.models import (
    Capability,
    PermissionGrant,
    PermissionMode,
    RiskLevel,
    ToolDefinition,
)
from jarvis.logging import get_logger

log = get_logger(__name__)

#: Applied when no grant matches. Read is permitted because it cannot change
#: anything; sensitive actions are denied outright rather than merely asked,
#: so that enabling them is an explicit, recorded act.
DEFAULT_MODES: dict[Capability, PermissionMode] = {
    Capability.READ: PermissionMode.ALLOW,
    Capability.WRITE: PermissionMode.ASK,
    Capability.EXECUTE: PermissionMode.ASK,
    Capability.EXTERNAL_ACTION: PermissionMode.ASK,
    Capability.SENSITIVE_ACTION: PermissionMode.DENY,
}

_RISK_ORDER: dict[RiskLevel, int] = {
    RiskLevel.NONE: 0,
    RiskLevel.LOW: 1,
    RiskLevel.MEDIUM: 2,
    RiskLevel.HIGH: 3,
    RiskLevel.CRITICAL: 4,
}


@dataclass(slots=True)
class PermissionRequest:
    user_id: str
    capability: Capability
    #: What is being acted on. For tools this is ``tool:<name>``; later phases
    #: use ``file:<path>``, ``app:<name>``, ``domain:<host>``.
    resource: str
    risk_level: RiskLevel = RiskLevel.NONE
    reversible: bool = True
    tool_name: str | None = None
    #: Set when the request's context contains content from an untrusted
    #: source (a fetched page, an email body). Reserved for Phase 5's taint
    #: tracking; the engine already honours it so the plumbing is not retrofit.
    tainted: bool = False


@dataclass(slots=True)
class PermissionDecision:
    mode: PermissionMode
    reason: str
    capability: Capability
    resource: str
    matched_grant_id: str | None = None
    applied_rules: list[str] = field(default_factory=list)

    @property
    def allowed(self) -> bool:
        return self.mode is PermissionMode.ALLOW

    @property
    def denied(self) -> bool:
        return self.mode is PermissionMode.DENY

    @property
    def needs_confirmation(self) -> bool:
        return self.mode is PermissionMode.ASK

    def describe(self) -> dict[str, object]:
        return {
            "mode": self.mode.value,
            "reason": self.reason,
            "capability": self.capability.value,
            "resource": self.resource,
            "matched_grant": self.matched_grant_id,
            "rules": self.applied_rules,
        }


def _specificity(scope: str) -> int:
    """Higher = more specific. ``*`` is least specific.

    Ranking by the length of the literal (non-wildcard) prefix means
    ``tool:create_task`` beats ``tool:*`` beats ``*``.
    """
    if scope == "*":
        return 0
    return len(scope.split("*", 1)[0]) * 10 + (0 if "*" in scope else 5)


class PermissionEngine:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def evaluate(self, request: PermissionRequest) -> PermissionDecision:
        rules: list[str] = []

        grant = await self._best_grant(request)
        if grant is not None:
            mode = grant.mode
            reason = f"grant:{grant.resource_scope}"
            rules.append(f"matched_grant({grant.resource_scope}->{mode.value})")
        else:
            mode = DEFAULT_MODES[request.capability]
            reason = f"default:{request.capability.value}"
            rules.append(f"default({request.capability.value}->{mode.value})")

        # Per-tool operator override sits above grants: it is the switch the UI
        # exposes for "always ask before this specific tool".
        if request.tool_name:
            override = await self.session.get(ToolDefinition, request.tool_name)
            if override is not None:
                if not override.enabled:
                    return PermissionDecision(
                        mode=PermissionMode.DENY,
                        reason="tool_disabled",
                        capability=request.capability,
                        resource=request.resource,
                        applied_rules=rules + ["tool_disabled"],
                    )
                if override.mode_override is not None:
                    mode = override.mode_override
                    reason = "tool_override"
                    rules.append(f"tool_override({mode.value})")

        # Risk ceiling: downgrade only.
        if grant is not None and mode is PermissionMode.ALLOW:
            max_risk = grant.conditions.get("max_risk")
            if max_risk and _RISK_ORDER.get(request.risk_level, 0) > _RISK_ORDER.get(
                RiskLevel(max_risk), 4
            ):
                mode = PermissionMode.ASK
                reason = "risk_exceeds_grant_ceiling"
                rules.append(f"risk_ceiling({request.risk_level.value}>{max_risk})")

        # Reversibility floor — never auto-allow something we cannot undo.
        if mode is PermissionMode.ALLOW and not request.reversible:
            mode = PermissionMode.ASK
            reason = "irreversible_action"
            rules.append("irreversible_floor")

        # Untrusted content in context escalates anything that leaves the
        # machine or changes state. Prompt injection is the threat this exists
        # for; see audit §14.4.
        if (
            request.tainted
            and mode is PermissionMode.ALLOW
            and request.capability is not Capability.READ
        ):
            mode = PermissionMode.ASK
            reason = "untrusted_context"
            rules.append("taint_escalation")

        # Sensitive actions can never be auto-allowed in Phase 1, whatever the
        # grants say. Later phases may relax this behind an explicit autonomy
        # mode; until that exists, this is a hard stop.
        if (
            request.capability is Capability.SENSITIVE_ACTION
            and mode is PermissionMode.ALLOW
        ):
            mode = PermissionMode.ASK
            reason = "sensitive_action_requires_confirmation"
            rules.append("sensitive_floor")

        decision = PermissionDecision(
            mode=mode,
            reason=reason,
            capability=request.capability,
            resource=request.resource,
            matched_grant_id=grant.id if grant else None,
            applied_rules=rules,
        )
        log.info(
            "permission_decision",
            capability=request.capability.value,
            resource=request.resource,
            tool=request.tool_name,
            decision=mode.value,
            reason=reason,
        )
        return decision

    async def _best_grant(self, request: PermissionRequest) -> PermissionGrant | None:
        now = datetime.now(timezone.utc)
        stmt = select(PermissionGrant).where(
            PermissionGrant.user_id == request.user_id,
            PermissionGrant.capability == request.capability,
            PermissionGrant.revoked_at.is_(None),
        )
        rows = (await self.session.execute(stmt)).scalars().all()

        candidates: list[PermissionGrant] = []
        for grant in rows:
            if grant.expires_at is not None:
                expires = grant.expires_at
                if expires.tzinfo is None:
                    expires = expires.replace(tzinfo=timezone.utc)
                if expires <= now:
                    continue
            if fnmatch.fnmatch(request.resource, grant.resource_scope):
                candidates.append(grant)

        if not candidates:
            return None

        # Most specific wins; DENY breaks ties so an explicit block cannot be
        # defeated by an equally-specific allow.
        candidates.sort(
            key=lambda g: (
                _specificity(g.resource_scope),
                1 if g.mode is PermissionMode.DENY else 0,
            ),
            reverse=True,
        )
        return candidates[0]


async def seed_default_grants(session: AsyncSession, user_id: str) -> list[PermissionGrant]:
    """Baseline policy for a new user.

    Deliberately minimal: read is open, the safe built-in tools are allowed by
    name, and everything else falls through to the defaults (ask or deny). New
    capabilities are therefore closed until someone opens them.
    """
    existing = (
        await session.execute(
            select(PermissionGrant).where(PermissionGrant.user_id == user_id)
        )
    ).scalars().first()
    if existing is not None:
        return []

    grants = [
        PermissionGrant(
            user_id=user_id,
            capability=Capability.READ,
            resource_scope="*",
            mode=PermissionMode.ALLOW,
            note="Read-only operations are permitted by default.",
        ),
        PermissionGrant(
            user_id=user_id,
            capability=Capability.WRITE,
            resource_scope="tool:create_task",
            mode=PermissionMode.ALLOW,
            conditions={"max_risk": RiskLevel.LOW.value},
            note="Creating a JARVIS task is reversible and low risk.",
        ),
        PermissionGrant(
            user_id=user_id,
            capability=Capability.WRITE,
            resource_scope="tool:browser_close_page",
            mode=PermissionMode.ALLOW,
            conditions={"max_risk": RiskLevel.LOW.value},
            note=(
                "Closing a browser page JARVIS itself opened releases JARVIS's "
                "own resource and touches nothing of the user's. Asking about "
                "it would teach them to approve without reading, which is what "
                "makes the approvals that matter worthless."
            ),
        ),
        PermissionGrant(
            user_id=user_id,
            capability=Capability.WRITE,
            resource_scope="tool:update_task",
            mode=PermissionMode.ALLOW,
            conditions={"max_risk": RiskLevel.LOW.value},
            note="Updating a task is reversible and recorded in task history.",
        ),
    ]
    session.add_all(grants)
    await session.flush()
    log.info("default_grants_seeded", user_id=user_id, count=len(grants))
    return grants
