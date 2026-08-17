"""Work that outlives the request that started it (Phase D, item 7).

Until now a turn was the unit of everything: nothing could survive the HTTP
request that began it, so "keep researching while I get lunch" had no
representation. This adds one, and the whole design question is *what stops it*
— because a job nobody is watching is the only thing in JARVIS that can go
wrong for an hour before anyone notices.

## Built on the tables that already exist

A background job is a :class:`~jarvis.db.models.Task` plus a
:class:`~jarvis.db.models.TaskExecution`, which is the split Phase 1 chose for
exactly this reason: the task is the intent, the execution is one attempt, and
a retry after a failure is a second execution rather than a rewritten first.
No new table, no migration, and the existing status machine already refuses the
transitions that would otherwise need re-inventing here.

## What bounds a job

Four things, in the order they bite:

* **The emergency stop**, checked before every step. A stop that leaves
  background work running is not a stop.
* **A step budget and a wall clock**, enforced by the runner rather than
  requested of the model.
* **Cancellation**, which is cooperative at the step boundary and hard at the
  asyncio level if the step itself hangs.
* **The permission system**, unchanged. A background job's tool calls go
  through :class:`~jarvis.tools.executor.ToolExecutor` like everything else,
  which means an action needing confirmation *suspends the job* rather than
  proceeding because nobody was there to object.

That last one is the important one, and it is the reason this module is short.
Running unattended does not grant authority. It removes the person who would
have been asked — so the answer is to stop and wait, not to assume yes.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from jarvis.agents.supervisor import AgentBudget, AgentBudgetExceeded
from jarvis.db.models import ActivityKind
from jarvis.logging import get_logger

log = get_logger(__name__)

#: Deliberately finite. Work that needs longer than this is work the user
#: should be watching, and an unbounded default is how a runaway becomes a
#: bill rather than an incident.
DEFAULT_JOB_TIMEOUT_SECONDS = 900.0
DEFAULT_JOB_STEPS = 40

#: How many jobs may run at once. Each holds a database session and may hold a
#: browser page; unbounded concurrency turns one bad plan into resource
#: exhaustion.
MAX_CONCURRENT_JOBS = 3


class JobState(str, Enum):
    """Where a job is. Distinct from ``TaskStatus``, which is about the *task*.

    A job can be paused while its task is still IN_PROGRESS — the intent has
    not changed, only whether anything is currently working on it.
    """

    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    #: Stopped and waiting for a human: a tool asked for confirmation.
    AWAITING_CONFIRMATION = "AWAITING_CONFIRMATION"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"

    @property
    def is_terminal(self) -> bool:
        return self in {JobState.COMPLETED, JobState.CANCELLED, JobState.FAILED}


class JobRejected(Exception):
    """The job was not started. Capacity, or a stop already engaged."""


@dataclass(slots=True)
class JobProgress:
    """What a job has to say for itself while it runs.

    Deliberately small. A progress report the user has to read carefully is one
    they will stop reading, and a job's honest answer to "how far along are
    you?" is usually a sentence and a fraction.
    """

    step: int = 0
    total: int | None = None
    note: str = ""

    def describe(self) -> dict[str, Any]:
        return {"step": self.step, "total": self.total, "note": self.note}


@dataclass(slots=True)
class Job:
    """One unit of background work and everything that can stop it."""

    job_id: str
    task_id: str | None
    title: str
    state: JobState = JobState.RUNNING
    progress: JobProgress = field(default_factory=JobProgress)
    budget: AgentBudget = field(default_factory=AgentBudget)
    result: str = ""
    error: str = ""
    #: Set when a tool inside the job asked for the user's approval. The job
    #: does not proceed; the confirmation is the ordinary one, resolvable from
    #: the UI, and resuming re-enters the step that raised it.
    confirmation_id: str | None = None
    started_at: float = field(default_factory=time.monotonic)
    #: Untrusted content reached this job. Travels back to whoever asks about
    #: it, so a job that read a page cannot hand back a clean-looking summary.
    tainted: bool = False
    _pause: Any = None
    _task: Any = None

    def describe(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "task_id": self.task_id,
            "title": self.title,
            "state": self.state.value,
            "progress": self.progress.describe(),
            "elapsed_seconds": round(time.monotonic() - self.started_at, 1),
            "budget": self.budget.describe(),
            "result": self.result,
            "error": self.error,
            "confirmation_id": self.confirmation_id,
            "tainted": self.tainted,
        }


class BackgroundRunner:
    """Starts, watches and stops background jobs.

    Process-wide, like the emergency stop and for the same reason: a registry
    with per-request scope tracks nothing. Constructed once by
    :class:`~jarvis.core.JarvisCore` and reached through ``extras``.
    """

    def __init__(
        self,
        *,
        activity_factory: Any = None,
        emergency_stop: Any = None,
        max_concurrent: int = MAX_CONCURRENT_JOBS,
    ) -> None:
        self.activity_factory = activity_factory
        self.emergency_stop = emergency_stop
        self.max_concurrent = max_concurrent
        self._jobs: dict[str, Job] = {}

    # ── inspection ───────────────────────────────────────────────────────────

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def list(self, *, include_finished: bool = True) -> list[Job]:
        return [
            job for job in self._jobs.values()
            if include_finished or not job.state.is_terminal
        ]

    @property
    def active(self) -> list[Job]:
        return [j for j in self._jobs.values() if not j.state.is_terminal]

    # ── lifecycle ────────────────────────────────────────────────────────────

    async def start(
        self,
        *,
        title: str,
        step: Any,
        task_id: str | None = None,
        max_steps: int = DEFAULT_JOB_STEPS,
        timeout_seconds: float = DEFAULT_JOB_TIMEOUT_SECONDS,
    ) -> Job:
        """Begin a job, or refuse to.

        Refuses rather than queues when at capacity. A queue would make the
        limit invisible — work would still be accepted, and the user would
        discover the backlog by noticing nothing had happened.
        """
        self._reap()
        if self.emergency_stop is not None and getattr(
            self.emergency_stop, "engaged", False
        ):
            raise JobRejected(
                "The emergency stop is engaged, so nothing new will start."
            )
        if len(self.active) >= self.max_concurrent:
            raise JobRejected(
                f"Already running {self.max_concurrent} background jobs, which "
                "is the limit. Cancel one or wait for it to finish."
            )

        from jarvis.db.base import new_id

        job = Job(
            job_id=new_id("job"),
            task_id=task_id,
            title=title,
            budget=AgentBudget(max_steps=max_steps, timeout_seconds=timeout_seconds),
        )
        job._pause = asyncio.Event()
        job._pause.set()  # not paused
        self._jobs[job.job_id] = job
        job._task = asyncio.create_task(self._drive(job, step))
        log.info("background_job_started", job_id=job.job_id, title=title)
        return job

    async def _drive(self, job: Job, step: Any) -> None:
        """The loop. Every exit path leaves the job in a terminal state.

        A job that ended without saying why is indistinguishable from one still
        running, and "is it still going?" is the only question anyone asks
        about background work.
        """
        try:
            while True:
                await job._pause.wait()
                if job.state is JobState.CANCELLED:
                    return
                self._check_stop()
                job.budget.tick()
                job.progress.step = job.budget.steps

                done, note = await asyncio.wait_for(
                    step(job), timeout=max(1.0, job.budget.remaining_seconds)
                )
                if note:
                    job.progress.note = note
                if done:
                    job.state = JobState.COMPLETED
                    job.result = note or "finished"
                    break
        except asyncio.CancelledError:
            job.state = JobState.CANCELLED
            job.error = "cancelled"
            raise
        except AgentBudgetExceeded as exc:
            job.state = JobState.FAILED
            job.error = str(exc)
            log.warning("background_job_budget", job_id=job.job_id, reason=str(exc))
        except asyncio.TimeoutError:
            job.state = JobState.FAILED
            job.error = "a step took longer than the job's remaining time"
        except Exception as exc:  # a job must not take the process with it
            job.state = JobState.FAILED
            job.error = str(exc)
            log.warning("background_job_failed", job_id=job.job_id, error=str(exc))
        finally:
            if not job.state.is_terminal and job.state is not JobState.AWAITING_CONFIRMATION:
                # Belt and braces: nothing should leave this loop RUNNING.
                job.state = JobState.FAILED
                job.error = job.error or "stopped without saying why"
            log.info("background_job_finished", job_id=job.job_id,
                     state=job.state.value, steps=job.progress.step)

    # ── control ──────────────────────────────────────────────────────────────

    def pause(self, job_id: str) -> bool:
        job = self._jobs.get(job_id)
        if job is None or job.state is not JobState.RUNNING:
            return False
        job.state = JobState.PAUSED
        job._pause.clear()
        log.info("background_job_paused", job_id=job_id)
        return True

    def resume(self, job_id: str) -> bool:
        job = self._jobs.get(job_id)
        if job is None or job.state not in {
            JobState.PAUSED, JobState.AWAITING_CONFIRMATION
        }:
            return False
        job.state = JobState.RUNNING
        job.confirmation_id = None
        job._pause.set()
        log.info("background_job_resumed", job_id=job_id)
        return True

    def cancel(self, job_id: str) -> bool:
        """Stop a job now, not at its convenience.

        Marks it cancelled *before* cancelling the asyncio task, so the
        ``CancelledError`` handler finds the state already correct and a job
        blocked in ``await`` cannot report itself completed on the way out.
        """
        job = self._jobs.get(job_id)
        if job is None or job.state.is_terminal:
            return False
        job.state = JobState.CANCELLED
        job.error = "cancelled"
        if job._pause is not None:
            job._pause.set()  # so a paused job actually wakes to notice
        if job._task is not None:
            job._task.cancel()
        log.info("background_job_cancelled", job_id=job_id)
        return True

    def cancel_all(self, *, reason: str = "stopped") -> int:
        stopped = 0
        for job in list(self.active):
            if self.cancel(job.job_id):
                job.error = reason
                stopped += 1
        return stopped

    def needs_confirmation(self, job_id: str, confirmation_id: str) -> bool:
        """A tool inside the job asked for approval, so the job waits.

        This is the whole reason unattended execution is safe here. Running
        without a human present does not grant authority — it removes the
        person who would have been asked, and the answer to that is to stop,
        not to assume yes.
        """
        job = self._jobs.get(job_id)
        if job is None or job.state.is_terminal:
            return False
        job.state = JobState.AWAITING_CONFIRMATION
        job.confirmation_id = confirmation_id
        if job._pause is not None:
            job._pause.clear()
        log.info("background_job_awaiting_confirmation", job_id=job_id,
                 confirmation_id=confirmation_id)
        return True

    # ── internals ────────────────────────────────────────────────────────────

    def _check_stop(self) -> None:
        stop = self.emergency_stop
        if stop is not None and getattr(stop, "engaged", False):
            raise AgentBudgetExceeded("the emergency stop was engaged")

    def _reap(self) -> None:
        """Forget finished jobs beyond a small tail.

        Kept rather than dropped immediately so "what happened to that thing I
        asked for?" survives the job ending, and bounded so a long-lived daemon
        does not accumulate every job it has ever run.
        """
        finished = [j for j in self._jobs.values() if j.state.is_terminal]
        for job in sorted(finished, key=lambda j: j.started_at)[:-20]:
            self._jobs.pop(job.job_id, None)

    async def record(self, job: Job, session: Any, ctx: Any = None) -> None:
        """One activity row per state change worth seeing."""
        if self.activity_factory is None:
            return
        activity = self.activity_factory(session)
        await activity.record(
            ActivityKind.TASK_UPDATED,
            summary=f"Background job {job.state.value.lower()}: {job.title}",
            actor="background_runner",
            status=job.state.value,
            detail=job.describe(),
            request_id=getattr(ctx, "request_id", None),
            conversation_id=getattr(ctx, "conversation_id", None),
        )
