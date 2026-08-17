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
from datetime import datetime, time, timezone

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
    #: The asking agent's ceiling, when a sub-agent is asking.
    #:
    #: ``None`` for the user's own loop, which acts on the grants alone. A
    #: sub-agent carries an :class:`~jarvis.agents.identity.AgentIdentity`
    #: whose capability and tool sets bound what it may attempt — checked
    #: *before* grants and never liftable by one, so delegation can only ever
    #: subtract authority.
    agent: Any = None


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

        # The agent ceiling comes first, and returns rather than downgrading.
        #
        # Order is the point. A ceiling checked after grants would be one more
        # thing that could be argued with by adding a grant, and the whole
        # value of delegation being safe is that a child's bound does not move
        # when the user's permissions do. A ceiling only ever subtracts, so
        # there is nothing to combine — outside it is simply not this agent's
        # to attempt.
        ceiling = self._ceiling_verdict(request)
        if ceiling is not None:
            return ceiling

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

    @staticmethod
    def _ceiling_verdict(request: PermissionRequest) -> PermissionDecision | None:
        """DENY when a sub-agent asks for something outside its ceiling.

        Returns ``None`` when there is nothing to say — no agent, root agent,
        or a request the ceiling permits — so the ordinary path is unchanged
        and costs one attribute read.
        """
        agent = request.agent
        if agent is None:
            return None

        def refuse(rule: str, why: str) -> PermissionDecision:
            log.warning(
                "agent_ceiling_denied",
                agent_id=getattr(agent, "agent_id", "?"),
                role=getattr(agent, "role", "?"),
                capability=request.capability.value,
                tool=request.tool_name,
            )
            return PermissionDecision(
                mode=PermissionMode.DENY,
                reason=why,
                capability=request.capability,
                resource=request.resource,
                applied_rules=[rule],
            )

        if not agent.permits_capability(request.capability):
            return refuse(
                f"agent_ceiling(capability={request.capability.value})",
                f"The {agent.role} agent was not delegated "
                f"{request.capability.value}.",
            )
        if request.tool_name and not agent.permits_tool(request.tool_name):
            return refuse(
                f"agent_ceiling(tool={request.tool_name})",
                f"The {agent.role} agent was not delegated {request.tool_name}.",
            )
        return None

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
            if not fnmatch.fnmatch(request.resource, grant.resource_scope):
                continue
            if not _context_matches(grant, request):
                continue
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


#: Conditions that decide *whether a grant applies at all*, as opposed to
#: ``max_risk``, which decides what it grants once it does.
#:
#: `vierisid/jarvis` models these as a separate "context rules" list evaluated
#: alongside its authority levels. Ours live on the grant instead, for one
#: reason worth stating: a separate list is a second policy surface, and the
#: whole argument of this engine is that there is exactly one place to look up
#: "what is this user allowed to do". A grant that does not apply right now is
#: simply not a candidate, so most-specific-wins and DENY-breaks-ties keep
#: working unchanged.
#:
#: Supported keys, all optional:
#:
#: ``active_between``  ``{"from": "09:00", "to": "18:00"}`` — local wall-clock,
#:   because "after 22:00" means the operator's evening and not UTC's. A window
#:   whose ``to`` is earlier than its ``from`` wraps past midnight, which is the
#:   case people actually want ("no external actions between 22:00 and 06:00").
#: ``tools``  ``["browser_open", …]`` — restrict to these tool names.
#: ``when_tainted``  ``true``/``false`` — apply only on a turn that has (or has
#:   not) read untrusted content. A DENY with ``true`` is "nothing that touched
#:   a web page may do this", expressed once rather than per tool.
#:
#: An unrecognised key is ignored rather than failing the grant. Operator data
#: outliving a rename must not silently revoke permissions — but note the
#: asymmetry: ignoring an unknown *restriction* widens rather than narrows, so
#: anything added here has to be additive to the key set, never a rename.


def _in_window(now: datetime, spec: Any) -> bool:
    """Is the local wall clock inside ``{"from": "HH:MM", "to": "HH:MM"}``?

    Inclusive of ``from``, exclusive of ``to``, so two adjacent windows do not
    both claim the boundary minute. A malformed spec returns ``False`` — the
    grant does not apply — because the alternative is a typo silently granting
    permission around the clock.
    """
    if not isinstance(spec, dict):
        return False
    try:
        start = time.fromisoformat(str(spec["from"]))
        end = time.fromisoformat(str(spec["to"]))
    except (KeyError, ValueError, TypeError):
        log.warning("permission_grant_bad_window", spec=str(spec)[:80])
        return False

    current = now.time()
    if start <= end:
        return start <= current < end
    # Wraps past midnight: 22:00 → 06:00 is two intervals, not none.
    return current >= start or current < end


def _context_matches(grant: PermissionGrant, request: PermissionRequest) -> bool:
    """Does this grant apply to the situation, before asking what it grants?"""
    conditions = grant.conditions or {}

    window = conditions.get("active_between")
    if window is not None and not _in_window(datetime.now(), window):
        return False

    tools = conditions.get("tools")
    if tools is not None:
        if not isinstance(tools, list) or request.tool_name not in tools:
            return False

    when_tainted = conditions.get("when_tainted")
    if when_tainted is not None and bool(when_tainted) is not bool(request.tainted):
        return False

    return True


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
