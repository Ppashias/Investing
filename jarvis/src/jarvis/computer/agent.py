"""The computer agent — the closed loop (§1, §9, §10, §11, §29, §31).

    PLAN → ACT → OBSERVE → VERIFY → REPLAN

rather than plan-fifty-actions-then-execute. §10 gives the reason and it is
worth restating precisely: an agent that plans ahead is planning against a
screen that no longer exists by step three. Every mis-click compounds, and the
first wrong assumption silently invalidates everything after it. Looping costs
one model call per step and is why a wrong click becomes a retry instead of a
cascade.

## Where each safety property lives

The loop itself enforces almost nothing. It calls
:class:`~jarvis.computer.executor.ActionExecutor`, which is where permission,
confirmation, the emergency stop, timeouts and audit live. What the loop owns
is the *sequencing* rules that only make sense across steps:

* **Step and time limits** (§28) — a task cannot run forever.
* **Retry limits** (§31) — the same failing action is not repeated, and a
  destructive action is never retried at all.
* **Human handoff** (§29) — a login, a CAPTCHA, an unfamiliar screen: the loop
  stops and says what it needs rather than guessing.
* **Cancellation** — checked between every step, so a stop lands mid-task.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from jarvis.computer.control import EmergencyStop
from jarvis.computer.executor import ActionExecutor, ExecutionContext
from jarvis.computer.observation import ObservationOptions, ObservationProcessor
from jarvis.computer.reasoner import ComputerReasoner
from jarvis.computer.types import (
    ActionKind,
    ActionOutcome,
    ActionRisk,
    ComputerAction,
    ComputerScope,
    ComputerTaskStatus,
    VerificationOutcome,
)
from jarvis.db.base import utcnow
from jarvis.db.models import ComputerTask
from jarvis.errors import ConfirmationRequiredError
from jarvis.logging import get_logger

log = get_logger(__name__)

#: Consecutive failures before the loop gives up. Three is enough for a
#: transient hiccup and few enough that a systematic misunderstanding is not
#: repeated a dozen times.
MAX_CONSECUTIVE_FAILURES = 3
#: Times the identical action may be attempted across a whole task.
MAX_IDENTICAL_ATTEMPTS = 2


@dataclass(slots=True)
class StepRecord:
    index: int
    action: str
    outcome: str
    verification: str
    detail: str = ""

    def describe(self) -> str:
        line = f"{self.index}. {self.action} -> {self.outcome}"
        if self.verification not in {"UNVERIFIED", ""}:
            line += f" ({self.verification.lower()})"
        if self.detail:
            line += f": {self.detail[:120]}"
        return line


@dataclass(slots=True)
class TaskRun:
    task_id: str
    status: ComputerTaskStatus
    steps: list[StepRecord] = field(default_factory=list)
    message: str = ""
    pending_confirmation: str | None = None
    duration_ms: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "status": self.status.value,
            "steps": [s.describe() for s in self.steps],
            "step_count": len(self.steps),
            "message": self.message,
            "pending_confirmation": self.pending_confirmation,
            "duration_ms": round(self.duration_ms, 1),
        }


class ComputerAgent:
    def __init__(
        self,
        *,
        executor: ActionExecutor,
        observation: ObservationProcessor,
        reasoner: ComputerReasoner,
        emergency_stop: EmergencyStop,
        enabled_scopes: set[ComputerScope],
        allowed_kinds: set[ActionKind] | None = None,
        max_steps: int = 25,
        task_timeout_seconds: float = 300.0,
    ) -> None:
        self.executor = executor
        self.observation = observation
        self.reasoner = reasoner
        self.emergency_stop = emergency_stop
        self.enabled_scopes = enabled_scopes
        self.allowed_kinds = allowed_kinds or {
            kind
            for kind in ActionKind
            # Only kinds whose scope the user has enabled. §34: the model must
            # not be shown a tool it may not use.
            if _scope_of(kind) in enabled_scopes
        }
        self.max_steps = max_steps
        self.task_timeout_seconds = task_timeout_seconds

    async def run(
        self,
        *,
        session: AsyncSession,
        user_id: str,
        objective: str,
        description: str | None = None,
        conversation_id: str | None = None,
        request_id: str | None = None,
    ) -> TaskRun:
        started = time.perf_counter()

        task = ComputerTask(
            user_id=user_id,
            conversation_id=conversation_id,
            description=(description or objective)[:1000],
            objective=objective,
            status=ComputerTaskStatus.PLANNING.value,
            max_steps=self.max_steps,
            scopes=sorted(s.value for s in self.enabled_scopes),
            started_at=utcnow(),
            deadline_at=utcnow() + timedelta(seconds=self.task_timeout_seconds),
        )
        session.add(task)
        await session.flush()

        run = TaskRun(task_id=task.id, status=ComputerTaskStatus.RUNNING)

        if not self.reasoner.available():
            return await self._finish(
                session, task, run, ComputerTaskStatus.BLOCKED,
                "No model provider is configured, so I cannot drive the "
                "computer. Individual actions still work through the tools.",
                started,
            )

        history: list[str] = []
        failures = 0
        attempts: dict[str, int] = {}
        deadline = time.monotonic() + self.task_timeout_seconds

        for step in range(1, self.max_steps + 1):
            if self.emergency_stop.is_cancelled(task.id):
                return await self._finish(
                    session, task, run, ComputerTaskStatus.CANCELLED,
                    "Stopped.", started,
                )
            if time.monotonic() > deadline:
                return await self._finish(
                    session, task, run, ComputerTaskStatus.FAILED,
                    f"Ran out of time after {self.task_timeout_seconds:.0f}s.",
                    started,
                )

            observation = await self._observe()
            if observation is None:
                return await self._finish(
                    session, task, run, ComputerTaskStatus.FAILED,
                    "I could not observe the screen.", started,
                )

            decision = await self.reasoner.decide(
                objective=objective,
                state=observation.state,
                image_base64=observation.image_base64,
                history=[s.describe() for s in run.steps],
                allowed_kinds=self.allowed_kinds,
                enabled_scopes=self.enabled_scopes,
                notes=observation.notes,
            )

            if decision.status == "done":
                return await self._finish(
                    session, task, run, ComputerTaskStatus.COMPLETED,
                    decision.message or "Done.", started,
                )
            if decision.status == "needs_user":
                # §29. Not a failure — the task is parked with an explanation.
                return await self._finish(
                    session, task, run, ComputerTaskStatus.WAITING_FOR_USER,
                    decision.message or "I need you to take over.", started,
                )
            if decision.status == "blocked" or decision.action is None:
                return await self._finish(
                    session, task, run, ComputerTaskStatus.BLOCKED,
                    decision.message or "I could not work out how to continue.",
                    started,
                )

            action = decision.action
            action.task_id = task.id

            fingerprint = f"{action.kind.value}:{sorted(action.params.items())}"
            attempts[fingerprint] = attempts.get(fingerprint, 0) + 1
            if attempts[fingerprint] > MAX_IDENTICAL_ATTEMPTS:
                return await self._finish(
                    session, task, run, ComputerTaskStatus.BLOCKED,
                    f"I tried the same action ({action.kind.value}) "
                    f"{MAX_IDENTICAL_ATTEMPTS} times without success. Stopping "
                    "rather than repeating it.",
                    started,
                )

            task.current_step = action.describe()[:500]
            task.step_count = step
            task.status = ComputerTaskStatus.RUNNING.value
            await session.flush()

            try:
                result = await self.executor.execute(
                    action,
                    ExecutionContext(
                        user_id=user_id, session=session, request_id=request_id,
                        task_id=task.id, sequence=step, actor="agent",
                    ),
                )
            except ConfirmationRequiredError as exc:
                run.pending_confirmation = exc.confirmation_id
                return await self._finish(
                    session, task, run, ComputerTaskStatus.WAITING_FOR_USER,
                    exc.user_message, started,
                )

            run.steps.append(
                StepRecord(
                    index=step,
                    action=action.describe(),
                    outcome=result.outcome.value,
                    verification=result.verification.value,
                    detail=result.detail,
                )
            )
            history.append(run.steps[-1].describe())

            if result.ok:
                task.completed_actions += 1
                failures = 0
                if result.verification is VerificationOutcome.CONTRADICTED:
                    # Succeeded mechanically, achieved nothing visible. Not a
                    # failure to retry — a fact for the reasoner to react to.
                    log.info("computer_action_unverified", task_id=task.id, step=step)
            else:
                task.failed_actions += 1
                failures += 1

                if result.risk in {ActionRisk.HIGH, ActionRisk.PROHIBITED}:
                    # §31: never retry a destructive operation. A delete that
                    # "failed" may have partly succeeded.
                    return await self._finish(
                        session, task, run, ComputerTaskStatus.FAILED,
                        f"A high-risk action failed and will not be retried: "
                        f"{result.detail}",
                        started,
                    )
                if failures >= MAX_CONSECUTIVE_FAILURES:
                    return await self._finish(
                        session, task, run, ComputerTaskStatus.FAILED,
                        f"{failures} actions failed in a row. Last: {result.detail}",
                        started,
                    )

            await session.flush()

        return await self._finish(
            session, task, run, ComputerTaskStatus.FAILED,
            f"Reached the {self.max_steps}-step limit without finishing.",
            started,
        )

    async def _observe(self):
        try:
            import asyncio

            return await asyncio.to_thread(
                self.observation.observe, ObservationOptions(include_image=True)
            )
        except Exception as exc:
            log.warning("computer_agent_observe_failed", error=str(exc))
            return None

    async def _finish(
        self,
        session: AsyncSession,
        task: ComputerTask,
        run: TaskRun,
        status: ComputerTaskStatus,
        message: str,
        started: float,
    ) -> TaskRun:
        run.status = status
        run.message = message
        run.duration_ms = (time.perf_counter() - started) * 1000.0

        task.status = status.value
        task.result = message if status is ComputerTaskStatus.COMPLETED else None
        task.error = message if status in {
            ComputerTaskStatus.FAILED, ComputerTaskStatus.BLOCKED
        } else None
        task.waiting_reason = (
            message if status is ComputerTaskStatus.WAITING_FOR_USER else None
        )
        task.current_step = None
        if status.is_terminal:
            task.finished_at = utcnow()
        task.observations = [s.describe() for s in run.steps][-30:]
        await session.flush()

        self.emergency_stop.clear_task(task.id)
        log.info(
            "computer_task_finished",
            task_id=task.id, status=status.value, steps=len(run.steps),
            duration_ms=round(run.duration_ms, 1),
        )
        return run


def _scope_of(kind: ActionKind) -> ComputerScope:
    from jarvis.computer.types import ACTION_SCOPE

    return ACTION_SCOPE[kind]
