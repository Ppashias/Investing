"""Sub-agents and the authority that bounds them (Phase D, item 6).

The machinery that runs a second agent loop is not the interesting part. The
interesting part is *on whose authority it acts*, because the obvious answer —
a child inherits its parent's reach and is trusted to behave — makes the
model's judgement the security boundary. A page that persuades a planner to
spawn a "cleanup specialist" would, at that moment, have obtained a second
actor with the planner's full authority and none of its context.

So almost everything here is about subtraction: what a child cannot do, what a
grant cannot restore, and what a budget ends regardless of what the agent
thinks it still needs to finish.

No test here sets a ceiling on a stored row by hand and asserts it came back.
Each drives the real engine, because the engine is the thing that has to
enforce it.
"""

from __future__ import annotations

import asyncio

import pytest

from jarvis.agents.identity import (
    FORBIDDEN_TO_CHILDREN,
    MAX_DEPTH,
    AgentDepthExceeded,
    AgentIdentity,
)
from jarvis.agents.supervisor import (
    AgentBudget,
    AgentBudgetExceeded,
    AgentSupervisor,
)
from jarvis.db.models import (
    Capability,
    PermissionGrant,
    PermissionMode,
    RiskLevel,
)
from jarvis.permissions.engine import PermissionEngine, PermissionRequest


# ── the ceiling can only subtract ────────────────────────────────────────────


def test_the_root_agent_has_no_ceiling() -> None:
    """The user's own loop acts on the grants alone.

    A root ceiling would be a second permission system with nothing above it to
    set the first one, which is how you end up with two sources of truth about
    what is allowed.
    """
    root = AgentIdentity.root()
    assert root.capabilities is None
    assert root.is_root
    for capability in Capability:
        assert root.permits_capability(capability)


def test_a_child_gets_only_what_its_parent_holds() -> None:
    parent = AgentIdentity.root().narrowed(
        role="planner", capabilities={Capability.READ, Capability.WRITE}
    )
    child = parent.narrowed(
        role="worker",
        capabilities={Capability.READ, Capability.WRITE, Capability.EXECUTE},
    )
    assert child.capabilities == frozenset({Capability.READ, Capability.WRITE})


def test_asking_for_more_is_dropped_rather_than_refused() -> None:
    """Silence, not an error.

    An error would make the *request* meaningful, and the request comes from
    the model. A prompt-injected spawn that gets a refusal can iterate against
    it to map the boundary; one that gets an agent quietly missing the
    capability learns nothing.
    """
    parent = AgentIdentity.root().narrowed(
        role="planner", capabilities={Capability.READ}
    )
    child = parent.narrowed(role="w", capabilities={Capability.EXECUTE})
    assert child.capabilities == frozenset()


@pytest.mark.parametrize("forbidden", sorted(FORBIDDEN_TO_CHILDREN, key=str))
def test_some_capabilities_never_reach_a_child(forbidden) -> None:
    """Even from a root agent that holds everything.

    A sub-agent is the least supervised actor in the system and the furthest
    from the user who would have to answer for it — the worst possible
    exception to a default-deny.
    """
    child = AgentIdentity.root().narrowed(
        role="anything", capabilities=set(Capability)
    )
    assert forbidden not in child.capabilities


def test_a_tool_allowlist_only_narrows() -> None:
    parent = AgentIdentity.root().narrowed(
        role="planner", tools={"get_current_time", "list_tasks"}
    )
    child = parent.narrowed(role="w", tools={"list_tasks", "create_task"})
    assert child.tools == frozenset({"list_tasks"})
    assert child.permits_tool("list_tasks")
    assert not child.permits_tool("create_task")


def test_an_identity_cannot_be_mutated_after_it_is_handed_out() -> None:
    """A ceiling that can be edited later is not a ceiling."""
    child = AgentIdentity.root().narrowed(role="w", capabilities={Capability.READ})
    with pytest.raises(Exception):
        child.capabilities = frozenset(Capability)  # type: ignore[misc]


def test_delegation_is_bounded_in_depth() -> None:
    identity = AgentIdentity.root()
    for level in range(MAX_DEPTH):
        identity = identity.narrowed(role=f"level{level}")
    with pytest.raises(AgentDepthExceeded):
        identity.narrowed(role="one-too-many")


# ── the engine enforces it, and no grant lifts it ────────────────────────────


async def _decide(session, user, capability, *, agent=None, tool="list_tasks"):
    return await PermissionEngine(session).evaluate(
        PermissionRequest(
            user_id=user.id,
            capability=capability,
            resource=f"tool:{tool}",
            risk_level=RiskLevel.LOW,
            tool_name=tool,
            agent=agent,
        )
    )


async def test_the_engine_denies_outside_the_ceiling(session, user) -> None:
    child = AgentIdentity.root().narrowed(role="reader",
                                          capabilities={Capability.READ})
    decision = await _decide(session, user, Capability.WRITE, agent=child)
    assert decision.mode is PermissionMode.DENY
    assert "agent_ceiling" in " ".join(decision.applied_rules)


async def test_a_grant_cannot_lift_a_ceiling(session, user) -> None:
    """The invariant that makes delegation safe.

    `vierisid/jarvis` gets this backwards — its engine checks temporary parent
    grants *before* per-action overrides, so a grant beats an explicit deny.
    A child's bound must not move when the user's permissions do.
    """
    session.add(
        PermissionGrant(
            user_id=user.id,
            capability=Capability.WRITE,
            resource_scope="*",
            mode=PermissionMode.ALLOW,
            note="Deliberately over-broad, for the sub-agent tests.",
        )
    )
    await session.flush()

    # The user themselves may write.
    assert (await _decide(session, user, Capability.WRITE)).mode is PermissionMode.ALLOW

    # The delegated agent still may not.
    child = AgentIdentity.root().narrowed(role="reader",
                                          capabilities={Capability.READ})
    assert (
        await _decide(session, user, Capability.WRITE, agent=child)
    ).mode is PermissionMode.DENY


async def test_the_ceiling_is_checked_before_grants_are_read(session, user) -> None:
    """Order, asserted rather than assumed.

    A ceiling applied after grants would be one more thing an added grant could
    argue with. The denial must name the ceiling and nothing else.
    """
    child = AgentIdentity.root().narrowed(role="reader",
                                          capabilities={Capability.READ})
    decision = await _decide(session, user, Capability.EXECUTE, agent=child)
    assert decision.applied_rules == [
        f"agent_ceiling(capability={Capability.EXECUTE.value})"
    ]


async def test_a_tool_outside_the_allowlist_is_denied(session, user) -> None:
    child = AgentIdentity.root().narrowed(
        role="timekeeper", capabilities={Capability.READ},
        tools={"get_current_time"},
    )
    allowed = await _decide(session, user, Capability.READ, agent=child,
                            tool="get_current_time")
    assert allowed.mode is not PermissionMode.DENY

    refused = await _decide(session, user, Capability.READ, agent=child,
                            tool="list_tasks")
    assert refused.mode is PermissionMode.DENY
    assert "agent_ceiling(tool=list_tasks)" in refused.applied_rules


async def test_a_root_request_is_completely_unaffected(session, user) -> None:
    """The ordinary path must not change shape because agents now exist."""
    decision = await _decide(session, user, Capability.READ, agent=None)
    assert decision.mode is PermissionMode.ALLOW
    assert not any("agent_ceiling" in r for r in decision.applied_rules)


# ── budgets end an agent; they are not advice ────────────────────────────────


def test_a_budget_stops_at_its_step_limit() -> None:
    budget = AgentBudget(max_steps=3)
    for _ in range(3):
        budget.tick()
    with pytest.raises(AgentBudgetExceeded):
        budget.tick()


def test_a_budget_stops_on_wall_clock() -> None:
    budget = AgentBudget(max_steps=1000, timeout_seconds=-1.0)
    with pytest.raises(AgentBudgetExceeded):
        budget.tick()


async def test_a_runaway_agent_is_stopped_rather_than_warned() -> None:
    """The step function never says it is done. The supervisor ends it anyway."""
    supervisor = AgentSupervisor(executor_factory=None, registry=None)
    child = AgentIdentity.root().narrowed(role="looper")

    async def never_finishes(ctx, run):
        return False, "still going"

    run = await supervisor.run(
        child, task="loop forever", ctx=_ctx(), budget=AgentBudget(max_steps=4),
        step=never_finishes,
    )
    # Four steps ran; the fifth tick raised before it could be charged.
    assert run.steps == 4
    assert "stopped after 4 steps" in run.stopped_because
    assert supervisor.running == []


async def test_an_agent_can_be_cancelled_mid_run() -> None:
    supervisor = AgentSupervisor(executor_factory=None, registry=None)
    child = AgentIdentity.root().narrowed(role="worker")

    async def slow(ctx, run):
        if run.steps == 1:
            supervisor.cancel(child.agent_id)
        await asyncio.sleep(0)
        return False, ""

    run = await supervisor.run(
        child, task="work", ctx=_ctx(), budget=AgentBudget(max_steps=50), step=slow
    )
    assert "stopped after" in run.stopped_because
    assert run.steps < 50


async def test_the_emergency_stop_ends_a_running_agent() -> None:
    """A stop that leaves delegated agents running is not a stop."""

    class _Stop:
        engaged = True

    supervisor = AgentSupervisor(executor_factory=None, registry=None,
                                 emergency_stop=_Stop())
    child = AgentIdentity.root().narrowed(role="worker")

    async def work(ctx, run):  # pragma: no cover - must never be reached
        raise AssertionError("the agent ran while the stop was engaged")

    run = await supervisor.run(child, task="work", ctx=_ctx(), step=work)
    assert "emergency stop" in run.stopped_because


# ── containment of the context itself ────────────────────────────────────────


def _ctx(**overrides):
    from jarvis.tools.base import ToolContext

    base = dict(user_id="u_1", session=None, request_id="req_agent")
    base.update(overrides)
    return ToolContext(**base)


async def test_a_child_context_does_not_leak_back_onto_the_parent() -> None:
    """The parent keeps acting after the child finishes.

    An identity left behind on a shared context would apply the child's ceiling
    to the parent — or, worse, the parent's to the next child.
    """
    supervisor = AgentSupervisor(executor_factory=None, registry=None)
    parent_ctx = _ctx()
    child = AgentIdentity.root().narrowed(role="worker",
                                          capabilities={Capability.READ})

    seen = {}

    async def look(ctx, run):
        seen["agent"] = ctx.agent
        return True, "done"

    await supervisor.run(child, task="t", ctx=parent_ctx, step=look)

    assert seen["agent"] is child
    assert parent_ctx.agent is None, "the child's identity outlived its run"


async def test_a_child_inherits_taint_and_cannot_wash_it_off() -> None:
    """Delegation must not become a laundry.

    This is the ambient-memory defect wearing a different hat: content goes in
    untrusted, comes back as another actor's summary, and arrives looking
    clean.
    """
    supervisor = AgentSupervisor(executor_factory=None, registry=None)
    parent_ctx = _ctx(tainted=True)
    child = AgentIdentity.root().narrowed(role="worker")

    seen = {}

    async def look(ctx, run):
        seen["tainted"] = ctx.tainted
        return True, "done"

    await supervisor.run(child, task="t", ctx=parent_ctx, step=look)
    assert seen["tainted"] is True


async def test_a_childs_taint_reaches_the_parent() -> None:
    """The other direction. A child that read a page poisons the turn."""
    supervisor = AgentSupervisor(executor_factory=None, registry=None)
    parent_ctx = _ctx(tainted=False)
    child = AgentIdentity.root().narrowed(role="researcher")

    async def reads_a_page(ctx, run):
        run.tainted = True
        return True, "the page said things"

    await supervisor.run(child, task="research", ctx=parent_ctx, step=reads_a_page)
    assert parent_ctx.tainted is True


# ── the spawn itself is an audited, bounded act ──────────────────────────────


async def test_spawning_is_recorded_with_the_ceiling_it_granted(core, session) -> None:
    """"What was this agent allowed to do?" must be answerable from the log.

    Re-deriving it later from a parent identity that no longer exists is not an
    answer.
    """
    from sqlalchemy import select

    from jarvis.db.models import ActivityLog

    activity = core.orchestrator._activity(session)
    supervisor = AgentSupervisor(executor_factory=None, registry=None,
                                 activity=activity)
    child = supervisor.spawn(
        AgentIdentity.root(), role="researcher",
        capabilities={Capability.READ}, tools={"browser_extract"},
    )
    await supervisor.record_spawn(child, "find the docs", _ctx())
    await session.commit()

    rows = (await session.execute(select(ActivityLog))).scalars().all()
    spawn = [r for r in rows if r.tool_name == "spawn_agent"]
    assert spawn, "the spawn was not recorded"
    detail = spawn[0].detail
    assert detail["role"] == "researcher"
    assert detail["capabilities"] == ["READ"]
    assert detail["tools"] == ["browser_extract"]
    assert detail["depth"] == 1
