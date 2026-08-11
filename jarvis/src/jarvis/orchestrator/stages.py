"""The concrete pipeline stages.

One class per stage of the flow in the brief (§5). Each is small and testable
in isolation; the pipeline supplies timing, activity recording, and error
handling.

The permission check does not appear here as a separate stage even though the
brief lists it as one. It is enforced *inside* tool execution, in
:class:`jarvis.tools.executor.ToolExecutor`, because that is the only place
that can guarantee it: a stage that checked permissions up front would be
checking a plan, while the executor checks the actual call with its actual
arguments, and cannot be bypassed.
"""

from __future__ import annotations

import re
from typing import Any

from jarvis.activity.service import ActivityService
from jarvis.context.manager import ContextManager
from jarvis.conversations.service import ConversationService
from jarvis.db.models import ActivityKind, MessageRole
from jarvis.errors import (
    ConfirmationRequiredError,
    ProviderRefusalError,
    ValidationError,
)
from jarvis.logging import get_logger
from jarvis.orchestrator.pipeline import PipelineContext, Stage
from jarvis.prompts.builder import SystemPromptBuilder
from jarvis.providers.base import (
    ChatMessage,
    CompletionRequest,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from jarvis.providers.retry import RetryPolicy
from jarvis.providers.router import ModelRouter, TaskClass
from jarvis.tools.base import Tool, ToolContext
from jarvis.tools.executor import ToolCall, ToolExecutor
from jarvis.tools.registry import ToolRegistry

log = get_logger(__name__)

MAX_MESSAGE_CHARS = 100_000


class ValidateRequestStage(Stage):
    name = "validate_request"

    async def run(self, ctx: PipelineContext) -> None:
        text = (ctx.message or "").strip()
        if not text:
            raise ValidationError(
                "Empty message",
                user_message="You sent an empty message — what would you like?",
            )
        if len(text) > MAX_MESSAGE_CHARS:
            raise ValidationError(
                f"Message of {len(text)} chars exceeds {MAX_MESSAGE_CHARS}",
                user_message=(
                    "That message is too long for me to take in one piece. "
                    "Split it up or point me at a file once I can read files."
                ),
            )
        # Normalise line endings so stored content and hashes are stable.
        ctx.message = text.replace("\r\n", "\n")


class LoadContextStage(Stage):
    """Creates or loads the conversation, then assembles context for it."""

    name = "load_context"

    def __init__(self, context_manager_factory: Any) -> None:
        self._make_context_manager = context_manager_factory

    async def run(self, ctx: PipelineContext) -> None:
        conversations = ConversationService(ctx.session)
        if ctx.conversation is None:
            ctx.set_conversation(await conversations.create(user_id=ctx.user.id))
        await conversations.maybe_autotitle(ctx.conversation, ctx.message)

        manager: ContextManager = self._make_context_manager(ctx.session)
        ctx.context_bundle = await manager.assemble(
            user_id=ctx.user.id,
            conversation_id=ctx.conversation_id,
            project_id=ctx.project_id,
            query=ctx.message,
        )
        ctx.provider_messages = conversations.to_provider_messages(
            ctx.context_bundle.history
        )


class AnalyseIntentStage(Stage):
    """Classifies the request to pick a task class and tool exposure.

    Phase 1 uses heuristics rather than a classifier model call. That is a
    deliberate trade: a routing call adds latency and cost to every single
    turn, and the only decisions it currently informs are "which tier of model"
    and "offer tools or not" — both cheap to get slightly wrong and easy to
    correct. The seam is here for Phase 6 to replace with a real classifier.
    """

    name = "analyse_intent"

    _REASONING_HINTS = re.compile(
        r"\b(plan|design|architect|analyse|analyze|compare|evaluate|strategy|"
        r"why|debug|refactor|trade-?offs?|decide|review)\b",
        re.IGNORECASE,
    )
    _TRIVIAL = re.compile(
        r"^\s*(hi|hey|hello|yo|thanks|thank you|ok|okay|got it|cheers|bye)\b[\s!.?]*$",
        re.IGNORECASE,
    )

    async def run(self, ctx: PipelineContext) -> None:
        text = ctx.message

        if self._TRIVIAL.match(text):
            ctx.task_class = TaskClass.FAST
            ctx.needs_tools = False
        elif self._REASONING_HINTS.search(text) or len(text) > 1500:
            ctx.task_class = TaskClass.REASONING
            ctx.needs_tools = True
        else:
            ctx.task_class = TaskClass.CONVERSATION
            ctx.needs_tools = True

        log.debug(
            "intent_analysed",
            request_id=ctx.request_id,
            task_class=ctx.task_class.value,
            needs_tools=ctx.needs_tools,
        )


class PlanStage(Stage):
    """Selects provider/model and the tool set for this turn."""

    name = "plan"

    def __init__(
        self, router: ModelRouter, registry: ToolRegistry, computer: Any = None
    ) -> None:
        self.router = router
        self.registry = registry
        self.computer = computer

    async def run(self, ctx: PipelineContext) -> None:
        available = [
            tool
            for tool in (self.registry.enabled() if ctx.needs_tools else [])
            if self._runnable_here(tool)
        ]
        ctx.available_tools = [t.name for t in available]

        ctx.routing = self.router.select(
            ctx.task_class,
            needs_tools=bool(available),
            needs_streaming=ctx.stream,
            provider_override=ctx.provider_override,
            model_override=ctx.model_override,
        )

        ctx.system_prompt = SystemPromptBuilder().build(ctx.context_bundle)

        log.info(
            "plan_selected",
            request_id=ctx.request_id,
            provider=ctx.routing.provider.key,
            model=ctx.routing.model,
            tools=len(ctx.available_tools),
        )

    def _runnable_here(self, tool: Tool) -> bool:
        """Can this machine actually perform what the tool does?

        Only computer tools have a machine-dependent answer, and only because
        §3 probes the environment at startup instead of assuming it. Offering
        ``click`` on a machine with no display backend is not a harmless
        no-op. The capability check lives in ``ComputerPolicyEngine``, which
        runs inside the handler; the executor's own permission decision runs
        *before* the handler and returns ASK for an ungranted EXECUTE. So the
        user was asked to approve a click, approved it, and only then learned
        that clicking is impossible here. Approving something that was never
        going to happen teaches someone their approvals are ceremonial.

        Withholding the tool is the narrow fix. It changes what the model is
        offered and nothing else — the policy engine still denies these
        actions on capability grounds for every other caller, and
        ``computer_status`` is never withheld, so "why can't you click?" is
        always answerable.
        """
        if self.computer is None or tool.category != "computer":
            return True

        capabilities = getattr(self.computer, "capabilities", None)
        if capabilities is None:
            return True

        from jarvis.tools.builtin.computer_tools import TOOL_ACTIONS

        kind = TOOL_ACTIONS.get(tool.name)
        return kind is None or capabilities.supports(kind)


class ExecuteStage(Stage):
    """The agent loop: call the model, run any tools it asks for, repeat.

    Bounded by ``max_iterations`` so a model that keeps calling tools cannot
    loop forever. Hitting the bound is reported rather than hidden.
    """

    name = "execute"

    def __init__(
        self,
        *,
        registry: ToolRegistry,
        executor_factory: Any,
        activity: ActivityService,
        retry: RetryPolicy,
        max_iterations: int = 8,
        embeddings: Any = None,
        computer: Any = None,
    ) -> None:
        self.registry = registry
        self._make_executor = executor_factory
        self.activity = activity
        self.retry = retry
        self.max_iterations = max_iterations
        self.embeddings = embeddings
        self.computer = computer

    async def run(self, ctx: PipelineContext) -> None:
        assert ctx.routing is not None
        provider = ctx.routing.provider
        system_text = ctx.system_prompt.render() if ctx.system_prompt else None
        # Pass the list as-is. `x or None` would turn an intentionally empty
        # tool set into None, which the registry reads as "every enabled tool"
        # — the opposite of what the plan asked for.
        tool_specs = self.registry.provider_specs(ctx.available_tools)

        messages = [*ctx.provider_messages, ChatMessage.user_text(ctx.message)]
        executor: ToolExecutor = self._make_executor(ctx.session)

        for iteration in range(1, self.max_iterations + 1):
            ctx.iterations = iteration

            request = CompletionRequest(
                messages=messages,
                system=system_text,
                model=ctx.routing.model,
                tools=tool_specs,
                max_tokens=4096,
                request_id=ctx.request_id,
            )

            try:
                completion = await self.retry.run(
                    lambda: provider.complete(request),
                    description=f"{provider.key}.complete",
                )
            except ProviderRefusalError as exc:
                ctx.final_text = exc.user_message
                ctx.add_warning("provider_refused")
                return

            ctx.completion = completion
            ctx.usage = ctx.usage + completion.usage

            await self.activity.record(
                ActivityKind.MODEL_CALL,
                summary=f"{completion.model} → {completion.stop_reason}",
                actor="orchestrator",
                detail={
                    "iteration": iteration,
                    "input_tokens": completion.usage.input_tokens,
                    "output_tokens": completion.usage.output_tokens,
                    "cost_micros": completion.usage.cost_micros,
                },
                request_id=ctx.request_id,
                conversation_id=ctx.conversation_id,
                provider=completion.provider,
                model=completion.model,
                status=completion.stop_reason,
                duration_ms=completion.latency_ms,
            )

            tool_uses = completion.tool_uses()
            if not tool_uses:
                ctx.final_text = completion.text()
                return

            # Persist the assistant turn *before* running tools, so a
            # confirmation suspension leaves a coherent transcript.
            await self._persist_assistant_turn(ctx, completion)
            messages.append(completion.to_message())

            results = await self._run_tools(ctx, executor, tool_uses)
            await self._persist_tool_results(ctx, results)
            messages.append(ChatMessage(role="user", content=list(results)))

        # Loop bound reached.
        ctx.add_warning("max_iterations_reached")
        ctx.final_text = (
            ctx.completion.text()
            if ctx.completion and ctx.completion.text()
            else (
                "I used the maximum number of tool steps for one turn without "
                "finishing. Tell me to continue if you want me to keep going."
            )
        )

    async def _run_tools(
        self, ctx: PipelineContext, executor: ToolExecutor, tool_uses: list[ToolUseBlock]
    ) -> list[ToolResultBlock]:
        results: list[ToolResultBlock] = []
        for use in tool_uses:
            call = ToolCall(id=use.id, name=use.name, arguments=use.input)
            tool_ctx = ToolContext(
                user_id=ctx.user.id,
                session=ctx.session,
                request_id=ctx.request_id,
                conversation_id=ctx.conversation_id,
                # Two sources, OR-ed: untrusted content retrieved into the
                # context *before* the turn started, and untrusted content a
                # tool has returned *during* it. The permission engine
                # escalates every non-read capability on a tainted request,
                # which is the structural defence against a document telling
                # JARVIS what to do.
                #
                # Read fresh on every iteration of this loop rather than
                # computed once, because the second source only exists once a
                # tool has run. That was the defect: taint came from the
                # context bundle alone, so a note read in step one left step
                # two untainted and a page saying "now delete everything"
                # reached a write that had never met a human.
                tainted=self._tainted(ctx),
                extras={
                    "embeddings": self.embeddings,
                    "project_id": ctx.project_id,
                    "computer": self.computer,
                    # The *same* ActivityService the executor writes its
                    # TOOL_CALL and PERMISSION_DECISION rows through, bound to
                    # this request's session. A tool that keeps its own
                    # subject-specific audit — Obsidian does — needs a way to
                    # reach the existing recorder; without it the vault audit
                    # was blind to precisely the operations it exists to
                    # answer for, namely the ones JARVIS performed itself.
                    # Passing the service rather than the bus keeps one audit
                    # system and one session.
                    "activity": self.activity,
                },
            )
            try:
                outcome = await executor.execute_safe(call, tool_ctx)
            except ConfirmationRequiredError as exc:
                ctx.pending_confirmation = {
                    "confirmation_id": exc.confirmation_id,
                    "tool": use.name,
                    "arguments": use.input,
                }
                # Results gathered so far are persisted by the caller's
                # handler; the turn stops here awaiting the user.
                raise
            ctx.tool_outcomes.append(outcome)

            # Accumulate before the next iteration, and before the next tool
            # in *this* batch: a model can request several tools at once, and
            # the second one must see the first one's taint.
            #
            # ``or``, never assignment. A later clean result must not clear
            # what an earlier untrusted one established — the untrusted content
            # is already in the transcript the model is reasoning from, and
            # "the most recent tool was safe" says nothing about that.
            if outcome.result.tainted and not ctx.tool_taint:
                ctx.tool_taint = True
                log.info(
                    "turn_tainted_by_tool_result",
                    request_id=ctx.request_id,
                    tool=use.name,
                )
            results.append(
                ToolResultBlock(
                    tool_use_id=use.id,
                    content=outcome.result.content,
                    is_error=outcome.result.is_error,
                )
            )
        return results

    @staticmethod
    def _tainted(ctx: PipelineContext) -> bool:
        """Is anything untrusted in scope for the next tool call?"""
        return bool(
            ctx.tool_taint
            or (
                ctx.context_bundle is not None
                and getattr(ctx.context_bundle, "tainted", False)
            )
        )

    @staticmethod
    async def _persist_assistant_turn(ctx: PipelineContext, completion: Any) -> None:
        conversations = ConversationService(ctx.session)
        await conversations.add_message(
            conversation_id=ctx.conversation_id,  # type: ignore[arg-type]
            role=MessageRole.ASSISTANT,
            content=completion.text(),
            blocks=list(completion.content),
            provider=completion.provider,
            model=completion.model,
            usage=completion.usage,
            stop_reason=completion.stop_reason,
            latency_ms=completion.latency_ms,
            request_id=ctx.request_id,
        )

    @staticmethod
    async def _persist_tool_results(
        ctx: PipelineContext, results: list[ToolResultBlock]
    ) -> None:
        if not results:
            return
        conversations = ConversationService(ctx.session)
        await conversations.add_message(
            conversation_id=ctx.conversation_id,  # type: ignore[arg-type]
            role=MessageRole.TOOL,
            content="\n".join(r.content for r in results)[:8000],
            blocks=list(results),
            request_id=ctx.request_id,
        )


class ValidateResultStage(Stage):
    """Catches empty or unusable output before it reaches the user."""

    name = "validate_result"

    async def run(self, ctx: PipelineContext) -> None:
        if ctx.final_text and ctx.final_text.strip():
            return
        if ctx.completion and ctx.completion.stop_reason == "max_tokens":
            ctx.final_text = (
                "I ran out of output space mid-answer. Ask me to continue and "
                "I will pick up where I stopped."
            )
            ctx.add_warning("truncated_output")
            return
        ctx.final_text = (
            "I did not produce a usable answer. That is a fault on my side — "
            "try rephrasing, and it will show up in the activity log."
        )
        ctx.add_warning("empty_completion")


class PersistStage(Stage):
    """Writes the final assistant turn.

    Intermediate assistant turns (the ones that requested tools) were already
    persisted inside the execute stage, so this only writes the closing message
    — and only when the last completion was not itself already stored.
    """

    name = "persist"

    async def run(self, ctx: PipelineContext) -> None:
        if ctx.conversation_id is None:
            return
        conversations = ConversationService(ctx.session)
        already_persisted = bool(ctx.completion and ctx.completion.tool_uses())
        if already_persisted:
            return

        await conversations.add_message(
            conversation_id=ctx.conversation_id,
            role=MessageRole.ASSISTANT,
            content=ctx.final_text,
            blocks=[TextBlock(text=ctx.final_text)],
            provider=ctx.completion.provider if ctx.completion else None,
            model=ctx.completion.model if ctx.completion else None,
            usage=ctx.completion.usage if ctx.completion else None,
            stop_reason=ctx.completion.stop_reason if ctx.completion else None,
            latency_ms=ctx.completion.latency_ms if ctx.completion else None,
            request_id=ctx.request_id,
            meta={"warnings": ctx.warnings} if ctx.warnings else {},
        )


class EvaluateMemoryStage(Stage):
    """Decide what this exchange is worth remembering (§12, §33).

    Last stage, after the answer is persisted, and deliberately so: the user is
    waiting on the reply, not on the bookkeeping, and a model asked to answer
    *and* curate memory does both worse.

    Nothing here can fail the turn. The response has already been produced; a
    memory candidate lost to a provider hiccup is not worth turning a good
    answer into an error.
    """

    name = "evaluate_memory"

    def __init__(
        self,
        *,
        router: Any = None,
        embeddings: Any = None,
        capture_mode: str = "ask",
        min_importance: float = 0.45,
        duplicate_threshold: float = 0.87,
    ) -> None:
        self.router = router
        self.embeddings = embeddings
        self.capture_mode = capture_mode
        self.min_importance = min_importance
        self.duplicate_threshold = duplicate_threshold

    async def should_run(self, ctx: PipelineContext) -> bool:
        return (
            self.capture_mode != "off"
            and self.router is not None
            and bool(ctx.final_text)
            and ctx.pending_confirmation is None
        )

    async def run(self, ctx: PipelineContext) -> None:
        from jarvis.memory.evaluator import MemoryEvaluator

        try:
            result = await MemoryEvaluator(
                ctx.session,
                router=self.router,
                embeddings=self.embeddings,
                capture_mode=self.capture_mode,
                min_importance=self.min_importance,
                duplicate_threshold=self.duplicate_threshold,
            ).evaluate_exchange(
                user_id=ctx.user.id,
                user_message=ctx.message,
                assistant_message=ctx.final_text,
                conversation_id=ctx.conversation_id,
                project_id=ctx.project_id,
                request_id=ctx.request_id,
            )
        except Exception as exc:
            log.warning("memory_evaluation_failed", error=str(exc))
            return

        if result.stored or result.proposed:
            await self.activity_record(ctx, result)

    async def activity_record(self, ctx: PipelineContext, result: Any) -> None:
        """Surface capture in the activity feed.

        Memory forming quietly in the background is exactly the kind of thing a
        user should be able to see happening.
        """
        from jarvis.activity.service import ActivityService

        verb = "Proposed" if result.proposed else "Remembered"
        count = len(result.proposed or result.stored)
        await ActivityService(ctx.session).record(
            ActivityKind.MEMORY_CAPTURED,
            summary=f"{verb} {count} memory/memories",
            actor="evaluator",
            detail=result.to_dict(),
            request_id=ctx.request_id,
            conversation_id=ctx.conversation_id,
        )
