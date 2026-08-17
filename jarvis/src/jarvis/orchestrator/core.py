"""The orchestrator.

Assembles the pipeline, runs it, and converts whatever happens — success,
failure, or suspension pending confirmation — into a single response shape the
API and UI can rely on.

The orchestrator owns transaction boundaries. Stages never commit; the
orchestrator commits once on success, and on failure rolls back the work while
still persisting the error turn so the conversation transcript stays coherent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from jarvis.activity.service import ActivityService
from jarvis.confirmations.service import ConfirmationService
from jarvis.context.manager import ContextManager
from jarvis.conversations.service import ConversationService
from jarvis.db.base import new_id
from jarvis.db.models import ActivityKind, Conversation, MessageRole, User
from jarvis.errors import ConfirmationRequiredError, JarvisError
from jarvis.logging import bind_context, get_logger, reset_context, timed
from jarvis.orchestrator.pipeline import Pipeline, PipelineContext
from jarvis.orchestrator.stages import (
    AnalyseIntentStage,
    EvaluateMemoryStage,
    ExecuteStage,
    LoadContextStage,
    PersistStage,
    PlanStage,
    ValidateRequestStage,
    ValidateResultStage,
)
from jarvis.permissions.engine import PermissionEngine
from jarvis.providers.base import TextBlock, Usage
from jarvis.providers.retry import RetryPolicy
from jarvis.providers.router import ModelRouter
from jarvis.tools.executor import ToolExecutor
from jarvis.tools.registry import ToolRegistry

log = get_logger(__name__)


@dataclass(slots=True)
class JarvisResponse:
    request_id: str
    conversation_id: str | None
    text: str
    status: str = "completed"          # completed | needs_confirmation | error
    provider: str | None = None
    model: str | None = None
    usage: Usage = field(default_factory=Usage)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    pending_confirmation: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    warnings: list[str] = field(default_factory=list)
    duration_ms: float = 0.0
    stage_timings: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "conversation_id": self.conversation_id,
            "text": self.text,
            "status": self.status,
            "provider": self.provider,
            "model": self.model,
            "usage": {
                "input_tokens": self.usage.input_tokens,
                "output_tokens": self.usage.output_tokens,
                "cost_micros": self.usage.cost_micros,
            },
            "tool_calls": self.tool_calls,
            "pending_confirmation": self.pending_confirmation,
            "error": self.error,
            "warnings": self.warnings,
            "duration_ms": round(self.duration_ms, 2),
            "stage_timings": {k: round(v, 2) for k, v in self.stage_timings.items()},
        }


class Orchestrator:
    def __init__(
        self,
        *,
        registry: ToolRegistry,
        router: ModelRouter,
        activity_bus: Any,
        retry: RetryPolicy,
        tool_timeout_seconds: float = 30.0,
        max_iterations: int = 8,
        confirmation_ttl_seconds: int = 900,
        embeddings: Any = None,
        context_budget: Any = None,
        memory_enabled: bool = True,
        knowledge_enabled: bool = True,
        memory_capture_mode: str = "ask",
        memory_min_importance: float = 0.45,
        memory_duplicate_threshold: float = 0.87,
        computer: Any = None,
        browser: Any = None,
        background: Any = None,
    ) -> None:
        self.registry = registry
        self.router = router
        self.activity_bus = activity_bus
        self.retry = retry
        self.tool_timeout_seconds = tool_timeout_seconds
        self.max_iterations = max_iterations
        self.confirmation_ttl_seconds = confirmation_ttl_seconds
        self.embeddings = embeddings
        self.context_budget = context_budget
        self.memory_enabled = memory_enabled
        self.knowledge_enabled = knowledge_enabled
        self.memory_capture_mode = memory_capture_mode
        self.memory_min_importance = memory_min_importance
        self.memory_duplicate_threshold = memory_duplicate_threshold
        self.computer = computer
        self.browser = browser
        self.background = background

    # ── wiring ───────────────────────────────────────────────────────────────

    def _activity(self, session: AsyncSession) -> ActivityService:
        return ActivityService(session, self.activity_bus)

    def _make_context_manager(self, session: AsyncSession) -> ContextManager:
        return ContextManager(
            session,
            self.context_budget,
            embeddings=self.embeddings,
            memory_enabled=self.memory_enabled,
            knowledge_enabled=self.knowledge_enabled,
        )

    def _make_executor(self, session: AsyncSession) -> ToolExecutor:
        return ToolExecutor(
            session=session,
            registry=self.registry,
            permissions=PermissionEngine(session),
            confirmations=ConfirmationService(
                session, ttl_seconds=self.confirmation_ttl_seconds
            ),
            activity=self._activity(session),
            timeout_seconds=self.tool_timeout_seconds,
            # The stop object lives on ComputerService because that is where it
            # was first needed, but what it governs is every tool. Reached
            # rather than moved so the computer API that engages and releases
            # it keeps working unchanged.
            emergency_stop=getattr(self.computer, "emergency_stop", None),
        )

    def _make_supervisor(self, session: AsyncSession) -> Any:
        """A supervisor per request, like the executor.

        Per request rather than process-wide because it hands out identities
        derived from *this* turn's actor, and one that outlived the turn could
        hand a later one a ceiling computed from an earlier caller. The jobs it
        starts outlive the request; the authority to start them does not.
        """
        from jarvis.agents.supervisor import AgentSupervisor

        return AgentSupervisor(
            executor_factory=self._make_executor,
            registry=self.registry,
            router=self.router,
            activity=self._activity(session),
            emergency_stop=getattr(self.computer, "emergency_stop", None),
        )

    def _build_pipeline(self, activity: ActivityService) -> Pipeline:
        return Pipeline(
            stages=[
                ValidateRequestStage(),
                LoadContextStage(self._make_context_manager),
                AnalyseIntentStage(),
                PlanStage(self.router, self.registry, self.computer, self.browser),
                ExecuteStage(
                    registry=self.registry,
                    executor_factory=self._make_executor,
                    activity=activity,
                    retry=self.retry,
                    max_iterations=self.max_iterations,
                    embeddings=self.embeddings,
                    computer=self.computer,
                    browser=self.browser,
                    background=self.background,
                    supervisor_factory=self._make_supervisor,
                ),
                ValidateResultStage(),
                PersistStage(),
                # After persistence on purpose: the user is waiting on the
                # answer, not on the bookkeeping (§33).
                EvaluateMemoryStage(
                    router=self.router,
                    embeddings=self.embeddings,
                    capture_mode=self.memory_capture_mode,
                    min_importance=self.memory_min_importance,
                    duplicate_threshold=self.memory_duplicate_threshold,
                ),
            ],
            activity=activity,
        )

    # ── entry point ──────────────────────────────────────────────────────────

    async def handle(
        self,
        *,
        session: AsyncSession,
        user: User,
        message: str,
        conversation: Conversation | None = None,
        project_id: str | None = None,
        provider_override: str | None = None,
        model_override: str | None = None,
    ) -> JarvisResponse:
        request_id = new_id("req")
        tokens = bind_context(
            request_id=request_id,
            conversation_id=conversation.id if conversation else None,
        )
        activity = self._activity(session)

        ctx = PipelineContext(
            request_id=request_id,
            user=user,
            message=message,
            session=session,
            conversation=conversation,
            conversation_id=conversation.id if conversation else None,
            project_id=project_id,
            provider_override=provider_override,
            model_override=model_override,
        )

        await activity.record(
            ActivityKind.REQUEST_STARTED,
            summary=_preview(message),
            actor="user",
            request_id=request_id,
            conversation_id=ctx.conversation_id,
            status="RUNNING",
        )

        try:
            with timed() as clock:
                try:
                    # Committed before the pipeline runs. "The user said X" is
                    # a fact regardless of whether JARVIS can answer, and the
                    # failure path below rolls back the pipeline's transaction
                    # — which would otherwise take the conversation and the
                    # user's own message with it.
                    await self._record_user_message(ctx)
                    await session.commit()

                    await self._build_pipeline(activity).run(ctx)
                    response = self._success(ctx, clock.duration_ms)
                    await activity.record(
                        ActivityKind.REQUEST_COMPLETED,
                        summary=_preview(ctx.final_text),
                        actor="jarvis",
                        request_id=request_id,
                        conversation_id=ctx.conversation_id,
                        provider=response.provider,
                        model=response.model,
                        status="COMPLETED",
                        duration_ms=clock.duration_ms,
                        detail={
                            "iterations": ctx.iterations,
                            "tools_used": [o.call.name for o in ctx.tool_outcomes],
                        },
                    )
                    await session.commit()
                    return response

                except ConfirmationRequiredError as exc:
                    response = await self._suspended(ctx, exc, clock.elapsed_ms)
                    await activity.record(
                        ActivityKind.REQUEST_COMPLETED,
                        summary="Awaiting your approval",
                        actor="jarvis",
                        request_id=request_id,
                        conversation_id=ctx.conversation_id,
                        status="AWAITING_CONFIRMATION",
                        duration_ms=clock.elapsed_ms,
                    )
                    # Commit: the confirmation record and the partial transcript
                    # must survive, or approving it later would resume nothing.
                    await session.commit()
                    return response

                except JarvisError as exc:
                    await session.rollback()
                    return await self._failed(ctx, exc, clock.elapsed_ms)

                except Exception as exc:  # unexpected — never leak internals
                    await session.rollback()
                    log.exception(
                        "orchestrator_unhandled", request_id=request_id, error=str(exc)
                    )
                    wrapped = JarvisError(
                        f"Unhandled orchestrator error: {exc}",
                        user_message=(
                            "Something went wrong inside me handling that. "
                            "The details are in the activity log."
                        ),
                    )
                    return await self._failed(ctx, wrapped, clock.elapsed_ms)
        finally:
            reset_context(tokens)

    # ── outcome builders ─────────────────────────────────────────────────────

    @staticmethod
    async def _record_user_message(ctx: PipelineContext) -> None:
        """Store the user's turn once the conversation exists.

        Deferred until after validation so an invalid request does not leave a
        turn behind, but before execution so the transcript is correct even if
        the model call fails.
        """
        conversations = ConversationService(ctx.session)
        if ctx.conversation is None:
            ctx.set_conversation(await conversations.create(user_id=ctx.user.id))
        await conversations.add_message(
            conversation_id=ctx.conversation_id,
            role=MessageRole.USER,
            content=ctx.message,
            blocks=[TextBlock(text=ctx.message)],
            request_id=ctx.request_id,
        )

    def _success(self, ctx: PipelineContext, duration_ms: float) -> JarvisResponse:
        return JarvisResponse(
            request_id=ctx.request_id,
            conversation_id=ctx.conversation_id,
            text=ctx.final_text,
            status="completed",
            provider=ctx.completion.provider if ctx.completion else None,
            model=ctx.completion.model if ctx.completion else None,
            usage=ctx.usage,
            tool_calls=[
                {
                    "tool": o.call.name,
                    "arguments": o.call.arguments,
                    "is_error": o.result.is_error,
                    "content": o.result.content[:2000],
                    "data": o.result.data,
                    "duration_ms": round(o.duration_ms, 2),
                }
                for o in ctx.tool_outcomes
            ],
            warnings=ctx.warnings,
            duration_ms=duration_ms,
            stage_timings=ctx.stage_timings,
        )

    async def _suspended(
        self, ctx: PipelineContext, exc: ConfirmationRequiredError, duration_ms: float
    ) -> JarvisResponse:
        confirmations = ConfirmationService(
            ctx.session, ttl_seconds=self.confirmation_ttl_seconds
        )
        record = await confirmations.get(exc.confirmation_id)
        payload = ConfirmationService.to_dict(record)

        text = exc.user_message
        if ctx.conversation_id is not None:
            await ConversationService(ctx.session).add_message(
                conversation_id=ctx.conversation_id,
                role=MessageRole.ASSISTANT,
                content=text,
                blocks=[TextBlock(text=text)],
                request_id=ctx.request_id,
                meta={"pending_confirmation": exc.confirmation_id},
            )

        return JarvisResponse(
            request_id=ctx.request_id,
            conversation_id=ctx.conversation_id,
            text=text,
            status="needs_confirmation",
            provider=ctx.completion.provider if ctx.completion else None,
            model=ctx.completion.model if ctx.completion else None,
            usage=ctx.usage,
            pending_confirmation=payload,
            warnings=ctx.warnings,
            duration_ms=duration_ms,
            stage_timings=ctx.stage_timings,
        )

    async def _failed(
        self, ctx: PipelineContext, exc: JarvisError, duration_ms: float
    ) -> JarvisResponse:
        """Record the failure on a fresh transaction.

        The pipeline's transaction was rolled back, so the error turn and its
        activity record are written separately — otherwise a failed request
        would vanish from the transcript entirely, which is the opposite of
        what an activity log is for.
        """
        try:
            activity = self._activity(ctx.session)
            await activity.record(
                ActivityKind.REQUEST_FAILED,
                summary=f"Request failed: {exc.code}",
                actor="jarvis",
                detail={"error": exc.user_message, "stage_timings": ctx.stage_timings},
                request_id=ctx.request_id,
                conversation_id=ctx.conversation_id,
                status="FAILED",
                error_code=exc.code,
                duration_ms=duration_ms,
            )
            if ctx.conversation_id is not None:
                await ConversationService(ctx.session).add_message(
                    conversation_id=ctx.conversation_id,
                    role=MessageRole.ASSISTANT,
                    content=exc.user_message,
                    blocks=[TextBlock(text=exc.user_message)],
                    request_id=ctx.request_id,
                    meta={"error_code": exc.code},
                )
            await ctx.session.commit()
        except Exception:  # pragma: no cover - the session is beyond saving
            await ctx.session.rollback()
            log.exception("failed_to_record_failure", request_id=ctx.request_id)

        log.warning(
            "request_failed",
            request_id=ctx.request_id,
            error_code=exc.code,
            duration_ms=round(duration_ms, 2),
        )
        return JarvisResponse(
            request_id=ctx.request_id,
            conversation_id=ctx.conversation_id,
            text=exc.user_message,
            status="error",
            error=exc.to_dict(),
            usage=ctx.usage,
            warnings=ctx.warnings,
            duration_ms=duration_ms,
            stage_timings=ctx.stage_timings,
        )


def _preview(text: str, limit: int = 160) -> str:
    cleaned = " ".join((text or "").split())
    return cleaned[:limit] + ("…" if len(cleaned) > limit else "")
