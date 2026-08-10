"""Tool execution.

This is the single chokepoint through which every tool call passes. It is
deliberately the only place that invokes a handler, so the guarantees below
hold for all tools, present and future:

1. Arguments are validated against the tool's schema before anything runs.
2. The permission engine decides. There is no path that skips it.
3. ``ASK`` suspends rather than proceeds; ``DENY`` refuses.
4. Execution is bounded by a timeout.
5. Every attempt — allowed, denied, failed, or suspended — is persisted as a
   ``tool_executions`` row and an activity record.

Step 5 covers the denied and suspended cases on purpose. An audit trail that
only records what succeeded cannot answer "what did it try to do?", which is
the question that matters after something goes wrong.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema import ValidationError as SchemaValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from jarvis.activity.service import ActivityService
from jarvis.confirmations.service import ConfirmationRequest, ConfirmationService
from jarvis.db.base import utcnow
from jarvis.db.models import (
    ActivityKind,
    ExecutionStatus,
    PermissionMode,
    ToolExecution,
)
from jarvis.errors import (
    ConfirmationRequiredError,
    JarvisError,
    PermissionDeniedError,
    ToolExecutionError,
    ToolInputError,
    ToolTimeoutError,
)
from jarvis.logging import get_logger, timed
from jarvis.permissions.engine import PermissionEngine, PermissionRequest
from jarvis.tools.base import Tool, ToolContext, ToolResult
from jarvis.tools.registry import ToolRegistry

log = get_logger(__name__)


@dataclass(slots=True)
class ToolCall:
    """A model's request to run a tool."""

    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(slots=True)
class ToolOutcome:
    call: ToolCall
    result: ToolResult
    execution_id: str
    decision: PermissionMode
    duration_ms: float
    confirmation_id: str | None = None


class ToolExecutor:
    def __init__(
        self,
        *,
        session: AsyncSession,
        registry: ToolRegistry,
        permissions: PermissionEngine,
        confirmations: ConfirmationService,
        activity: ActivityService,
        timeout_seconds: float = 30.0,
    ) -> None:
        self.session = session
        self.registry = registry
        self.permissions = permissions
        self.confirmations = confirmations
        self.activity = activity
        self.timeout_seconds = timeout_seconds

    # ── public API ───────────────────────────────────────────────────────────

    async def execute(self, call: ToolCall, ctx: ToolContext) -> ToolOutcome:
        """Run a tool. Raises on denial, suspension, or unrecoverable failure.

        Callers that want a tool failure fed back to the model instead of
        raising should use :meth:`execute_safe`.
        """
        record = ToolExecution(
            tool_name=call.name,
            arguments=call.arguments,
            request_id=ctx.request_id,
            conversation_id=ctx.conversation_id,
            task_execution_id=ctx.task_execution_id,
            status=ExecutionStatus.RUNNING,
        )
        self.session.add(record)
        await self.session.flush()

        try:
            tool = self.registry.get(call.name)
            self._validate_arguments(tool, call.arguments)
            decision = await self._authorise(tool, call, ctx, record)
            outcome = await self._invoke(tool, call, ctx, record, decision)
            return outcome
        except JarvisError as exc:
            await self._finalise_error(record, exc, ctx)
            raise
        except Exception as exc:  # unexpected — normalise before it escapes
            wrapped = ToolExecutionError(f"Tool '{call.name}' crashed: {exc}")
            await self._finalise_error(record, wrapped, ctx)
            raise wrapped from exc

    async def execute_safe(self, call: ToolCall, ctx: ToolContext) -> ToolOutcome:
        """Like :meth:`execute`, but turns recoverable failures into a result.

        The agent loop uses this: a tool that failed with a bad argument or a
        transient error should come back to the model as a ``tool_result`` with
        ``is_error`` so it can correct itself, rather than aborting the turn.

        Confirmation suspension is *not* absorbed — that must reach the
        orchestrator, which is what pauses the turn and asks the user.
        """
        try:
            return await self.execute(call, ctx)
        except ConfirmationRequiredError:
            raise
        except PermissionDeniedError as exc:
            return ToolOutcome(
                call=call,
                result=ToolResult.error(
                    f"Permission denied: {exc.details.get('reason', 'not permitted')}. "
                    "Do not retry this tool; tell the user it is not permitted."
                ),
                execution_id="",
                decision=PermissionMode.DENY,
                duration_ms=0.0,
            )
        except JarvisError as exc:
            return ToolOutcome(
                call=call,
                result=ToolResult.error(f"{exc.code}: {exc.message}"),
                execution_id="",
                decision=PermissionMode.ALLOW,
                duration_ms=0.0,
            )

    # ── stages ───────────────────────────────────────────────────────────────

    @staticmethod
    def _validate_arguments(tool: Tool, arguments: dict[str, Any]) -> None:
        try:
            Draft202012Validator(tool.parameters).validate(arguments)
        except SchemaValidationError as exc:
            path = ".".join(str(p) for p in exc.absolute_path) or "(root)"
            raise ToolInputError(
                f"Invalid arguments for '{tool.name}' at {path}: {exc.message}",
                user_message="I called a tool with the wrong arguments.",
                details={"tool": tool.name, "path": path, "error": exc.message},
            ) from exc

    async def _authorise(
        self,
        tool: Tool,
        call: ToolCall,
        ctx: ToolContext,
        record: ToolExecution,
    ) -> PermissionMode:
        decision = await self.permissions.evaluate(
            PermissionRequest(
                user_id=ctx.user_id,
                capability=tool.capability,
                resource=tool.resource,
                risk_level=tool.risk_level,
                reversible=tool.reversible,
                tool_name=tool.name,
                tainted=ctx.tainted,
            )
        )
        record.permission_decision = decision.mode

        await self.activity.record(
            ActivityKind.PERMISSION_DECISION,
            summary=f"{decision.mode.value} {tool.name}",
            actor="permission_engine",
            detail=decision.describe(),
            request_id=ctx.request_id,
            conversation_id=ctx.conversation_id,
            tool_name=tool.name,
            status=decision.mode.value,
        )

        if decision.denied:
            record.status = ExecutionStatus.FAILED
            record.error_code = "permission_denied"
            record.error_message = decision.reason
            record.finished_at = utcnow()
            await self.session.flush()
            raise PermissionDeniedError(
                f"Tool '{tool.name}' denied: {decision.reason}",
                capability=tool.capability.value,
                tool=tool.name,
                reason=decision.reason,
            )

        # A tool can demand confirmation independently of the grant state.
        needs_ask = decision.needs_confirmation or tool.requires_confirmation
        if not needs_ask:
            return decision.mode

        approval = await self.confirmations.find_approval(
            ctx.user_id, tool.name, call.arguments
        )
        if approval is not None:
            await self.confirmations.consume(approval)
            record.confirmation_id = approval.id
            log.info(
                "confirmation_satisfied",
                tool=tool.name,
                confirmation_id=approval.id,
            )
            return PermissionMode.ALLOW

        confirmation = await self.confirmations.request(
            ConfirmationRequest(
                user_id=ctx.user_id,
                title=f"Allow {tool.name}?",
                body=tool.confirmation_text(call.arguments),
                tool_name=tool.name,
                arguments=call.arguments,
                risk_level=tool.risk_level,
                reversible=tool.reversible,
                request_id=ctx.request_id,
                conversation_id=ctx.conversation_id,
                reason=decision.reason,
            )
        )
        record.status = ExecutionStatus.AWAITING_CONFIRMATION
        record.confirmation_id = confirmation.id
        await self.session.flush()

        await self.activity.record(
            ActivityKind.CONFIRMATION_REQUESTED,
            summary=f"Waiting for approval: {tool.name}",
            actor="tool_executor",
            detail={"confirmation_id": confirmation.id, "tool": tool.name},
            request_id=ctx.request_id,
            conversation_id=ctx.conversation_id,
            tool_name=tool.name,
            status="AWAITING_CONFIRMATION",
        )
        raise ConfirmationRequiredError(
            f"Tool '{tool.name}' requires confirmation",
            confirmation_id=confirmation.id,
            user_message=f"I need your approval before I can {tool.name}.",
        )

    async def _invoke(
        self,
        tool: Tool,
        call: ToolCall,
        ctx: ToolContext,
        record: ToolExecution,
        decision: PermissionMode,
    ) -> ToolOutcome:
        with timed() as clock:
            try:
                result = await asyncio.wait_for(
                    tool.handler(ctx=ctx, **call.arguments),
                    timeout=self.timeout_seconds,
                )
            except asyncio.TimeoutError as exc:
                raise ToolTimeoutError(
                    f"Tool '{tool.name}' exceeded {self.timeout_seconds}s",
                    details={"tool": tool.name, "timeout": self.timeout_seconds},
                ) from exc
            except TypeError as exc:
                # Schema validation passed but the handler signature disagrees
                # — a programming error, surfaced as an input error so the loop
                # can recover rather than crash the turn.
                raise ToolInputError(
                    f"Tool '{tool.name}' rejected its arguments: {exc}",
                    details={"tool": tool.name},
                ) from exc

        if not isinstance(result, ToolResult):
            raise ToolExecutionError(
                f"Tool '{tool.name}' returned {type(result).__name__}, expected ToolResult"
            )

        record.status = (
            ExecutionStatus.FAILED if result.is_error else ExecutionStatus.SUCCEEDED
        )
        record.result = {"content": result.content, "data": result.data}
        record.duration_ms = clock.duration_ms
        record.undo_token = result.undo_token
        record.finished_at = utcnow()
        await self.session.flush()

        await self.activity.record(
            ActivityKind.TOOL_CALL,
            summary=f"{tool.name} → {'error' if result.is_error else 'ok'}",
            actor=f"tool:{tool.name}",
            detail={"arguments": call.arguments, "data": result.data},
            request_id=ctx.request_id,
            conversation_id=ctx.conversation_id,
            execution_id=record.id,
            tool_name=tool.name,
            status=record.status.value,
            duration_ms=clock.duration_ms,
        )

        log.info(
            "tool_executed",
            tool=tool.name,
            status=record.status.value,
            duration_ms=round(clock.duration_ms, 2),
        )
        return ToolOutcome(
            call=call,
            result=result,
            execution_id=record.id,
            decision=decision,
            duration_ms=clock.duration_ms,
            confirmation_id=record.confirmation_id,
        )

    async def _finalise_error(
        self, record: ToolExecution, exc: JarvisError, ctx: ToolContext
    ) -> None:
        # A suspended call is not a failure; leave its status intact so the
        # resume path can find it.
        if record.status is not ExecutionStatus.AWAITING_CONFIRMATION:
            record.status = ExecutionStatus.FAILED
            record.error_code = exc.code
            record.error_message = exc.message[:2000]
            record.finished_at = utcnow()
        try:
            await self.session.flush()
        except Exception:  # pragma: no cover - session already broken
            return

        if not isinstance(exc, ConfirmationRequiredError):
            await self.activity.record(
                ActivityKind.ERROR,
                summary=f"{record.tool_name} failed: {exc.code}",
                actor="tool_executor",
                detail={"error": exc.user_message},
                request_id=ctx.request_id,
                conversation_id=ctx.conversation_id,
                tool_name=record.tool_name,
                status="FAILED",
                error_code=exc.code,
            )
