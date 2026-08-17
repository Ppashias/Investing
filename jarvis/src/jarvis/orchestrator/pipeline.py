"""Pipeline framework.

The request path is a sequence of named, independently testable stages rather
than one function. The brief asks for this (§5) and it matters more than it
looks: when autonomous agents arrive, they need to run the same stages in a
different order, skip some, and re-enter mid-way after a confirmation. That is
only possible if the stages are separable objects rather than sections of a
procedure.

Each stage receives and mutates a :class:`PipelineContext`. The pipeline itself
handles timing, activity recording, and error normalisation, so no stage has to.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from jarvis.activity.service import ActivityService
from jarvis.db.models import ActivityKind, Conversation, User
from jarvis.errors import ConfirmationRequiredError, JarvisError
from jarvis.logging import get_logger, timed
from jarvis.providers.base import ChatMessage, CompletionResult, Usage
from jarvis.providers.router import RoutingDecision, TaskClass

log = get_logger(__name__)


@dataclass(slots=True)
class PipelineContext:
    """State threaded through the stages of one request."""

    # ── input ────────────────────────────────────────────────────────────────
    request_id: str
    user: User
    message: str
    session: AsyncSession
    conversation: Conversation | None = None
    #: Held as a plain string, not read off :attr:`conversation`. A rollback
    #: expires every ORM object in the session, and touching an expired
    #: attribute afterwards triggers a lazy reload — which raises in async
    #: context. The error path needs this id precisely when that has happened.
    conversation_id: str | None = None
    project_id: str | None = None
    provider_override: str | None = None
    model_override: str | None = None
    stream: bool = False

    # ── derived ──────────────────────────────────────────────────────────────
    context_bundle: Any = None            # ContextBundle
    system_prompt: Any = None             # SystemPrompt
    task_class: TaskClass = TaskClass.CONVERSATION
    needs_tools: bool = True
    available_tools: list[str] = field(default_factory=list)
    routing: RoutingDecision | None = None
    provider_messages: list[ChatMessage] = field(default_factory=list)

    # ── output ───────────────────────────────────────────────────────────────
    final_text: str = ""
    completion: CompletionResult | None = None
    usage: Usage = field(default_factory=Usage)
    tool_outcomes: list[Any] = field(default_factory=list)   # ToolOutcome
    #: Taint acquired from tool *results* during this turn, as opposed to the
    #: taint :attr:`context_bundle` carries from retrieved memory and knowledge.
    #: Kept separate so neither can be mistaken for the other, and monotonic:
    #: once a tool has returned untrusted content, nothing later in the turn
    #: makes the turn trustworthy again. A clean read after a poisoned one does
    #: not unread the poisoned one.
    tool_taint: bool = False
    iterations: int = 0
    pending_confirmation: dict[str, Any] | None = None
    stage_timings: dict[str, float] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    @property
    def tainted(self) -> bool:
        """Is anything untrusted in scope for this turn?

        The union of the two sources, in one place. They are stored separately
        so neither can be mistaken for the other — ``tool_taint`` is what this
        turn *read*, the bundle's flag is what was *retrieved* into context —
        but every consumer wants the union, and two consumers computing it
        independently is how one of them ends up not computing it at all. That
        is exactly what happened: the memory stage never asked, so an exchange
        that had read a poisoned page was captured as an untainted permanent
        memory.
        """
        return bool(
            self.tool_taint
            or (
                self.context_bundle is not None
                and getattr(self.context_bundle, "tainted", False)
            )
        )

    def set_conversation(self, conversation: Conversation) -> None:
        self.conversation = conversation
        self.conversation_id = conversation.id

    def add_warning(self, message: str) -> None:
        self.warnings.append(message)
        log.warning("pipeline_warning", request_id=self.request_id, warning=message)


class Stage(abc.ABC):
    """One step of the request path."""

    name: str = "stage"

    @abc.abstractmethod
    async def run(self, ctx: PipelineContext) -> None: ...

    async def should_run(self, ctx: PipelineContext) -> bool:
        return True


class Pipeline:
    def __init__(self, stages: list[Stage], activity: ActivityService) -> None:
        self.stages = stages
        self.activity = activity

    async def run(self, ctx: PipelineContext) -> PipelineContext:
        for stage in self.stages:
            if not await stage.should_run(ctx):
                log.debug("stage_skipped", stage=stage.name, request_id=ctx.request_id)
                continue

            with timed() as clock:
                try:
                    await stage.run(ctx)
                except ConfirmationRequiredError:
                    # Not a failure — the turn is suspended awaiting a human.
                    # Stages after this one are skipped by design; the
                    # orchestrator persists what happened and returns.
                    ctx.stage_timings[stage.name] = clock.duration_ms
                    raise
                except JarvisError as exc:
                    ctx.stage_timings[stage.name] = clock.duration_ms
                    await self.activity.record(
                        ActivityKind.ERROR,
                        summary=f"{stage.name} failed: {exc.code}",
                        actor="orchestrator",
                        detail={"stage": stage.name, "error": exc.user_message},
                        request_id=ctx.request_id,
                        conversation_id=ctx.conversation_id,
                        status="FAILED",
                        error_code=exc.code,
                        duration_ms=clock.duration_ms,
                    )
                    raise
            ctx.stage_timings[stage.name] = clock.duration_ms
            log.debug(
                "stage_completed",
                stage=stage.name,
                request_id=ctx.request_id,
                duration_ms=round(clock.duration_ms, 2),
            )
        return ctx
