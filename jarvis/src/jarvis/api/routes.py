"""HTTP routes.

Grouped into routers by resource. Everything except ``/api/health`` requires
the bearer token.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from jarvis.activity.service import ActivityService
from jarvis.api.deps import AuthDep, CoreDep, SessionDep, UserDep
from jarvis.confirmations.service import ConfirmationService
from jarvis.conversations.service import ConversationService
from jarvis.db.models import (
    ActivityKind,
    PermissionGrant,
    TaskPriority,
    TaskStatus,
    ToolDefinition,
)
from jarvis.errors import JarvisError, NotFoundError
from jarvis.logging import get_logger
from jarvis.tasks.service import TaskFilter, TaskService

log = get_logger(__name__)

Protected = [Depends(lambda auth: None)]  # placeholder replaced per-router below


# ── request models ───────────────────────────────────────────────────────────


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=100_000)
    conversation_id: str | None = None
    project_id: str | None = None
    provider: str | None = None
    model: str | None = None


class CreateTaskRequest(BaseModel):
    title: str = Field(min_length=1, max_length=500)
    description: str | None = None
    priority: TaskPriority = TaskPriority.NORMAL
    due_at: datetime | None = None
    project_id: str | None = None
    parent_task_id: str | None = None
    tags: list[str] = Field(default_factory=list)


class UpdateTaskRequest(BaseModel):
    title: str | None = None
    description: str | None = None
    status: TaskStatus | None = None
    priority: TaskPriority | None = None
    due_at: datetime | None = None
    note: str | None = None


class DecisionRequest(BaseModel):
    approved: bool
    note: str | None = None


class ToolPolicyRequest(BaseModel):
    enabled: bool | None = None
    mode_override: str | None = None


# ── health (unauthenticated) ─────────────────────────────────────────────────

health_router = APIRouter(tags=["health"])


@health_router.get("/health")
async def health(core: CoreDep) -> dict[str, Any]:
    """Liveness only — deliberately reveals nothing about configuration."""
    return {
        "status": "ok",
        "phase": 2,
        "providers_configured": len(core.providers.configured()),
        "time": datetime.now(timezone.utc).isoformat(),
    }


# ── chat ─────────────────────────────────────────────────────────────────────

chat_router = APIRouter(prefix="/chat", tags=["chat"])


@chat_router.post("")
async def chat(
    body: ChatRequest,
    core: CoreDep,
    session: SessionDep,
    user: UserDep,
    _: AuthDep,
) -> dict[str, Any]:
    """The main entry point. Runs the full orchestration pipeline."""
    conversation = None
    if body.conversation_id:
        conversation = await ConversationService(session).get(body.conversation_id)
        if conversation.user_id != user.id:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Conversation not found")

    response = await core.orchestrator.handle(
        session=session,
        user=user,
        message=body.message,
        conversation=conversation,
        project_id=body.project_id,
        provider_override=body.provider,
        model_override=body.model,
    )
    return response.to_dict()


# ── conversations ────────────────────────────────────────────────────────────

conversations_router = APIRouter(prefix="/conversations", tags=["conversations"])


@conversations_router.get("")
async def list_conversations(
    session: SessionDep,
    user: UserDep,
    _: AuthDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    include_archived: bool = False,
) -> dict[str, Any]:
    service = ConversationService(session)
    rows = await service.list(user.id, limit=limit, include_archived=include_archived)
    return {"conversations": [ConversationService.to_dict(c) for c in rows]}


@conversations_router.get("/{conversation_id}")
async def get_conversation(
    conversation_id: str, session: SessionDep, user: UserDep, _: AuthDep
) -> dict[str, Any]:
    service = ConversationService(session)
    conversation = await service.get(conversation_id)
    if conversation.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Conversation not found")
    messages = await service.messages(conversation_id)
    return ConversationService.to_dict(conversation, messages=messages)


@conversations_router.post("/{conversation_id}/archive")
async def archive_conversation(
    conversation_id: str, session: SessionDep, user: UserDep, _: AuthDep
) -> dict[str, Any]:
    service = ConversationService(session)
    conversation = await service.get(conversation_id)
    if conversation.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Conversation not found")
    await service.archive(conversation_id)
    await session.commit()
    return {"ok": True}


# ── tasks ────────────────────────────────────────────────────────────────────

tasks_router = APIRouter(prefix="/tasks", tags=["tasks"])


@tasks_router.get("")
async def list_tasks(
    session: SessionDep,
    user: UserDep,
    _: AuthDep,
    status_filter: Annotated[list[TaskStatus] | None, Query(alias="status")] = None,
    include_completed: bool = True,
    search: str | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> dict[str, Any]:
    service = TaskService(session)
    tasks = await service.list(
        user.id,
        TaskFilter(
            status=status_filter,
            include_terminal=include_completed,
            search=search,
            limit=limit,
        ),
    )
    return {
        "tasks": [TaskService.to_dict(t) for t in tasks],
        "counts": await service.counts_by_status(user.id),
    }


@tasks_router.post("", status_code=status.HTTP_201_CREATED)
async def create_task(
    body: CreateTaskRequest, session: SessionDep, user: UserDep, _: AuthDep
) -> dict[str, Any]:
    service = TaskService(session)
    task = await service.create(
        user_id=user.id,
        title=body.title,
        description=body.description,
        priority=body.priority,
        due_at=body.due_at,
        project_id=body.project_id,
        parent_task_id=body.parent_task_id,
        tags=body.tags,
        actor="user",
    )
    await session.commit()
    return TaskService.to_dict(task)


@tasks_router.get("/{task_id}")
async def get_task(
    task_id: str, session: SessionDep, user: UserDep, _: AuthDep
) -> dict[str, Any]:
    service = TaskService(session)
    task = await service.get(task_id)
    if task.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Task not found")
    return TaskService.to_dict(
        task,
        executions=await service.executions(task_id),
        history=await service.history(task_id),
    )


@tasks_router.patch("/{task_id}")
async def update_task(
    task_id: str,
    body: UpdateTaskRequest,
    session: SessionDep,
    user: UserDep,
    _: AuthDep,
) -> dict[str, Any]:
    service = TaskService(session)
    task = await service.get(task_id)
    if task.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Task not found")

    changes = body.model_dump(exclude_none=True, exclude={"note"})
    if not changes:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "No fields to update")

    updated = await service.update(task_id, actor="user", note=body.note, **changes)
    await session.commit()
    return TaskService.to_dict(updated)


# ── tools ────────────────────────────────────────────────────────────────────

tools_router = APIRouter(prefix="/tools", tags=["tools"])


@tools_router.get("")
async def list_tools(core: CoreDep, _: AuthDep) -> dict[str, Any]:
    return {"tools": core.tools.describe(), "categories": core.tools.categories()}


@tools_router.patch("/{tool_name}")
async def set_tool_policy(
    tool_name: str,
    body: ToolPolicyRequest,
    core: CoreDep,
    session: SessionDep,
    _: AuthDep,
) -> dict[str, Any]:
    """Operator policy for a tool: disable it, or force it to always ask."""
    from jarvis.db.models import PermissionMode

    if not core.tools.has(tool_name):
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Unknown tool '{tool_name}'")

    row = await session.get(ToolDefinition, tool_name)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Tool policy row missing")

    if body.enabled is not None:
        row.enabled = body.enabled
        core.tools.get(tool_name).enabled = body.enabled
    if body.mode_override is not None:
        row.mode_override = (
            None if body.mode_override == "" else PermissionMode(body.mode_override)
        )
    await session.commit()
    return {
        "name": row.name,
        "enabled": row.enabled,
        "mode_override": row.mode_override.value if row.mode_override else None,
    }


# ── permissions ──────────────────────────────────────────────────────────────

permissions_router = APIRouter(prefix="/permissions", tags=["permissions"])


@permissions_router.get("")
async def list_permissions(
    session: SessionDep, user: UserDep, _: AuthDep
) -> dict[str, Any]:
    from sqlalchemy import select

    rows = (
        await session.execute(
            select(PermissionGrant)
            .where(PermissionGrant.user_id == user.id)
            .order_by(PermissionGrant.capability, PermissionGrant.resource_scope)
        )
    ).scalars().all()

    return {
        "grants": [
            {
                "id": g.id,
                "capability": g.capability.value,
                "resource_scope": g.resource_scope,
                "mode": g.mode.value,
                "conditions": g.conditions,
                "note": g.note,
                "granted_at": g.granted_at.isoformat() if g.granted_at else None,
                "expires_at": g.expires_at.isoformat() if g.expires_at else None,
                "revoked_at": g.revoked_at.isoformat() if g.revoked_at else None,
            }
            for g in rows
        ],
        "defaults": {
            "READ": "ALLOW",
            "WRITE": "ASK",
            "EXECUTE": "ASK",
            "EXTERNAL_ACTION": "ASK",
            "SENSITIVE_ACTION": "DENY",
        },
    }


# ── confirmations ────────────────────────────────────────────────────────────

confirmations_router = APIRouter(prefix="/confirmations", tags=["confirmations"])


@confirmations_router.get("")
async def list_confirmations(
    core: CoreDep, session: SessionDep, user: UserDep, _: AuthDep
) -> dict[str, Any]:
    service = ConfirmationService(
        session, ttl_seconds=core.settings.confirmation_ttl_seconds
    )
    pending = await service.list_pending(user.id)
    await session.commit()  # persist any lazy expiry transitions
    return {"confirmations": [ConfirmationService.to_dict(c) for c in pending]}


@confirmations_router.post("/{confirmation_id}/decide")
async def decide_confirmation(
    confirmation_id: str,
    body: DecisionRequest,
    core: CoreDep,
    session: SessionDep,
    user: UserDep,
    _: AuthDep,
) -> dict[str, Any]:
    service = ConfirmationService(
        session, ttl_seconds=core.settings.confirmation_ttl_seconds
    )
    confirmation = await service.get(confirmation_id)
    if confirmation.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Confirmation not found")

    decided = await service.decide(
        confirmation_id, approved=body.approved, decided_by="user", note=body.note
    )
    await ActivityService(session, core.activity_bus).record(
        ActivityKind.CONFIRMATION_RESOLVED,
        summary=f"{'Approved' if body.approved else 'Denied'}: "
                f"{decided.action.get('tool')}",
        actor="user",
        detail={"confirmation_id": confirmation_id, "approved": body.approved},
        conversation_id=decided.conversation_id,
        status=decided.status.value,
    )
    await session.commit()
    return ConfirmationService.to_dict(decided)


# ── activity ─────────────────────────────────────────────────────────────────

activity_router = APIRouter(prefix="/activity", tags=["activity"])


@activity_router.get("")
async def list_activity(
    session: SessionDep,
    _: AuthDep,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    request_id: str | None = None,
    conversation_id: str | None = None,
    task_id: str | None = None,
) -> dict[str, Any]:
    service = ActivityService(session)
    rows = await service.recent(
        limit=limit,
        request_id=request_id,
        conversation_id=conversation_id,
        task_id=task_id,
    )
    return {"activity": [ActivityService.to_dict(r) for r in rows]}


@activity_router.get("/stream")
async def stream_activity(core: CoreDep, _: AuthDep) -> StreamingResponse:
    """Server-sent events for the live activity view.

    Heartbeats every 15s keep proxies from closing an idle connection, and let
    the client notice a dead server rather than believing nothing is happening.
    """

    async def event_source():
        queue = await core.activity_bus.subscribe()
        try:
            yield 'event: ready\ndata: {"ok":true}\n\n'
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    yield ": heartbeat\n\n"
                    continue
                yield f"event: activity\ndata: {json.dumps(event.to_dict())}\n\n"
        except asyncio.CancelledError:  # client disconnected
            raise
        finally:
            await core.activity_bus.unsubscribe(queue)

    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ── system ───────────────────────────────────────────────────────────────────

system_router = APIRouter(prefix="/system", tags=["system"])


@system_router.get("/status")
async def system_status(core: CoreDep, _: AuthDep) -> dict[str, Any]:
    return core.status()


@system_router.get("/prompt")
async def system_prompt(
    core: CoreDep,
    session: SessionDep,
    user: UserDep,
    _: AuthDep,
    q: str | None = None,
    project_id: str | None = None,
) -> dict[str, Any]:
    """Show exactly what JARVIS is told.

    Inspectable on purpose — when behaviour surprises you, the first question
    is what the prompt actually said.

    Pass ``q`` to see the prompt for a specific request, including the memory
    and knowledge blocks retrieval would produce for it. Without it the
    retrieval blocks are absent, which is accurate rather than incomplete:
    nothing is retrieved for a request that does not exist.
    """
    from jarvis.context.manager import ContextManager
    from jarvis.prompts.builder import SystemPromptBuilder

    bundle = await ContextManager(
        session,
        embeddings=core.embeddings,
        memory_enabled=core.settings.memory_enabled,
        knowledge_enabled=core.settings.knowledge_enabled,
    ).assemble(
        user_id=user.id,
        conversation_id=None,
        project_id=project_id,
        query=q,
    )
    prompt = SystemPromptBuilder().build(bundle)
    return {
        "blocks": prompt.describe(),
        "approx_tokens": prompt.approx_tokens,
        "rendered": prompt.render(),
        # Why those memories and not others — the same question the Memory UI
        # answers per-memory, answered here for the whole prompt.
        "retrieval": bundle.retrieval,
        "tainted": bundle.tainted,
    }


from jarvis.api.computer_routes import COMPUTER_ROUTERS  # noqa: E402
from jarvis.api.memory_routes import MEMORY_ROUTERS  # noqa: E402
from jarvis.api.obsidian_routes import OBSIDIAN_ROUTERS  # noqa: E402

ALL_ROUTERS = [
    health_router,
    chat_router,
    conversations_router,
    tasks_router,
    tools_router,
    permissions_router,
    confirmations_router,
    activity_router,
    system_router,
    *MEMORY_ROUTERS,
    *OBSIDIAN_ROUTERS,
    *COMPUTER_ROUTERS,
]
