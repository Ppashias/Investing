"""Computer-control endpoints (§36, §38).

The emergency stop route is the one worth reading carefully. §27 requires the
stop to work independently of the AI reasoning process, and that shapes the
route: it engages a process-global latch directly, touching neither the
orchestrator nor the database on the way. It cannot be blocked by a wedged
model call, a held transaction, or a pipeline stuck mid-turn — and there is no
tool the model can call to reach it.

Everything else follows Phase 1's conventions: bearer auth on every route,
ownership checked before any read or write, and structured errors.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Body, HTTPException, Query, Response, status
from pydantic import BaseModel, Field

from jarvis.api.deps import AuthDep, CoreDep, SessionDep, UserDep
from jarvis.computer.policy import (
    PHASE3_FORBIDDEN_SCOPES,
    ComputerPolicy,
    load_policy,
)
from jarvis.computer.types import (
    ActionKind,
    ComputerAction,
    ComputerMode,
    ComputerScope,
)
from jarvis.db.models import ActivityKind
from jarvis.errors import ConfirmationRequiredError, JarvisError
from jarvis.logging import get_logger

log = get_logger(__name__)

computer_router = APIRouter(prefix="/computer", tags=["computer"])


class ActionRequest(BaseModel):
    kind: ActionKind
    params: dict[str, Any] = Field(default_factory=dict)
    reason: str = Field(min_length=1, max_length=500)
    expectation: str | None = None


class PolicyRequest(BaseModel):
    mode: ComputerMode | None = None
    enabled_scopes: list[ComputerScope] | None = None
    auto_scopes: list[ComputerScope] | None = None


class TaskRequest(BaseModel):
    objective: str = Field(min_length=3, max_length=2000)
    description: str | None = None


# ── status and capabilities ──────────────────────────────────────────────────


@computer_router.get("/status")
async def computer_status(
    core: CoreDep, session: SessionDep, user: UserDep, _: AuthDep
) -> dict[str, Any]:
    """Everything §36's panel renders."""
    return await core.computer.status(session, user.id)


@computer_router.get("/capabilities")
async def computer_capabilities(core: CoreDep, _: AuthDep) -> dict[str, Any]:
    """What this machine can do, and why not, per action."""
    return core.computer.capabilities.to_dict()


# ── emergency stop (§27) ─────────────────────────────────────────────────────


@computer_router.post("/stop")
async def emergency_stop(
    core: CoreDep,
    session: SessionDep,
    user: UserDep,
    _: AuthDep,
    reason: Annotated[str, Body(embed=True)] = "User pressed stop",
) -> dict[str, Any]:
    """Engage the emergency stop.

    The latch is set **first**, before anything that could fail or block. The
    audit write happens afterwards and cannot delay it: if the database were
    wedged, a stop that waited for it would be a stop that does not work.
    """
    state = core.computer.emergency_stop.engage(reason=reason, by="user")

    try:
        from jarvis.activity.service import ActivityService

        await ActivityService(session, core.activity_bus).record(
            ActivityKind.EMERGENCY_STOP,
            summary=f"Emergency stop engaged: {reason}",
            actor="user",
            detail=state.to_dict(),
            status="ENGAGED",
        )
        await session.commit()
    except Exception as exc:  # pragma: no cover - the stop still stands
        log.warning("emergency_stop_audit_failed", error=str(exc))

    return state.to_dict()


@computer_router.post("/resume")
async def release_stop(
    core: CoreDep, session: SessionDep, user: UserDep, _: AuthDep
) -> dict[str, Any]:
    """Release the stop. Only the user can do this — no tool reaches it."""
    state = core.computer.emergency_stop.release(by="user")

    from jarvis.activity.service import ActivityService

    await ActivityService(session, core.activity_bus).record(
        ActivityKind.EMERGENCY_STOP,
        summary="Emergency stop released",
        actor="user",
        detail=state.to_dict(),
        status="RELEASED",
    )
    await session.commit()
    return state.to_dict()


# ── observation ──────────────────────────────────────────────────────────────


@computer_router.get("/observe")
async def observe(
    core: CoreDep,
    session: SessionDep,
    user: UserDep,
    _: AuthDep,
    include_image: bool = True,
    window_id: str | None = None,
) -> dict[str, Any]:
    """Observe on demand (§37).

    Deliberately pull-based. §35 says do not continuously record the screen,
    so there is no stream — the UI refreshes when the user asks it to.
    """
    action = ComputerAction(
        kind=ActionKind.OBSERVE_SCREEN,
        params={
            "include_image": include_image,
            "window_id": window_id,
            # Explicitly requested by a human, so never suppressed as
            # unchanged.
            "force_image": include_image,
        },
        reason="Observation requested from the Computer panel",
    )
    result = await core.computer.execute_action(
        session, user.id, action, actor="user"
    )
    await session.commit()

    if not result.ok:
        raise HTTPException(
            status.HTTP_409_CONFLICT
            if result.outcome.value == "DENIED"
            else status.HTTP_501_NOT_IMPLEMENTED,
            result.detail,
        )
    return result.data


@computer_router.get("/screenshot/{screenshot_id}")
async def screenshot(
    screenshot_id: str, core: CoreDep, _: AuthDep
) -> Response:
    """Fetch a held screenshot as PNG. Expires with the store's TTL."""
    item = core.computer.screenshots.get(screenshot_id)
    if item is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "That screenshot has expired or never existed. Screenshots are "
            "held in memory for a limited time and never written to disk "
            "unless retention is enabled.",
        )
    return Response(
        content=item.png,
        media_type="image/png",
        # Never cached: a screenshot may contain anything that was on screen.
        headers={"Cache-Control": "no-store, max-age=0"},
    )


# ── actions and tasks ────────────────────────────────────────────────────────


@computer_router.post("/action")
async def execute_action(
    body: ActionRequest,
    core: CoreDep,
    session: SessionDep,
    user: UserDep,
    _: AuthDep,
) -> dict[str, Any]:
    """Run one action, straight through the executor.

    ``actor="user"`` because a request arriving here came from the operator,
    not the model. The distinction is recorded in the audit log and is the
    difference between "JARVIS did this" and "I did this".
    """
    action = ComputerAction(
        kind=body.kind, params=body.params, reason=body.reason,
        expectation=body.expectation,
    )
    try:
        result = await core.computer.execute_action(
            session, user.id, action, actor="user"
        )
    except ConfirmationRequiredError as exc:
        await session.commit()
        return {
            "status": "needs_confirmation",
            "confirmation_id": exc.confirmation_id,
            "message": exc.user_message,
        }
    except JarvisError as exc:
        await session.commit()
        raise HTTPException(exc.http_status, exc.user_message)

    await session.commit()
    return result.to_dict()


@computer_router.post("/tasks", status_code=status.HTTP_201_CREATED)
async def run_task(
    body: TaskRequest,
    core: CoreDep,
    session: SessionDep,
    user: UserDep,
    _: AuthDep,
) -> dict[str, Any]:
    """Run a multi-step objective through the closed loop."""
    agent, _policy = await core.computer.agent(session, user.id)
    run = await agent.run(
        session=session,
        user_id=user.id,
        objective=body.objective,
        description=body.description,
    )
    await session.commit()
    return run.to_dict()


@computer_router.get("/tasks")
async def list_tasks(
    core: CoreDep,
    session: SessionDep,
    user: UserDep,
    _: AuthDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> dict[str, Any]:
    return {"tasks": await core.computer.tasks(session, user.id, limit=limit)}


@computer_router.post("/tasks/{task_id}/cancel")
async def cancel_task(
    task_id: str, core: CoreDep, session: SessionDep, user: UserDep, _: AuthDep
) -> dict[str, Any]:
    result = await core.computer.cancel_task(session, user.id, task_id)
    await session.commit()
    return result


# ── permissions (§16, §38) ───────────────────────────────────────────────────


@computer_router.get("/permissions")
async def get_permissions(
    session: SessionDep, user: UserDep, _: AuthDep
) -> dict[str, Any]:
    return (await load_policy(session, user.id)).to_dict()


@computer_router.patch("/permissions")
async def update_permissions(
    body: PolicyRequest,
    core: CoreDep,
    session: SessionDep,
    user: UserDep,
    _: AuthDep,
) -> dict[str, Any]:
    """Change mode and scopes.

    Forbidden scopes are rejected explicitly rather than filtered silently:
    a user who asks to enable FINANCIAL should be told it does not exist in
    this phase, not left believing it worked.
    """
    current = await load_policy(session, user.id)

    for field_name in ("enabled_scopes", "auto_scopes"):
        requested = getattr(body, field_name)
        if requested:
            forbidden = [s.value for s in requested if s in PHASE3_FORBIDDEN_SCOPES]
            if forbidden:
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST,
                    f"{', '.join(forbidden)} cannot be enabled: these scopes "
                    "are not implemented in this phase.",
                )

    enabled = (
        frozenset(body.enabled_scopes)
        if body.enabled_scopes is not None
        else current.enabled_scopes
    )
    auto = (
        frozenset(body.auto_scopes)
        if body.auto_scopes is not None
        else current.auto_scopes
    )
    # A scope cannot be automatic without being enabled — otherwise disabling
    # a scope would leave a live "run this without asking" flag behind it.
    auto = frozenset(s for s in auto if s in enabled)

    updated = await core.computer.update_policy(
        session,
        user.id,
        ComputerPolicy(
            mode=body.mode or current.mode,
            enabled_scopes=enabled,
            auto_scopes=auto,
        ),
    )
    await session.commit()
    log.warning(
        "computer_permissions_changed",
        mode=updated.mode.value,
        enabled=sorted(s.value for s in updated.enabled_scopes),
        auto=sorted(s.value for s in updated.auto_scopes),
    )
    return updated.to_dict()


# ── audit (§26) ──────────────────────────────────────────────────────────────


@computer_router.get("/audit")
async def audit_log(
    core: CoreDep,
    session: SessionDep,
    user: UserDep,
    _: AuthDep,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    task_id: str | None = None,
    outcome: str | None = None,
) -> dict[str, Any]:
    """What JARVIS did on this computer.

    Read-only by design. There is no route that edits or deletes an entry, and
    that absence is the mechanism §26 asks for.
    """
    return {
        "entries": await core.computer.audit(
            session, user.id, limit=limit, task_id=task_id, outcome=outcome
        ),
        "summary": await core.computer.audit_summary(session, user.id),
    }


COMPUTER_ROUTERS = [computer_router]
