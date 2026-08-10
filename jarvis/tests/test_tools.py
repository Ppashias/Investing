"""Tool registration, discovery, and execution."""

from __future__ import annotations

import asyncio

import pytest

from jarvis.activity.service import ActivityBus, ActivityService
from jarvis.confirmations.service import ConfirmationService
from jarvis.db.models import (
    Capability,
    ExecutionStatus,
    PermissionMode,
    RiskLevel,
    ToolExecution,
)
from jarvis.errors import (
    ConfirmationRequiredError,
    PermissionDeniedError,
    ToolInputError,
    ToolNotFoundError,
    ToolTimeoutError,
)
from jarvis.permissions.engine import PermissionEngine
from jarvis.tools.base import Tool, ToolContext, ToolResult, tool
from jarvis.tools.executor import ToolCall, ToolExecutor
from jarvis.tools.registry import ToolRegistry, build_default_registry


# ── registry ─────────────────────────────────────────────────────────────────


def test_default_registry_has_expected_tools() -> None:
    names = {t.name for t in build_default_registry().all()}
    assert names == {
        "get_current_time", "system_status", "create_task", "list_tasks", "update_task"
    }


def test_all_phase1_tools_are_safe() -> None:
    """Phase 1 ships nothing irreversible and nothing above LOW risk."""
    for t in build_default_registry().all():
        assert t.reversible is True, f"{t.name} is irreversible"
        assert t.risk_level in (RiskLevel.NONE, RiskLevel.LOW), f"{t.name} too risky"
        assert t.capability in (Capability.READ, Capability.WRITE)


def test_duplicate_registration_rejected() -> None:
    registry = build_default_registry()
    with pytest.raises(ValueError):
        registry.register(registry.get("get_current_time"))


def test_unknown_tool_raises() -> None:
    with pytest.raises(ToolNotFoundError):
        build_default_registry().get("nonexistent")


def test_handler_must_be_async() -> None:
    with pytest.raises(TypeError):
        Tool(
            name="bad", description="d", parameters={"type": "object"},
            handler=lambda ctx: None,  # type: ignore[arg-type]
        ).validate_handler()


def test_handler_must_accept_ctx() -> None:
    async def no_ctx() -> ToolResult:  # pragma: no cover
        return ToolResult.ok("x")

    with pytest.raises(TypeError):
        Tool(name="bad", description="d", parameters={"type": "object"},
             handler=no_ctx).validate_handler()  # type: ignore[arg-type]


def test_provider_spec_excludes_policy() -> None:
    """The model must never see risk or capability — a prompt injection must
    not be able to argue with a tool's rating."""
    import dataclasses

    spec = build_default_registry().get("create_task").to_provider_spec()
    assert {f.name for f in dataclasses.fields(spec)} == {
        "name", "description", "input_schema"
    }


def test_disabled_tools_are_not_advertised() -> None:
    registry = build_default_registry()
    registry.get("create_task").enabled = False
    assert "create_task" not in {s.name for s in registry.provider_specs()}


async def test_sync_preserves_operator_columns(session) -> None:
    from jarvis.db.models import ToolDefinition

    registry = build_default_registry()
    await registry.sync_to_db(session)

    row = await session.get(ToolDefinition, "create_task")
    row.enabled = False
    row.mode_override = PermissionMode.ASK
    await session.flush()

    await registry.sync_to_db(session)  # a restart
    refreshed = await session.get(ToolDefinition, "create_task")
    assert refreshed.enabled is False
    assert refreshed.mode_override is PermissionMode.ASK


# ── executor ─────────────────────────────────────────────────────────────────


def _executor(session, registry: ToolRegistry | None = None,
              timeout: float = 5.0) -> ToolExecutor:
    return ToolExecutor(
        session=session,
        registry=registry or build_default_registry(),
        permissions=PermissionEngine(session),
        confirmations=ConfirmationService(session),
        activity=ActivityService(session, ActivityBus()),
        timeout_seconds=timeout,
    )


def _ctx(session, user) -> ToolContext:
    return ToolContext(user_id=user.id, session=session, request_id="req_test")


async def test_execute_read_tool(session, user) -> None:
    outcome = await _executor(session).execute(
        ToolCall(id="1", name="get_current_time", arguments={}), _ctx(session, user)
    )
    assert outcome.result.is_error is False
    assert outcome.decision is PermissionMode.ALLOW
    assert outcome.result.data["timezone"] == "UTC"


async def test_execute_write_tool_creates_row(session, user) -> None:
    outcome = await _executor(session).execute(
        ToolCall(id="1", name="create_task", arguments={"title": "From tool"}),
        _ctx(session, user),
    )
    assert outcome.result.data["task_id"].startswith("task_")


async def test_invalid_arguments_rejected_before_handler(session, user) -> None:
    with pytest.raises(ToolInputError):
        await _executor(session).execute(
            ToolCall(id="1", name="create_task", arguments={"wrong": True}),
            _ctx(session, user),
        )


async def test_additional_properties_rejected(session, user) -> None:
    with pytest.raises(ToolInputError):
        await _executor(session).execute(
            ToolCall(id="1", name="create_task",
                     arguments={"title": "ok", "sneaky": "x"}),
            _ctx(session, user),
        )


async def test_denied_tool_raises_and_is_recorded(session, user) -> None:
    from jarvis.db.models import ToolDefinition

    session.add(ToolDefinition(name="danger", capability=Capability.EXECUTE,
                               enabled=False))
    await session.flush()

    @tool(name="danger", description="d", capability=Capability.EXECUTE,
          parameters={"type": "object", "properties": {}, "additionalProperties": False})
    async def danger(*, ctx: ToolContext) -> ToolResult:  # pragma: no cover
        return ToolResult.ok("should not run")

    registry = build_default_registry()
    registry.register(danger)

    with pytest.raises(PermissionDeniedError):
        await _executor(session, registry).execute(
            ToolCall(id="1", name="danger", arguments={}), _ctx(session, user)
        )

    from sqlalchemy import select

    row = (await session.execute(
        select(ToolExecution).where(ToolExecution.tool_name == "danger")
    )).scalars().first()
    assert row is not None, "denied attempts must still be audited"
    assert row.status is ExecutionStatus.FAILED
    assert row.error_code == "permission_denied"


async def test_confirmation_required_then_satisfied(session, user) -> None:
    """The full suspend/approve/resume cycle."""

    @tool(
        name="risky", description="Does something risky",
        capability=Capability.EXECUTE, risk_level=RiskLevel.HIGH,
        reversible=False, requires_confirmation=True,
        parameters={"type": "object", "properties": {"x": {"type": "string"}},
                    "required": ["x"], "additionalProperties": False},
    )
    async def risky(*, ctx: ToolContext, x: str) -> ToolResult:
        return ToolResult.ok(f"did {x}")

    registry = build_default_registry()
    registry.register(risky)
    executor = _executor(session, registry)
    call = ToolCall(id="1", name="risky", arguments={"x": "thing"})

    with pytest.raises(ConfirmationRequiredError) as exc_info:
        await executor.execute(call, _ctx(session, user))
    confirmation_id = exc_info.value.confirmation_id

    await ConfirmationService(session).decide(confirmation_id, approved=True)

    outcome = await executor.execute(call, _ctx(session, user))
    assert outcome.result.content == "did thing"

    # Single-use: the same approval must not authorise a second run.
    with pytest.raises(ConfirmationRequiredError):
        await executor.execute(call, _ctx(session, user))


async def test_execute_safe_absorbs_denial_but_not_confirmation(session, user) -> None:
    @tool(
        name="needs_ok", description="d", capability=Capability.EXECUTE,
        requires_confirmation=True,
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
    )
    async def needs_ok(*, ctx: ToolContext) -> ToolResult:  # pragma: no cover
        return ToolResult.ok("ran")

    registry = build_default_registry()
    registry.register(needs_ok)

    with pytest.raises(ConfirmationRequiredError):
        await _executor(session, registry).execute_safe(
            ToolCall(id="1", name="needs_ok", arguments={}), _ctx(session, user)
        )


async def test_execute_safe_returns_error_for_bad_input(session, user) -> None:
    outcome = await _executor(session).execute_safe(
        ToolCall(id="1", name="create_task", arguments={}), _ctx(session, user)
    )
    assert outcome.result.is_error is True


async def test_tool_timeout_is_enforced(session, user) -> None:
    @tool(name="slow", description="d",
          parameters={"type": "object", "properties": {}, "additionalProperties": False})
    async def slow(*, ctx: ToolContext) -> ToolResult:
        await asyncio.sleep(5)
        return ToolResult.ok("never")  # pragma: no cover

    registry = build_default_registry()
    registry.register(slow)

    with pytest.raises(ToolTimeoutError):
        await _executor(session, registry, timeout=0.05).execute(
            ToolCall(id="1", name="slow", arguments={}), _ctx(session, user)
        )


async def test_crashing_tool_is_normalised(session, user) -> None:
    @tool(name="boom", description="d",
          parameters={"type": "object", "properties": {}, "additionalProperties": False})
    async def boom(*, ctx: ToolContext) -> ToolResult:
        raise RuntimeError("internal detail that must not leak")

    registry = build_default_registry()
    registry.register(boom)

    outcome = await _executor(session, registry).execute_safe(
        ToolCall(id="1", name="boom", arguments={}), _ctx(session, user)
    )
    assert outcome.result.is_error is True


async def test_every_execution_is_recorded(session, user) -> None:
    from sqlalchemy import func, select

    executor = _executor(session)
    for i in range(3):
        await executor.execute(
            ToolCall(id=str(i), name="get_current_time", arguments={}),
            _ctx(session, user),
        )
    count = (await session.execute(select(func.count(ToolExecution.id)))).scalar_one()
    assert count == 3
