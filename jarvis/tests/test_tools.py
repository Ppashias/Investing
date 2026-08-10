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
        # Phase 1
        "get_current_time", "system_status", "create_task", "list_tasks", "update_task",
        # Phase 2
        "remember", "recall", "update_memory", "forget", "forget_project_memories",
        "search_knowledge",
        # Phase 3
        "observe_screen", "list_windows", "click", "scroll", "type_text",
        "press_key", "open_application", "read_file", "write_file",
        "list_directory", "run_command", "computer_status",
    }


def test_no_tool_declares_a_sensitive_capability() -> None:
    """Phase 3 adds EXECUTE tools; SENSITIVE_ACTION remains unreachable.

    Phase 1-2's invariant was "nothing above WRITE", which computer control
    legitimately breaks — clicking and running commands are EXECUTE by
    definition. What must NOT appear is SENSITIVE_ACTION: financial,
    communication and system-settings scopes are absent from Phase 3 by
    design (§43), and a tool declaring that capability would be the first
    crack in it.
    """
    for t in build_default_registry().all():
        assert t.capability is not Capability.SENSITIVE_ACTION, t.name
        assert t.capability is not Capability.EXTERNAL_ACTION, (
            f"{t.name} claims EXTERNAL_ACTION; network actions are Phase 4."
        )


def test_every_computer_tool_routes_through_the_executor() -> None:
    """§13: no tool may reach the machine except through the chokepoint.

    Checked structurally rather than by inspection — a future tool that calls
    a backend directly would bypass the policy engine, the emergency stop and
    the audit log all at once, and would look perfectly reasonable in review.
    """
    import inspect

    from jarvis.tools.builtin import computer_tools

    source = inspect.getsource(computer_tools)
    # The module may reach the service, and the service alone.
    assert "self.backend" not in source
    assert ".backend." not in source, (
        "a computer tool touches a backend directly instead of going through "
        "ComputerService.execute_action"
    )
    for name in ("X11Backend", "xtest", "subprocess"):
        assert name not in source, f"{name} must not be reachable from a tool"


def test_irreversible_tools_can_never_be_auto_allowed() -> None:
    """The floor that makes one irreversible tool acceptable.

    Phase 2 adds `forget_project_memories`, the first tool marked irreversible.
    It archives rather than erases, so the marking is a deliberate use of the
    permission engine's irreversibility floor: a bulk operation must always
    meet a human, whatever grants exist. The engine half is exercised by the
    test below; this half asserts no read-only tool carries the marking, which
    would make it meaningless.
    """
    irreversible = [t for t in build_default_registry().all() if not t.reversible]
    assert irreversible, "expected at least one irreversible tool to guard"

    for t in irreversible:
        assert t.capability is not Capability.READ, (
            f"{t.name} is marked irreversible but only reads"
        )


async def test_irreversible_tool_is_floored_to_ask(session, user) -> None:
    """The floor, exercised for real against a grant that would allow it."""
    from jarvis.db.models import PermissionGrant, PermissionMode
    from jarvis.permissions.engine import PermissionEngine, PermissionRequest

    tool = build_default_registry().get("forget_project_memories")
    session.add(
        PermissionGrant(
            user_id=user.id,
            capability=tool.capability,
            resource_scope=tool.resource,
            mode=PermissionMode.ALLOW,
        )
    )
    await session.flush()

    decision = await PermissionEngine(session).evaluate(
        PermissionRequest(
            user_id=user.id,
            capability=tool.capability,
            resource=tool.resource,
            risk_level=tool.risk_level,
            reversible=tool.reversible,
            tool_name=tool.name,
        )
    )
    assert decision.mode is PermissionMode.ASK
    assert "irreversible_floor" in decision.applied_rules


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
