"""HTTP routes.

Grouped into routers by resource. Everything except ``/api/health`` requires
the bearer token.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Annotated, Any, Literal

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
    #: How the decision arrived. Defaults to the deliberate path; a voice
    #: front-end says so, and the service refuses a voice approval of anything
    #: destructive. Recorded either way, because "through what?" is a forensic
    #: question whose answer must not depend on correlating timestamps.
    channel: Literal["ui", "api", "voice"] = "ui"


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
        confirmation_id, approved=body.approved, decided_by="user",
        note=body.note, channel=body.channel,
    )
    await ActivityService(session, core.activity_bus).record(
        ActivityKind.CONFIRMATION_RESOLVED,
        summary=f"{'Approved' if body.approved else 'Denied'}: "
                f"{decided.action.get('tool')}",
        actor="user",
        detail={"confirmation_id": confirmation_id, "approved": body.approved,
                "channel": body.channel, "impact": decided.impact},
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


@activity_router.get("/console")
async def stream_console(core: CoreDep, _: AuthDep) -> StreamingResponse:
    """The command centre's event stream.

    The same bus and the same transport as ``/activity/stream``, filtered and
    reshaped into the closed vocabulary in :mod:`jarvis.events.schema`. A
    second endpoint rather than a second bus: one place produces the truth, and
    two views of it cannot disagree about what happened.

    Records the console has no view for are dropped here rather than sent and
    ignored, so a tab is not woken for every model call.
    """
    from jarvis.events.schema import envelope

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
                payload = envelope(event)
                if payload is None:
                    continue
                yield f"event: console\ndata: {json.dumps(payload)}\n\n"
        except asyncio.CancelledError:
            raise
        finally:
            await core.activity_bus.unsubscribe(queue)

    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive",
                 "X-Accel-Buffering": "no"},
    )


# ── agents and background work ───────────────────────────────────────────────

agents_router = APIRouter(prefix="/agents", tags=["agents"])


@agents_router.get("")
async def list_agents(core: CoreDep, _: AuthDep) -> dict[str, Any]:
    """Running delegated agents and background jobs.

    Read-only by construction. There is deliberately no endpoint here that
    *creates* an agent: spawning hands authority to a second actor, so it goes
    through ``spawn_agent`` and therefore through ToolExecutor, the permission
    engine and the confirmation flow. A console button that spawned one
    directly would be the bypass this architecture exists to prevent.
    """
    runner = getattr(core, "background", None)
    jobs = [job.describe() for job in runner.list()] if runner else []
    return {
        "jobs": jobs,
        "running_jobs": sum(1 for j in jobs if j["state"] == "RUNNING"),
        # The root agent is the user's own loop; it has no ceiling because the
        # grants are the whole story for it.
        "root": {"role": "jarvis", "depth": 0, "capabilities": None},
    }


@agents_router.post("/jobs/{job_id}/{action}")
async def control_job(
    job_id: str, action: str, core: CoreDep, _: AuthDep
) -> dict[str, Any]:
    """Pause, resume or cancel a background job.

    These three are not tool calls and deliberately do not go through
    ToolExecutor: they *withdraw* authority rather than exercise it. Pausing
    something cannot do anything the running job was not already permitted to
    do, and requiring an approval to stop work would be an approval fatigue
    trap at exactly the wrong moment.

    There is no ``start`` here for the same reason in reverse.
    """
    runner = getattr(core, "background", None)
    if runner is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            "Background execution is not available")
    if action not in {"pause", "resume", "cancel"}:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Unknown action")

    done = getattr(runner, action)(job_id)
    job = runner.get(job_id)
    return {"ok": bool(done), "job": job.describe() if job else None}


# ── security ─────────────────────────────────────────────────────────────────

security_router = APIRouter(prefix="/security", tags=["security"])


@security_router.get("")
async def security_state(core: CoreDep, session: SessionDep, user: UserDep,
                         _: AuthDep) -> dict[str, Any]:
    """One place answering "is anything wrong, and what is currently allowed?".

    Assembled from existing sources rather than a new store: the stop latch,
    the computer policy, the grant table and the telemetry view. A security
    panel with its own state would be a second opinion, and the one that is
    wrong is always the one on screen.
    """
    from sqlalchemy import select

    from jarvis.telemetry.service import TelemetryService

    # Composed from ``core.status()`` rather than by reaching into the
    # subsystems. ``test_the_api_module_does_not_import_the_browser_service``
    # exists to stop a route driving the browser, and it is deliberately blunt
    # — blunt is what makes it hold. A security panel that read
    # the browser's settings object directly would be the first exception, and
    # the second would be a route that also opened a page.
    status_report = core.status()
    stop = getattr(core.computer, "emergency_stop", None)
    report = await TelemetryService(session).report(window_hours=24)

    grants = (
        await session.execute(
            select(PermissionGrant).where(
                PermissionGrant.user_id == user.id,
                PermissionGrant.revoked_at.is_(None),
            )
        )
    ).scalars().all()

    runner = getattr(core, "background", None)
    return {
        "emergency_stop": stop.state().to_dict() if stop else None,
        "browser": status_report.get("browser", {}),
        "computer": status_report.get("computer", {}),
        "grants": [
            {"capability": g.capability.value, "scope": g.resource_scope,
             "mode": g.mode.value, "conditions": g.conditions or {}}
            for g in grants
        ],
        "denied_last_24h": report.permission_decisions.get("DENY", 0),
        "running_jobs": len(runner.active) if runner else 0,
        "top_error_codes": report.top_error_codes,
    }


# ── system ───────────────────────────────────────────────────────────────────

system_router = APIRouter(prefix="/system", tags=["system"])


@system_router.get("/telemetry")
async def system_telemetry(
    session: SessionDep,
    _: AuthDep,
    window_hours: int = 168,
) -> dict[str, Any]:
    """What is failing most often, from local rows only.

    Nothing here is transmitted anywhere — see :mod:`jarvis.telemetry.service`
    for why that is a design decision rather than an omission.
    """
    from jarvis.telemetry.service import TelemetryService

    window = max(1, min(int(window_hours), 24 * 90))
    report = await TelemetryService(session).report(window_hours=window)
    return report.describe()


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
    agents_router,
    security_router,
    system_router,
    *MEMORY_ROUTERS,
    *OBSIDIAN_ROUTERS,
    *COMPUTER_ROUTERS,
]
