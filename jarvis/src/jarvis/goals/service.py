"""Goals, commitments and following up (Phase D, item 12).

JARVIS was entirely reactive: it could hold a task list, and nothing made it
*come back* to one. `vierisid/jarvis` fills that with a goals package —
accountability, rhythm, commitments, deadlines — and the capability is worth
having. Its implementation is not worth copying wholesale: it is a second
object graph beside its task system, with its own status vocabulary.

## No new table

A goal is a :class:`~jarvis.db.models.Task` with sub-tasks. The table already
carries ``parent_task_id``, ``due_at``, ``priority``, a status machine that
refuses illegal transitions, and field-level history. What was missing was not
storage — it was three questions nobody could ask:

* how far along is this?
* what is due, or overdue?
* what has gone quiet?

Those are queries, so this module is queries. A ``goals`` table would have
duplicated the status machine, and the second copy is the one that drifts.

## A commitment is a promise with a date on it

The distinction worth keeping from `vierisid` is *commitment* versus *task*: "I
should tidy the vault sometime" and "I told Sam I would send it by Friday" are
different objects, and only the second one should chase you. Marked in ``meta``
rather than by a column, because it is a property of how the task was created
rather than of what it is.

## What this module deliberately cannot do

It does not act. Nothing here executes a task, sends a reminder, or starts a
job — it answers questions, and the caller decides what to do about the answer.
That boundary matters: a goal system that could act on its own schedule is an
autonomous actor, and autonomous actors in this codebase go through
``ToolExecutor`` like everything else. Making the *reminder* a tool call keeps
that true; making it a side effect in here would not.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from jarvis.db.base import utcnow
from jarvis.db.models import Task, TaskPriority, TaskStatus
from jarvis.logging import get_logger

log = get_logger(__name__)

#: Marks a task as a promise rather than an intention.
COMMITMENT_KEY = "commitment"

#: How long a goal may sit untouched before it counts as gone quiet. Two weeks
#: is long enough not to nag about something being thought about, and short
#: enough that a forgotten commitment surfaces while it still matters.
STALE_AFTER_DAYS = 14

#: How far ahead "due soon" looks.
SOON_DAYS = 3


@dataclass(slots=True)
class GoalProgress:
    """How far along a goal is, counted rather than estimated.

    Deliberately not a model-produced percentage. "About 60% done" from a
    language model is a number with no referent, and a progress bar that moves
    for no reason is worse than none.
    """

    goal_id: str
    title: str
    total: int = 0
    completed: int = 0
    blocked: int = 0
    due_at: datetime | None = None
    is_commitment: bool = False

    @property
    def fraction(self) -> float:
        return round(self.completed / self.total, 2) if self.total else 0.0

    @property
    def overdue(self) -> bool:
        if self.due_at is None or self.completed == self.total and self.total:
            return False
        due = self.due_at
        if due.tzinfo is None:
            due = due.replace(tzinfo=utcnow().tzinfo)
        return due < utcnow()

    def describe(self) -> dict[str, Any]:
        return {
            "goal_id": self.goal_id,
            "title": self.title,
            "steps": {"total": self.total, "completed": self.completed,
                      "blocked": self.blocked},
            "fraction": self.fraction,
            "due_at": self.due_at.isoformat() if self.due_at else None,
            "overdue": self.overdue,
            "is_commitment": self.is_commitment,
        }


@dataclass(slots=True)
class FollowUp:
    """Something worth raising, and why.

    The reason travels with it because "JARVIS reminded me about this" is only
    useful if it can say what prompted it. A nudge with no stated cause reads
    as nagging.
    """

    task_id: str
    title: str
    reason: str
    due_at: datetime | None = None
    is_commitment: bool = False

    def describe(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "title": self.title,
            "reason": self.reason,
            "due_at": self.due_at.isoformat() if self.due_at else None,
            "is_commitment": self.is_commitment,
        }


class GoalService:
    """Queries over the task tree. Answers questions; never acts."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # ── creating ─────────────────────────────────────────────────────────────

    async def create_goal(
        self,
        user_id: str,
        *,
        title: str,
        steps: list[str] | None = None,
        due_at: datetime | None = None,
        is_commitment: bool = False,
        description: str | None = None,
        project_id: str | None = None,
    ) -> Task:
        """A goal, and its steps, as one task tree.

        Steps are created TODO and in order. Their absence is fine — a goal
        with no steps is a goal nobody has broken down yet, which is a normal
        state and not an error.
        """
        goal = Task(
            user_id=user_id,
            title=title.strip()[:500],
            description=description,
            status=TaskStatus.TODO,
            priority=TaskPriority.HIGH if is_commitment else TaskPriority.NORMAL,
            due_at=due_at,
            project_id=project_id,
            meta={COMMITMENT_KEY: bool(is_commitment), "is_goal": True},
        )
        self.session.add(goal)
        await self.session.flush()

        for step in steps or []:
            self.session.add(
                Task(
                    user_id=user_id,
                    parent_task_id=goal.id,
                    title=step.strip()[:500],
                    status=TaskStatus.TODO,
                    project_id=project_id,
                    # A step inherits the deadline of the thing it serves.
                    # Without this, "due Friday" would apply to the goal while
                    # every step looked open-ended, and the follow-up queries
                    # below would never surface them.
                    due_at=due_at,
                )
            )
        await self.session.flush()
        log.info("goal_created", goal_id=goal.id, steps=len(steps or []),
                 commitment=is_commitment)
        return goal

    # ── asking ───────────────────────────────────────────────────────────────

    async def progress(self, goal_id: str) -> GoalProgress:
        goal = await self.session.get(Task, goal_id)
        if goal is None:
            raise LookupError(f"No task {goal_id}")

        children = (
            await self.session.execute(
                select(Task).where(Task.parent_task_id == goal.id)
            )
        ).scalars().all()

        # A goal with no steps reports on itself, so "how far along?" always has
        # an answer rather than dividing by zero and returning nothing.
        units = children or [goal]
        return GoalProgress(
            goal_id=goal.id,
            title=goal.title,
            total=len(units),
            completed=sum(1 for t in units if t.status is TaskStatus.COMPLETED),
            blocked=sum(1 for t in units if t.status is TaskStatus.BLOCKED),
            due_at=goal.due_at,
            is_commitment=bool((goal.meta or {}).get(COMMITMENT_KEY)),
        )

    async def follow_ups(
        self,
        user_id: str,
        *,
        soon_days: int = SOON_DAYS,
        stale_after_days: int = STALE_AFTER_DAYS,
    ) -> list[FollowUp]:
        """What is worth raising with the user, most pressing first.

        Three reasons, in descending order of how much they deserve to
        interrupt: overdue, due soon, gone quiet. A commitment outranks an
        intention within each, because breaking a promise to somebody else
        costs more than missing a note to yourself.
        """
        now = utcnow()
        soon = now + timedelta(days=soon_days)
        stale_before = now - timedelta(days=stale_after_days)

        open_tasks = (
            await self.session.execute(
                select(Task).where(
                    Task.user_id == user_id,
                    Task.status.in_(
                        [TaskStatus.TODO, TaskStatus.IN_PROGRESS,
                         TaskStatus.WAITING, TaskStatus.BLOCKED]
                    ),
                )
            )
        ).scalars().all()

        found: list[FollowUp] = []
        for task in open_tasks:
            commitment = bool((task.meta or {}).get(COMMITMENT_KEY))
            due = _aware(task.due_at)
            if due is not None and due < now:
                reason = "overdue"
            elif due is not None and due <= soon:
                reason = "due soon"
            elif _aware(task.updated_at) is not None and _aware(task.updated_at) < stale_before:
                reason = f"no movement in {stale_after_days} days"
            else:
                continue
            found.append(
                FollowUp(task_id=task.id, title=task.title, reason=reason,
                         due_at=task.due_at, is_commitment=commitment)
            )

        order = {"overdue": 0, "due soon": 1}
        found.sort(
            key=lambda f: (order.get(f.reason, 2), 0 if f.is_commitment else 1)
        )
        return found

    async def goals(self, user_id: str) -> list[GoalProgress]:
        rows = (
            await self.session.execute(
                select(Task).where(
                    Task.user_id == user_id, Task.parent_task_id.is_(None)
                )
            )
        ).scalars().all()
        wanted = [t for t in rows if (t.meta or {}).get("is_goal")]
        return [await self.progress(task.id) for task in wanted]


def _aware(value: datetime | None) -> datetime | None:
    """SQLite hands back naive datetimes; comparing one to an aware ``now``
    raises. Normalised here rather than at four call sites."""
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(
        tzinfo=utcnow().tzinfo
    )
