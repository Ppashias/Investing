"""Task engine.

Implements the task/execution split the brief asks for in §10 and the audit
argued for: a **task** is what JARVIS intends to accomplish; an **execution**
is one attempt at it. "Create promotional video" is one task; the attempt that
failed because the editor was unavailable and the attempt that succeeded are
two executions of it.

Keeping them separate from the start is what lets Phase 10 retry autonomously
without losing the history of what was already tried and why it failed.

Every field change is recorded in ``task_history`` — not just status, because
"who moved the deadline and when" is the question that actually gets asked.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from jarvis.db.base import utcnow
from jarvis.db.models import (
    ExecutionStatus,
    Task,
    TaskExecution,
    TaskHistory,
    TaskPriority,
    TaskStatus,
)
from jarvis.errors import InvalidStateTransitionError, NotFoundError, ValidationError
from jarvis.logging import get_logger

log = get_logger(__name__)

#: Legal status transitions. Anything absent is rejected — a task cannot go
#: from COMPLETED back to IN_PROGRESS by accident, and an agent cannot quietly
#: resurrect a cancelled task.
ALLOWED_TRANSITIONS: dict[TaskStatus, frozenset[TaskStatus]] = {
    TaskStatus.TODO: frozenset(
        {TaskStatus.IN_PROGRESS, TaskStatus.WAITING, TaskStatus.BLOCKED,
         TaskStatus.CANCELLED, TaskStatus.COMPLETED}
    ),
    TaskStatus.IN_PROGRESS: frozenset(
        {TaskStatus.WAITING, TaskStatus.BLOCKED, TaskStatus.COMPLETED,
         TaskStatus.FAILED, TaskStatus.CANCELLED, TaskStatus.TODO}
    ),
    TaskStatus.WAITING: frozenset(
        {TaskStatus.IN_PROGRESS, TaskStatus.BLOCKED, TaskStatus.CANCELLED,
         TaskStatus.COMPLETED, TaskStatus.FAILED}
    ),
    TaskStatus.BLOCKED: frozenset(
        {TaskStatus.TODO, TaskStatus.IN_PROGRESS, TaskStatus.WAITING,
         TaskStatus.CANCELLED, TaskStatus.FAILED}
    ),
    # FAILED is not terminal: a failed task can be retried, which is the whole
    # point of separating executions from tasks.
    TaskStatus.FAILED: frozenset({TaskStatus.TODO, TaskStatus.IN_PROGRESS,
                                  TaskStatus.CANCELLED}),
    TaskStatus.COMPLETED: frozenset({TaskStatus.IN_PROGRESS}),
    TaskStatus.CANCELLED: frozenset({TaskStatus.TODO}),
}

_MUTABLE_FIELDS = {
    "title", "description", "priority", "due_at", "assigned_agent",
    "project_id", "tags",
}


@dataclass(slots=True)
class TaskFilter:
    status: Sequence[TaskStatus] | None = None
    priority: Sequence[TaskPriority] | None = None
    project_id: str | None = None
    parent_task_id: str | None = None
    assigned_agent: str | None = None
    conversation_id: str | None = None
    include_terminal: bool = True
    due_before: datetime | None = None
    search: str | None = None
    limit: int = 100
    offset: int = 0


class TaskService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # ── creation ─────────────────────────────────────────────────────────────

    async def create(
        self,
        *,
        user_id: str,
        title: str,
        description: str | None = None,
        priority: TaskPriority = TaskPriority.NORMAL,
        due_at: datetime | None = None,
        project_id: str | None = None,
        parent_task_id: str | None = None,
        conversation_id: str | None = None,
        assigned_agent: str | None = None,
        tags: list[str] | None = None,
        actor: str = "user",
    ) -> Task:
        title = (title or "").strip()
        if not title:
            raise ValidationError(
                "Task title cannot be empty",
                user_message="A task needs a title.",
            )

        if parent_task_id:
            parent = await self.session.get(Task, parent_task_id)
            if parent is None:
                raise NotFoundError(f"Parent task {parent_task_id} not found")
            if parent.user_id != user_id:
                raise ValidationError("Parent task belongs to a different user")

        task = Task(
            user_id=user_id,
            title=title[:500],
            description=description,
            priority=priority,
            due_at=due_at,
            project_id=project_id,
            parent_task_id=parent_task_id,
            conversation_id=conversation_id,
            assigned_agent=assigned_agent,
            tags=tags or [],
            status=TaskStatus.TODO,
        )
        self.session.add(task)
        await self.session.flush()

        self.session.add(
            TaskHistory(
                task_id=task.id, field="created", new_value=title, actor=actor
            )
        )
        await self.session.flush()
        log.info("task_created", task_id=task.id, title=title[:80])
        return task

    # ── retrieval ────────────────────────────────────────────────────────────

    async def get(self, task_id: str, *, with_relations: bool = False) -> Task:
        if with_relations:
            stmt = (
                select(Task)
                .where(Task.id == task_id)
                .options(
                    selectinload(Task.executions),
                    selectinload(Task.history),
                    selectinload(Task.subtasks),
                )
            )
            task = (await self.session.execute(stmt)).scalars().first()
        else:
            task = await self.session.get(Task, task_id)
        if task is None:
            raise NotFoundError(
                f"Task {task_id} not found",
                user_message="I could not find that task.",
            )
        return task

    async def list(self, user_id: str, filters: TaskFilter | None = None) -> list[Task]:
        f = filters or TaskFilter()
        stmt = select(Task).where(Task.user_id == user_id)

        if f.status:
            stmt = stmt.where(Task.status.in_(list(f.status)))
        elif not f.include_terminal:
            stmt = stmt.where(
                Task.status.notin_([TaskStatus.COMPLETED, TaskStatus.CANCELLED])
            )
        if f.priority:
            stmt = stmt.where(Task.priority.in_(list(f.priority)))
        if f.project_id:
            stmt = stmt.where(Task.project_id == f.project_id)
        if f.parent_task_id:
            stmt = stmt.where(Task.parent_task_id == f.parent_task_id)
        if f.assigned_agent:
            stmt = stmt.where(Task.assigned_agent == f.assigned_agent)
        if f.conversation_id:
            stmt = stmt.where(Task.conversation_id == f.conversation_id)
        if f.due_before:
            stmt = stmt.where(Task.due_at.is_not(None), Task.due_at <= f.due_before)
        if f.search:
            like = f"%{f.search.strip()}%"
            stmt = stmt.where(Task.title.ilike(like) | Task.description.ilike(like))

        stmt = (
            stmt.order_by(Task.created_at.desc())
            .limit(min(f.limit, 500))
            .offset(max(f.offset, 0))
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def counts_by_status(self, user_id: str) -> dict[str, int]:
        stmt = (
            select(Task.status, func.count(Task.id))
            .where(Task.user_id == user_id)
            .group_by(Task.status)
        )
        rows = (await self.session.execute(stmt)).all()
        counts = {s.value: 0 for s in TaskStatus}
        for status, count in rows:
            key = status.value if hasattr(status, "value") else str(status)
            counts[key] = count
        return counts

    # ── mutation ─────────────────────────────────────────────────────────────

    async def update(
        self,
        task_id: str,
        *,
        actor: str = "user",
        note: str | None = None,
        **changes: Any,
    ) -> Task:
        task = await self.get(task_id)
        unknown = set(changes) - _MUTABLE_FIELDS - {"status"}
        if unknown:
            raise ValidationError(
                f"Cannot update fields: {sorted(unknown)}",
                user_message="Some of those task fields cannot be changed.",
            )

        if "status" in changes and changes["status"] is not None:
            await self._transition(task, TaskStatus(changes.pop("status")), actor, note)
        else:
            changes.pop("status", None)

        for field, new_value in changes.items():
            if new_value is None:
                continue
            old_value = getattr(task, field)
            if old_value == new_value:
                continue
            setattr(task, field, new_value)
            self.session.add(
                TaskHistory(
                    task_id=task.id,
                    field=field,
                    old_value=_stringify(old_value),
                    new_value=_stringify(new_value),
                    actor=actor,
                    note=note,
                )
            )

        task.updated_at = utcnow()
        await self.session.flush()
        log.info("task_updated", task_id=task.id, fields=sorted(changes))
        return task

    async def _transition(
        self, task: Task, new_status: TaskStatus, actor: str, note: str | None
    ) -> None:
        current = task.status
        if current == new_status:
            return
        allowed = ALLOWED_TRANSITIONS.get(current, frozenset())
        if new_status not in allowed:
            raise InvalidStateTransitionError(
                f"Cannot move task from {current.value} to {new_status.value}",
                user_message=(
                    f"That task is {current.value.lower().replace('_', ' ')}; "
                    f"it cannot become {new_status.value.lower().replace('_', ' ')}."
                ),
                details={
                    "from": current.value,
                    "to": new_status.value,
                    "allowed": sorted(s.value for s in allowed),
                },
            )

        task.status = new_status
        task.completed_at = utcnow() if new_status is TaskStatus.COMPLETED else None
        self.session.add(
            TaskHistory(
                task_id=task.id,
                field="status",
                old_value=current.value,
                new_value=new_status.value,
                actor=actor,
                note=note,
            )
        )
        log.info(
            "task_status_changed",
            task_id=task.id,
            **{"from": current.value, "to": new_status.value},
        )

    async def complete(self, task_id: str, *, actor: str = "user",
                       note: str | None = None) -> Task:
        return await self.update(task_id, status=TaskStatus.COMPLETED, actor=actor,
                                 note=note)

    async def cancel(self, task_id: str, *, actor: str = "user",
                     note: str | None = None) -> Task:
        return await self.update(task_id, status=TaskStatus.CANCELLED, actor=actor,
                                 note=note)

    async def history(self, task_id: str) -> list[TaskHistory]:
        stmt = (
            select(TaskHistory)
            .where(TaskHistory.task_id == task_id)
            .order_by(TaskHistory.created_at.asc())
        )
        return list((await self.session.execute(stmt)).scalars().all())

    # ── executions ───────────────────────────────────────────────────────────

    async def start_execution(
        self,
        task_id: str,
        *,
        trigger: str = "manual",
        agent: str | None = None,
        provider: str | None = None,
        model: str | None = None,
    ) -> TaskExecution:
        task = await self.get(task_id)
        next_attempt = (
            await self.session.execute(
                select(func.coalesce(func.max(TaskExecution.attempt), 0)).where(
                    TaskExecution.task_id == task_id
                )
            )
        ).scalar_one() + 1

        execution = TaskExecution(
            task_id=task_id,
            attempt=next_attempt,
            status=ExecutionStatus.RUNNING,
            trigger=trigger,
            agent=agent,
            provider=provider,
            model=model,
        )
        self.session.add(execution)

        if task.status in (TaskStatus.TODO, TaskStatus.WAITING):
            await self._transition(task, TaskStatus.IN_PROGRESS, agent or "system", None)

        await self.session.flush()
        log.info("task_execution_started", task_id=task_id, attempt=next_attempt)
        return execution

    async def finish_execution(
        self,
        execution_id: str,
        *,
        status: ExecutionStatus,
        result: dict[str, Any] | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        duration_ms: float | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        propagate_to_task: bool = True,
    ) -> TaskExecution:
        execution = await self.session.get(TaskExecution, execution_id)
        if execution is None:
            raise NotFoundError(f"Task execution {execution_id} not found")

        execution.status = status
        execution.result = result
        execution.error_code = error_code
        execution.error_message = (error_message or None) and error_message[:2000]
        execution.duration_ms = duration_ms
        execution.input_tokens = input_tokens
        execution.output_tokens = output_tokens
        execution.finished_at = utcnow()

        if propagate_to_task:
            task = await self.get(execution.task_id)
            target = {
                ExecutionStatus.SUCCEEDED: TaskStatus.COMPLETED,
                ExecutionStatus.FAILED: TaskStatus.FAILED,
                ExecutionStatus.CANCELLED: TaskStatus.CANCELLED,
                ExecutionStatus.AWAITING_CONFIRMATION: TaskStatus.WAITING,
            }.get(status)
            if target is not None and target in ALLOWED_TRANSITIONS.get(
                task.status, frozenset()
            ):
                await self._transition(task, target, "system", f"execution {status.value}")

        await self.session.flush()
        log.info(
            "task_execution_finished",
            task_id=execution.task_id,
            attempt=execution.attempt,
            status=status.value,
        )
        return execution

    async def executions(self, task_id: str) -> list[TaskExecution]:
        stmt = (
            select(TaskExecution)
            .where(TaskExecution.task_id == task_id)
            .order_by(TaskExecution.attempt.asc())
        )
        return list((await self.session.execute(stmt)).scalars().all())

    # ── serialisation ────────────────────────────────────────────────────────

    @staticmethod
    def to_dict(task: Task, *, executions: list[TaskExecution] | None = None,
                history: list[TaskHistory] | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": task.id,
            "title": task.title,
            "description": task.description,
            "status": task.status.value,
            "priority": task.priority.value,
            "project_id": task.project_id,
            "parent_task_id": task.parent_task_id,
            "conversation_id": task.conversation_id,
            "assigned_agent": task.assigned_agent,
            "tags": task.tags or [],
            "due_at": _iso(task.due_at),
            "created_at": _iso(task.created_at),
            "updated_at": _iso(task.updated_at),
            "completed_at": _iso(task.completed_at),
        }
        if executions is not None:
            payload["executions"] = [
                {
                    "id": e.id,
                    "attempt": e.attempt,
                    "status": e.status.value,
                    "trigger": e.trigger,
                    "agent": e.agent,
                    "provider": e.provider,
                    "model": e.model,
                    "error_code": e.error_code,
                    "error_message": e.error_message,
                    "duration_ms": e.duration_ms,
                    "started_at": _iso(e.started_at),
                    "finished_at": _iso(e.finished_at),
                }
                for e in executions
            ]
        if history is not None:
            payload["history"] = [
                {
                    "field": h.field,
                    "old_value": h.old_value,
                    "new_value": h.new_value,
                    "actor": h.actor,
                    "note": h.note,
                    "created_at": _iso(h.created_at),
                }
                for h in history
            ]
        return payload


def _stringify(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    if hasattr(value, "value"):
        return str(value.value)
    return str(value)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None
