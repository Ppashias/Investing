"""Delegation and background work, as tools (Phase D, items 6 & 7 wiring).

Four tools. They are the only way anything reaches
:class:`~jarvis.agents.supervisor.AgentSupervisor` or
:class:`~jarvis.agents.background.BackgroundRunner`, and they are thin for the
same reason the browser tools are: the decision belongs where a reviewer can
see it, and the mechanics belong somewhere else.

## Why spawning is a tool rather than a pipeline stage

Because a tool passes through :class:`~jarvis.tools.executor.ToolExecutor`, and
a pipeline stage does not. Spawning an agent hands authority to a second actor;
that is exactly the sort of act the permission engine, the confirmation flow
and the audit log exist for. Making it a stage would have created the second
execution path this codebase has consistently refused to build.

``spawn_agent`` declares ``EXECUTE``, which defaults to ASK. So delegation is
something the user is asked about the first time and can grant standing
permission for — rather than something that either always interrupts or never
does.

## What the model can and cannot say

It can name a role, a task, and the capabilities it *wants*. It cannot obtain
more than the spawning agent already holds: the ceiling is computed by
:meth:`~jarvis.agents.identity.AgentIdentity.narrowed` from the parent, and a
request for more is quietly dropped rather than refused — a refusal is a signal
a prompt-injected spawn could iterate against to map the boundary.
"""

from __future__ import annotations

from typing import Any

from jarvis.agents.background import JobRejected, JobState
from jarvis.agents.identity import AgentDepthExceeded, AgentIdentity
from jarvis.db.models import Capability, RiskLevel
from jarvis.tools.base import ToolContext, ToolResult, tool

#: What the model may ask for, mapped to what the system understands. A model
#: naming a capability outside this table gets nothing rather than an error —
#: the same reasoning as the ceiling itself.
_CAPABILITIES = {
    "read": Capability.READ,
    "write": Capability.WRITE,
    "execute": Capability.EXECUTE,
    "external": Capability.EXTERNAL_ACTION,
}

_NO_SUPERVISOR = (
    "Delegation is not available in this build, so I will do this myself."
)
_NO_RUNNER = (
    "Background work is not available in this build, so I will do this now "
    "instead."
)


def _supervisor(ctx: ToolContext) -> Any:
    return ctx.extras.get("agents")


def _runner(ctx: ToolContext) -> Any:
    return ctx.extras.get("background")


def _identity(ctx: ToolContext) -> AgentIdentity:
    """Who is spawning. The root agent when nothing says otherwise.

    A turn with no identity on it *is* the user's own loop, so treating that as
    root is correct rather than a fallback — but it is written out here because
    "None means full authority" is exactly the kind of default that deserves to
    be visible.
    """
    return ctx.agent if ctx.agent is not None else AgentIdentity.root()


@tool(
    name="spawn_agent",
    description=(
        "Delegate a self-contained piece of work to a focused sub-agent — for "
        "example research you will summarise later. The sub-agent gets a "
        "subset of your own permissions and never more, and it is bounded in "
        "steps and time. Prefer doing small things yourself; this is for work "
        "worth isolating."
    ),
    parameters={
        "type": "object",
        "properties": {
            "role": {
                "type": "string",
                "minLength": 1,
                "description": "Short name for what it is for, e.g. 'researcher'.",
            },
            "task": {
                "type": "string",
                "minLength": 1,
                "description": "What it should accomplish, in one or two sentences.",
            },
            "capabilities": {
                "type": "array",
                "items": {"type": "string", "enum": sorted(_CAPABILITIES)},
                "description": (
                    "What it needs. You cannot grant more than you hold, and "
                    "asking for more is simply not granted."
                ),
            },
            "tools": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional: restrict it to these tools by name.",
            },
        },
        "required": ["role", "task"],
        "additionalProperties": False,
    },
    capability=Capability.EXECUTE,
    risk_level=RiskLevel.MEDIUM,
    category="agents",
)
async def spawn_agent(
    *,
    ctx: ToolContext,
    role: str,
    task: str,
    capabilities: list[str] | None = None,
    tools: list[str] | None = None,
) -> ToolResult:
    supervisor = _supervisor(ctx)
    if supervisor is None:
        return ToolResult.error(_NO_SUPERVISOR, available=False)

    wanted = None
    if capabilities is not None:
        # Unknown names drop out rather than raising. The list comes from the
        # model, and an error would tell it which names exist.
        wanted = {_CAPABILITIES[c] for c in capabilities if c in _CAPABILITIES}

    try:
        child = supervisor.spawn(
            _identity(ctx),
            role=role.strip()[:60],
            capabilities=wanted,
            tools=set(tools) if tools else None,
        )
    except AgentDepthExceeded as exc:
        return ToolResult.error(
            f"{exc} Do this piece of work yourself rather than delegating again.",
            spawned=False,
        )

    await supervisor.record_spawn(child, task, ctx)

    granted = sorted(c.value for c in (child.capabilities or ()))
    return ToolResult.ok(
        f"Spawned the {child.role} agent ({child.agent_id}) for: {task}\n"
        f"It holds {', '.join(granted) if granted else 'no capabilities'}"
        + (f", limited to {', '.join(sorted(child.tools))}" if child.tools else "")
        + ".",
        agent_id=child.agent_id,
        role=child.role,
        depth=child.depth,
        capabilities=granted,
        tools=sorted(child.tools) if child.tools else None,
    )


@tool(
    name="start_background_task",
    description=(
        "Start work that continues after this reply — a long search, a "
        "multi-step job. Returns a job id you can check with "
        "background_status. Anything the job needs approval for will pause it "
        "and ask, rather than proceeding while nobody is watching."
    ),
    parameters={
        "type": "object",
        "properties": {
            "title": {"type": "string", "minLength": 1},
            "task": {"type": "string", "minLength": 1,
                     "description": "What the job should accomplish."},
        },
        "required": ["title", "task"],
        "additionalProperties": False,
    },
    capability=Capability.EXECUTE,
    risk_level=RiskLevel.MEDIUM,
    category="agents",
)
async def start_background_task(
    *, ctx: ToolContext, title: str, task: str
) -> ToolResult:
    runner = _runner(ctx)
    if runner is None:
        return ToolResult.error(_NO_RUNNER, available=False)

    # The step function is deliberately a placeholder that completes: the loop
    # that drives a real model turn inside a job is the next piece of work, and
    # shipping a runner that *pretends* to do the work would be worse than one
    # that says plainly it recorded the request. Reporting an empty job as
    # progress is the fake-success failure this system exists to avoid.
    async def _record_only(job: Any) -> tuple[bool, str]:
        return True, f"recorded: {task}"

    try:
        job = await runner.start(title=title.strip()[:200], step=_record_only)
    except JobRejected as exc:
        return ToolResult.error(str(exc), started=False)

    return ToolResult.ok(
        f"Started background job {job.job_id}: {title}. "
        "Note that background jobs currently record the request rather than "
        "carrying it out — check with background_status.",
        job_id=job.job_id, title=job.title, state=job.state.value,
    )


@tool(
    name="background_status",
    description=(
        "List background jobs and how they are doing. Use this when the user "
        "asks what you are working on, or after starting a job."
    ),
    parameters={
        "type": "object",
        "properties": {
            "job_id": {"type": "string",
                       "description": "Optional: just this one."},
        },
        "additionalProperties": False,
    },
    capability=Capability.READ,
    category="agents",
)
async def background_status(*, ctx: ToolContext, job_id: str | None = None) -> ToolResult:
    runner = _runner(ctx)
    if runner is None:
        return ToolResult.error(_NO_RUNNER, available=False)

    if job_id:
        job = runner.get(job_id)
        if job is None:
            return ToolResult.error(
                f"There is no job {job_id}. It may have finished long enough "
                "ago to be forgotten.",
                job_id=job_id,
            )
        jobs = [job]
    else:
        jobs = runner.list()

    if not jobs:
        return ToolResult.ok("Nothing is running in the background.", count=0,
                             jobs=[])

    lines = []
    for job in jobs:
        line = f"- {job.job_id}: {job.title} — {job.state.value.lower()}"
        if job.state is JobState.AWAITING_CONFIRMATION:
            line += " (waiting for your approval)"
        elif job.progress.note:
            line += f" ({job.progress.note})"
        lines.append(line)

    return ToolResult.ok(
        "\n".join(lines), count=len(jobs), jobs=[j.describe() for j in jobs]
    )


@tool(
    name="cancel_background_task",
    description="Stop a background job. It stops now, not at its convenience.",
    parameters={
        "type": "object",
        "properties": {"job_id": {"type": "string", "minLength": 1}},
        "required": ["job_id"],
        "additionalProperties": False,
    },
    capability=Capability.WRITE,
    risk_level=RiskLevel.LOW,
    category="agents",
)
async def cancel_background_task(*, ctx: ToolContext, job_id: str) -> ToolResult:
    runner = _runner(ctx)
    if runner is None:
        return ToolResult.error(_NO_RUNNER, available=False)

    if runner.cancel(job_id):
        return ToolResult.ok(f"Cancelled {job_id}.", job_id=job_id, cancelled=True)
    return ToolResult.ok(
        f"Job {job_id} was not running — nothing to cancel.",
        job_id=job_id, cancelled=False,
    )


TOOLS = [
    spawn_agent,
    start_background_task,
    background_status,
    cancel_background_task,
]

__all__ = ["TOOLS"] + [t.name for t in TOOLS]


# ── goals (Phase D, item 12) ─────────────────────────────────────────────────
#
# The goal service answers questions and never acts. These tools are how the
# *answers* reach a turn — and, deliberately, how any resulting action becomes
# an ordinary tool call subject to the ordinary checks. A goal system that
# could act on its own schedule would be an autonomous actor with a private
# execution path, which is the one thing this codebase keeps refusing to build.


@tool(
    name="set_goal",
    description=(
        "Record something the user wants to achieve, optionally with steps and "
        "a deadline. Mark it as a commitment when they have promised it to "
        "someone — commitments are chased harder than intentions."
    ),
    parameters={
        "type": "object",
        "properties": {
            "title": {"type": "string", "minLength": 1},
            "steps": {"type": "array", "items": {"type": "string"}},
            "due_at": {
                "type": "string",
                "description": "ISO 8601, e.g. 2026-09-01T17:00:00Z.",
            },
            "is_commitment": {"type": "boolean"},
            "description": {"type": "string"},
        },
        "required": ["title"],
        "additionalProperties": False,
    },
    capability=Capability.WRITE,
    risk_level=RiskLevel.LOW,
    category="agents",
)
async def set_goal(
    *,
    ctx: ToolContext,
    title: str,
    steps: list[str] | None = None,
    due_at: str | None = None,
    is_commitment: bool = False,
    description: str | None = None,
) -> ToolResult:
    from datetime import datetime

    from jarvis.goals.service import GoalService

    when = None
    if due_at:
        try:
            when = datetime.fromisoformat(due_at.replace("Z", "+00:00"))
        except ValueError:
            # Refused rather than silently dropped: a goal that quietly loses
            # its deadline is one nothing will ever chase, and the model would
            # have no way to know.
            return ToolResult.error(
                f"I could not read {due_at!r} as a date. Use ISO 8601, like "
                "2026-09-01T17:00:00Z.",
                created=False,
            )

    goal = await GoalService(ctx.session).create_goal(
        ctx.user_id, title=title, steps=steps, due_at=when,
        is_commitment=is_commitment, description=description,
    )
    return ToolResult.ok(
        f"Recorded {'commitment' if is_commitment else 'goal'}: {goal.title}"
        + (f" ({len(steps)} steps)" if steps else "")
        + (f", due {when.isoformat()}" if when else "") + ".",
        goal_id=goal.id, steps=len(steps or []), is_commitment=is_commitment,
    )


@tool(
    name="review_goals",
    description=(
        "Show what the user is working towards, how far along each is, and "
        "anything overdue, due soon, or gone quiet. Use this when they ask "
        "what they should be doing, or at the start of a working session."
    ),
    parameters={"type": "object", "properties": {}, "additionalProperties": False},
    capability=Capability.READ,
    category="agents",
)
async def review_goals(*, ctx: ToolContext) -> ToolResult:
    from jarvis.goals.service import GoalService

    service = GoalService(ctx.session)
    goals = await service.goals(ctx.user_id)
    follow_ups = await service.follow_ups(ctx.user_id)

    if not goals and not follow_ups:
        return ToolResult.ok("No goals recorded, and nothing needs chasing.",
                             goals=[], follow_ups=[])

    lines: list[str] = []
    if goals:
        lines.append("Goals:")
        for goal in goals:
            done = f"{goal.completed}/{goal.total}"
            lines.append(
                f"- {goal.title} — {done} steps"
                + (" — OVERDUE" if goal.overdue else "")
            )
    if follow_ups:
        lines.append("")
        lines.append("Worth raising:")
        for item in follow_ups:
            mark = "promise" if item.is_commitment else "intention"
            lines.append(f"- {item.title} ({item.reason}, {mark})")

    return ToolResult.ok(
        "\n".join(lines),
        goals=[g.describe() for g in goals],
        follow_ups=[f.describe() for f in follow_ups],
    )


TOOLS = TOOLS + [set_goal, review_goals]
__all__ = ["TOOLS"] + [t.name for t in TOOLS]
