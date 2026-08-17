"""Task tools — the write path that exercises permissions for real.

``create_task`` and ``update_task`` are ``WRITE`` capability, so they go
through a genuine permission decision rather than the free pass ``READ`` gets.
Both are reversible (a task can be cancelled, and every change is recorded in
task history), which is why the default grants allow them without a prompt.

``delete_task`` deliberately does not exist. Deletion is irreversible and
Phase 1 ships nothing irreversible; cancelling is the supported operation.
"""

from __future__ import annotations

from datetime import datetime, timezone

from jarvis.db.models import Capability, RiskLevel, TaskPriority, TaskStatus
from jarvis.errors import JarvisError
from jarvis.tasks.service import TaskFilter, TaskService
from jarvis.tools.base import ToolContext, ToolResult, tool

_PRIORITIES = [p.value for p in TaskPriority]
_STATUSES = [s.value for s in TaskStatus]


def _parse_due(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(
            f"Could not read '{raw}' as a date. Use ISO-8601, e.g. 2026-08-15T17:00:00Z."
        ) from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


@tool(
    name="create_task",
    description=(
        "Create a persistent task for the user. Use this when they ask you to "
        "remember to do something, track a piece of work, or add something to "
        "their list. Tasks survive across conversations."
    ),
    parameters={
        "type": "object",
        "properties": {
            "title": {
                "type": "string",
                "description": "Short imperative title, e.g. 'Renew car insurance'.",
                "minLength": 1,
                "maxLength": 500,
            },
            "description": {
                "type": "string",
                "description": "Optional detail, context, or acceptance criteria.",
            },
            "priority": {
                "type": "string",
                "enum": _PRIORITIES,
                "description": "Defaults to NORMAL.",
            },
            "due_at": {
                "type": "string",
                "description": "ISO-8601 due date, e.g. 2026-08-15T17:00:00Z.",
            },
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional labels for grouping.",
            },
        },
        "required": ["title"],
        "additionalProperties": False,
    },
    capability=Capability.WRITE,
    risk_level=RiskLevel.LOW,
    reversible=True,
    category="tasks",
    confirmation_template="Create a new task: {title}",
)
async def create_task(
    *,
    ctx: ToolContext,
    title: str,
    description: str | None = None,
    priority: str | None = None,
    due_at: str | None = None,
    tags: list[str] | None = None,
) -> ToolResult:
    service = TaskService(ctx.session)
    try:
        due = _parse_due(due_at)
    except ValueError as exc:
        return ToolResult.error(str(exc))

    try:
        task = await service.create(
            user_id=ctx.user_id,
            title=title,
            description=description,
            priority=TaskPriority(priority) if priority else TaskPriority.NORMAL,
            due_at=due,
            conversation_id=ctx.conversation_id,
            tags=tags,
            actor="jarvis",
        )
    except JarvisError as exc:
        return ToolResult.error(exc.message)

    return ToolResult.ok(
        f"Created task '{task.title}' ({task.id}) with priority {task.priority.value}.",
        task_id=task.id,
        title=task.title,
        status=task.status.value,
        priority=task.priority.value,
        due_at=task.due_at.isoformat() if task.due_at else None,
    )


@tool(
    name="list_tasks",
    description=(
        "List the user's tasks. Use this before answering anything about what "
        "they have to do, what is outstanding, or what is overdue. By default "
        "returns only tasks that are not completed or cancelled."
    ),
    parameters={
        "type": "object",
        "properties": {
            "status": {
                "type": "array",
                "items": {"type": "string", "enum": _STATUSES},
                "description": "Filter to these statuses.",
            },
            "include_completed": {
                "type": "boolean",
                "description": "Include completed and cancelled tasks. Default false.",
            },
            "search": {
                "type": "string",
                "description": "Substring match against title and description.",
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 100,
                "description": "Maximum tasks to return. Default 25.",
            },
        },
        "required": [],
        "additionalProperties": False,
    },
    capability=Capability.READ,
    risk_level=RiskLevel.NONE,
    category="tasks",
)
async def list_tasks(
    *,
    ctx: ToolContext,
    status: list[str] | None = None,
    include_completed: bool = False,
    search: str | None = None,
    limit: int = 25,
) -> ToolResult:
    service = TaskService(ctx.session)
    tasks = await service.list(
        ctx.user_id,
        TaskFilter(
            status=[TaskStatus(s) for s in status] if status else None,
            include_terminal=include_completed,
            search=search,
            limit=limit,
        ),
    )

    if not tasks:
        return ToolResult.ok("No matching tasks.", tasks=[], count=0)

    lines = []
    for t in tasks:
        due = f", due {t.due_at.date().isoformat()}" if t.due_at else ""
        lines.append(f"- [{t.status.value}] {t.title} ({t.priority.value}{due}) — {t.id}")

    return ToolResult.ok(
        f"{len(tasks)} task(s):\n" + "\n".join(lines),
        count=len(tasks),
        tasks=[TaskService.to_dict(t) for t in tasks],
    )


@tool(
    name="update_task",
    description=(
        "Update an existing task — change its status, priority, title, "
        "description, or due date. Use the task id from list_tasks. To finish a "
        "task set status to COMPLETED; to abandon one set CANCELLED."
    ),
    parameters={
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": "The task's id."},
            "status": {"type": "string", "enum": _STATUSES},
            "title": {"type": "string", "maxLength": 500},
            "description": {"type": "string"},
            "priority": {"type": "string", "enum": _PRIORITIES},
            "due_at": {"type": "string", "description": "ISO-8601 due date."},
            "note": {
                "type": "string",
                "description": "Why this change was made. Recorded in task history.",
            },
        },
        "required": ["task_id"],
        "additionalProperties": False,
    },
    capability=Capability.WRITE,
    risk_level=RiskLevel.LOW,
    reversible=True,
    category="tasks",
    confirmation_template="Update task {task_id}",
)
async def update_task(
    *,
    ctx: ToolContext,
    task_id: str,
    status: str | None = None,
    title: str | None = None,
    description: str | None = None,
    priority: str | None = None,
    due_at: str | None = None,
    note: str | None = None,
) -> ToolResult:
    service = TaskService(ctx.session)
    try:
        due = _parse_due(due_at)
    except ValueError as exc:
        return ToolResult.error(str(exc))

    changes: dict[str, object] = {}
    if status is not None:
        changes["status"] = TaskStatus(status)
    if title is not None:
        changes["title"] = title
    if description is not None:
        changes["description"] = description
    if priority is not None:
        changes["priority"] = TaskPriority(priority)
    if due is not None:
        changes["due_at"] = due

    if not changes:
        return ToolResult.error("Nothing to update — supply at least one field.")

    try:
        task = await service.update(task_id, actor="jarvis", note=note, **changes)
    except JarvisError as exc:
        # Returned rather than raised so the model can read the reason and
        # correct itself (e.g. an illegal status transition).
        return ToolResult.error(f"{exc.code}: {exc.message}")

    return ToolResult.ok(
        f"Updated '{task.title}' — now {task.status.value}, priority {task.priority.value}.",
        task_id=task.id,
        status=task.status.value,
        priority=task.priority.value,
    )


TOOLS = [create_task, list_tasks, update_task]
