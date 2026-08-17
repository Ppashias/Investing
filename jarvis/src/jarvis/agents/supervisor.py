"""Running a delegated agent, and stopping it (Phase D, item 6).

The supervisor owns three things a sub-agent must not own itself: its identity,
its budget, and its ability to stop. Everything else — the model call, the tool
calls, the permission decisions — goes through exactly the machinery the root
loop uses. There is deliberately no second execution path; a delegated tool call
is a :class:`~jarvis.tools.executor.ToolExecutor` call with a different
:class:`~jarvis.agents.identity.AgentIdentity` on the context, and nothing else
about it is special.

## Budgets are the containment, not good behaviour

A sub-agent is the least supervised actor in the system: nobody is reading its
output as it goes, and by construction it was spawned because the root agent did
not want to do the work itself. So the limits are wall-clock, steps and depth,
they are enforced by the supervisor rather than requested of the model, and
exceeding one ends the agent rather than warning it.

## What it cannot do

* It cannot widen its ceiling — see :mod:`jarvis.agents.identity`.
* It cannot spawn past :data:`~jarvis.agents.identity.MAX_DEPTH`.
* It cannot outlive its budget.
* It cannot take an action needing confirmation without the user being asked;
  the executor's confirmation path is unchanged, and a suspended delegated call
  suspends the whole turn exactly as a root one does.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

from jarvis.agents.identity import AgentDepthExceeded, AgentIdentity
from jarvis.db.models import ActivityKind, Capability
from jarvis.logging import get_logger

log = get_logger(__name__)

#: Defaults chosen to be obviously finite rather than generous. A delegated
#: task that needs more than this is a task the root agent should be doing
#: where the user can see it.
DEFAULT_MAX_STEPS = 8
DEFAULT_TIMEOUT_SECONDS = 120.0


class AgentBudgetExceeded(Exception):
    """The agent ran out of steps or time. Not an error — a boundary."""


@dataclass(slots=True)
class AgentBudget:
    max_steps: int = DEFAULT_MAX_STEPS
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    started_at: float = field(default_factory=time.monotonic)
    steps: int = 0

    def tick(self) -> None:
        """Charge one step, and fail closed if either limit is spent."""
        self.steps += 1
        if self.steps > self.max_steps:
            raise AgentBudgetExceeded(
                f"stopped after {self.max_steps} steps"
            )
        if time.monotonic() - self.started_at > self.timeout_seconds:
            raise AgentBudgetExceeded(
                f"stopped after {self.timeout_seconds:g}s"
            )

    @property
    def remaining_seconds(self) -> float:
        return max(0.0, self.timeout_seconds - (time.monotonic() - self.started_at))

    def describe(self) -> dict[str, Any]:
        return {
            "steps": self.steps,
            "max_steps": self.max_steps,
            "timeout_seconds": self.timeout_seconds,
        }


@dataclass(slots=True)
class AgentRun:
    """What a delegated agent produced, and what it cost."""

    identity: AgentIdentity
    task: str
    summary: str = ""
    steps: int = 0
    stopped_because: str = "completed"
    tool_calls: list[str] = field(default_factory=list)
    #: True when anything the agent read was untrusted. Propagates to the
    #: parent: a child that read a web page taints the turn that spawned it,
    #: or delegation would be a laundry for taint exactly as ambient memory
    #: capture was.
    tainted: bool = False

    def describe(self) -> dict[str, Any]:
        return {
            **self.identity.describe(),
            "task": self.task,
            "steps": self.steps,
            "stopped_because": self.stopped_because,
            "tool_calls": self.tool_calls,
            "tainted": self.tainted,
        }


class AgentSupervisor:
    """Spawns delegated agents and holds their leash.

    Constructed per request, like the executor, so it cannot accumulate state
    across turns and cannot be reached by a handler that was not given it.
    """

    def __init__(
        self,
        *,
        executor_factory: Any,
        registry: Any,
        router: Any = None,
        activity: Any = None,
        emergency_stop: Any = None,
    ) -> None:
        self.executor_factory = executor_factory
        self.registry = registry
        self.router = router
        self.activity = activity
        self.emergency_stop = emergency_stop
        self._running: dict[str, AgentBudget] = {}

    # ── spawning ─────────────────────────────────────────────────────────────

    def spawn(
        self,
        parent: AgentIdentity,
        *,
        role: str,
        capabilities: set[Capability] | None = None,
        tools: set[str] | None = None,
    ) -> AgentIdentity:
        """A child identity, bounded by its parent's.

        Raises only on depth. Asking for authority the parent lacks is not an
        error — it is silently not granted, so a prompt-injected spawn cannot
        use refusals to map the boundary.
        """
        child = parent.narrowed(role=role, capabilities=capabilities, tools=tools)
        log.info(
            "agent_spawned",
            agent_id=child.agent_id, role=role, parent=parent.agent_id,
            depth=child.depth,
            capabilities=sorted(c.value for c in (child.capabilities or ())),
        )
        return child

    async def record_spawn(
        self, child: AgentIdentity, task: str, ctx: Any
    ) -> None:
        """One activity row per spawn, with the ceiling that was granted.

        "What was this agent allowed to do?" must be answerable from the log
        rather than by re-deriving it from a parent identity that no longer
        exists.
        """
        if self.activity is None:
            return
        await self.activity.record(
            ActivityKind.TOOL_CALL,
            summary=f"Spawned the {child.role} agent",
            actor="agent_supervisor",
            status="OK",
            tool_name="spawn_agent",
            detail={"task": task, **child.describe()},
            request_id=getattr(ctx, "request_id", None),
            conversation_id=getattr(ctx, "conversation_id", None),
        )

    # ── running ──────────────────────────────────────────────────────────────

    async def run(
        self,
        child: AgentIdentity,
        *,
        task: str,
        ctx: Any,
        budget: AgentBudget | None = None,
        step: Any = None,
    ) -> AgentRun:
        """Drive one delegated agent to completion, or to its budget.

        ``step`` is the per-iteration callable — injected rather than built here
        so this class stays about *supervision*. It receives the child's
        :class:`~jarvis.tools.base.ToolContext` and returns ``(done, summary)``.
        The default is a single no-op step, which is what makes this testable
        without a provider.
        """
        budget = budget or AgentBudget()
        self._running[child.agent_id] = budget
        run = AgentRun(identity=child, task=task)

        child_ctx = self._child_context(ctx, child)
        try:
            while True:
                self._check_stop()
                budget.tick()
                run.steps = budget.steps
                if step is None:
                    run.summary = "no step function supplied"
                    break
                done, summary = await asyncio.wait_for(
                    step(child_ctx, run),
                    timeout=max(1.0, budget.remaining_seconds),
                )
                if summary:
                    run.summary = summary
                if done:
                    break
        except AgentBudgetExceeded as exc:
            run.stopped_because = str(exc)
            log.warning("agent_budget_exceeded", agent_id=child.agent_id,
                        reason=str(exc))
        except asyncio.TimeoutError:
            run.stopped_because = "timed out"
            log.warning("agent_timed_out", agent_id=child.agent_id)
        except AgentDepthExceeded as exc:
            run.stopped_because = str(exc)
        finally:
            self._running.pop(child.agent_id, None)
            # Taint travels up. A child that read a page must not be able to
            # hand its parent a clean-looking summary — that is the ambient
            # memory defect wearing a different hat.
            if run.tainted:
                setattr(ctx, "tainted", True)

        return run

    def cancel(self, agent_id: str) -> bool:
        """Spend a running agent's budget, so it stops at its next step."""
        budget = self._running.get(agent_id)
        if budget is None:
            return False
        budget.steps = budget.max_steps + 1
        log.info("agent_cancelled", agent_id=agent_id)
        return True

    @property
    def running(self) -> list[str]:
        return list(self._running)

    # ── internals ────────────────────────────────────────────────────────────

    def _check_stop(self) -> None:
        stop = self.emergency_stop
        if stop is not None and getattr(stop, "engaged", False):
            raise AgentBudgetExceeded("the emergency stop was engaged")

    @staticmethod
    def _child_context(ctx: Any, child: AgentIdentity) -> Any:
        """The parent's context, re-pointed at the child.

        Deliberately a copy rather than a mutation: the parent keeps acting
        after the child finishes, and an identity left behind on a shared
        object would silently apply the child's ceiling to the parent — or,
        worse, the parent's to the next child.

        Taint is inherited, never reset. A child spawned by a poisoned turn is
        a poisoned turn.
        """
        from dataclasses import replace as _replace

        return _replace(ctx, agent=child)
