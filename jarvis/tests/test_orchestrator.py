"""Full request pipeline, driven by the stub provider.

These are the integration tests: they exercise the real orchestrator, real
permission engine, real tool executor, and real persistence, with only the
model call substituted.
"""

from __future__ import annotations

import pytest

from jarvis.confirmations.service import ConfirmationService
from jarvis.core import JarvisCore
from jarvis.db.models import Capability, MessageRole, RiskLevel
from jarvis.errors import ProviderRateLimitError, ProviderRefusalError
from jarvis.tools.base import ToolContext, ToolResult, tool
from tests.conftest import StubProvider, text_result, tool_result


async def _run(core: JarvisCore, message: str, conversation=None):
    async with core.database.session_factory() as session:
        user = await JarvisCore.ensure_default_user(session)
        await session.commit()
        return await core.orchestrator.handle(
            session=session, user=user, message=message, conversation=conversation
        )


# ── happy path ───────────────────────────────────────────────────────────────


async def test_plain_conversation_round_trip(core: JarvisCore, stub: StubProvider) -> None:
    stub.responses = [text_result("Hello. I am operational.")]
    response = await _run(core, "Hello JARVIS.")

    assert response.status == "completed"
    assert response.text == "Hello. I am operational."
    assert response.conversation_id is not None
    assert response.usage.input_tokens == 10
    assert set(response.stage_timings) >= {
        "validate_request", "load_context", "analyse_intent", "plan", "execute",
        "validate_result", "persist",
    }


async def test_transcript_is_persisted(core: JarvisCore, stub: StubProvider) -> None:
    stub.responses = [text_result("Noted.")]
    response = await _run(core, "Remember this.")

    async with core.database.session_factory() as session:
        from jarvis.conversations.service import ConversationService

        messages = await ConversationService(session).messages(response.conversation_id)
    assert [m.role for m in messages] == [MessageRole.USER, MessageRole.ASSISTANT]
    assert messages[1].model == "stub-model"
    assert messages[1].input_tokens == 10


async def test_conversation_continues_with_history(core: JarvisCore,
                                                   stub: StubProvider) -> None:
    stub.responses = [text_result("First."), text_result("Second.")]
    first = await _run(core, "One")

    async with core.database.session_factory() as session:
        from jarvis.conversations.service import ConversationService

        conversation = await ConversationService(session).get(first.conversation_id)
        user = await JarvisCore.ensure_default_user(session)
        second = await core.orchestrator.handle(
            session=session, user=user, message="Two", conversation=conversation
        )

    assert second.conversation_id == first.conversation_id
    # The second call must have seen the first exchange.
    last_request = stub.requests[-1]
    assert len(last_request.messages) >= 3


async def test_system_prompt_reaches_the_provider(core: JarvisCore,
                                                  stub: StubProvider) -> None:
    stub.responses = [text_result("ok")]
    await _run(core, "Hello")
    system = stub.requests[0].system or ""
    assert "You are JARVIS" in system
    assert "Security rules" in system


# ── tool use ─────────────────────────────────────────────────────────────────


async def test_agent_loop_executes_tool_then_answers(core: JarvisCore,
                                                     stub: StubProvider) -> None:
    stub.responses = [
        tool_result("create_task", {"title": "Buy milk"}),
        text_result("Added 'Buy milk' to your tasks."),
    ]
    response = await _run(core, "Add a task to buy milk")

    assert response.status == "completed"
    assert response.iterations if hasattr(response, "iterations") else True
    assert len(response.tool_calls) == 1
    assert response.tool_calls[0]["tool"] == "create_task"
    assert response.tool_calls[0]["is_error"] is False

    async with core.database.session_factory() as session:
        from jarvis.tasks.service import TaskService

        user = await JarvisCore.ensure_default_user(session)
        tasks = await TaskService(session).list(user.id)
    assert [t.title for t in tasks] == ["Buy milk"]


async def test_tool_error_is_fed_back_not_fatal(core: JarvisCore,
                                                stub: StubProvider) -> None:
    stub.responses = [
        tool_result("update_task", {"task_id": "task_missing", "status": "COMPLETED"}),
        text_result("I could not find that task."),
    ]
    response = await _run(core, "Complete task task_missing")

    assert response.status == "completed"
    assert response.tool_calls[0]["is_error"] is True
    assert "not find" in response.text


async def test_max_iterations_is_bounded(core: JarvisCore, stub: StubProvider) -> None:
    """A model that keeps calling tools must not loop forever."""
    stub.responses = [
        tool_result("get_current_time", {}, call_id=f"tu_{i}") for i in range(20)
    ]
    response = await _run(core, "What time is it?")

    assert "max_iterations_reached" in response.warnings
    assert stub.call_count <= core.settings.max_agent_iterations


async def test_tools_are_offered_to_the_provider(core: JarvisCore,
                                                 stub: StubProvider) -> None:
    stub.responses = [text_result("ok")]
    await _run(core, "Add a task please")
    assert {t.name for t in stub.requests[0].tools} == {
        "get_current_time", "system_status", "create_task", "list_tasks", "update_task"
    }


async def test_trivial_greeting_skips_tools(core: JarvisCore,
                                            stub: StubProvider) -> None:
    stub.responses = [text_result("Hello.")]
    await _run(core, "hi")
    assert list(stub.requests[0].tools) == []


# ── confirmation ─────────────────────────────────────────────────────────────


async def test_pipeline_suspends_for_confirmation(core: JarvisCore,
                                                  stub: StubProvider) -> None:
    @tool(
        name="dangerous_action", description="Deletes things",
        capability=Capability.EXECUTE, risk_level=RiskLevel.HIGH,
        reversible=False, requires_confirmation=True,
        parameters={"type": "object", "properties": {"target": {"type": "string"}},
                    "required": ["target"], "additionalProperties": False},
    )
    async def dangerous_action(*, ctx: ToolContext, target: str) -> ToolResult:
        return ToolResult.ok(f"deleted {target}")

    core.tools.register(dangerous_action)
    stub.responses = [tool_result("dangerous_action", {"target": "/tmp/x"})]

    response = await _run(core, "Delete /tmp/x")

    assert response.status == "needs_confirmation"
    assert response.pending_confirmation is not None
    assert response.pending_confirmation["tool"] == "dangerous_action"
    assert response.pending_confirmation["reversible"] is False
    assert response.pending_confirmation["risk_level"] == "HIGH"

    # The suspension must be durable — it survives into a fresh session.
    async with core.database.session_factory() as session:
        user = await JarvisCore.ensure_default_user(session)
        pending = await ConfirmationService(session).list_pending(user.id)
    assert len(pending) == 1


async def test_approved_confirmation_lets_the_action_run(core: JarvisCore,
                                                         stub: StubProvider) -> None:
    @tool(
        name="guarded", description="Guarded action", capability=Capability.EXECUTE,
        requires_confirmation=True,
        parameters={"type": "object", "properties": {"v": {"type": "string"}},
                    "required": ["v"], "additionalProperties": False},
    )
    async def guarded(*, ctx: ToolContext, v: str) -> ToolResult:
        return ToolResult.ok(f"ran with {v}")

    core.tools.register(guarded)
    stub.responses = [tool_result("guarded", {"v": "abc"})]
    first = await _run(core, "Do the guarded thing")
    assert first.status == "needs_confirmation"

    async with core.database.session_factory() as session:
        service = ConfirmationService(session)
        await service.decide(first.pending_confirmation["id"], approved=True)
        await session.commit()

    stub.responses = [
        tool_result("guarded", {"v": "abc"}),
        text_result("Done."),
    ]
    second = await _run(core, "Now do it")
    assert second.status == "completed"
    assert second.tool_calls[0]["content"] == "ran with abc"


# ── error handling ───────────────────────────────────────────────────────────


async def test_provider_failure_produces_error_response(core: JarvisCore,
                                                        stub: StubProvider) -> None:
    stub.responses = [
        ProviderRateLimitError("limited"),
        ProviderRateLimitError("limited"),
    ]
    response = await _run(core, "Hello")

    assert response.status == "error"
    assert response.error["code"] == "provider_rate_limit"
    # The user's own message survives the failure.
    async with core.database.session_factory() as session:
        from jarvis.conversations.service import ConversationService

        messages = await ConversationService(session).messages(response.conversation_id)
    assert messages[0].role is MessageRole.USER
    assert messages[0].content == "Hello"


async def test_retry_then_success(core: JarvisCore, stub: StubProvider) -> None:
    stub.responses = [ProviderRateLimitError("wait"), text_result("Recovered.")]
    response = await _run(core, "Hello")
    assert response.status == "completed"
    assert response.text == "Recovered."


async def test_refusal_is_reported_not_crashed(core: JarvisCore,
                                               stub: StubProvider) -> None:
    stub.responses = [ProviderRefusalError("declined")]
    response = await _run(core, "Something disallowed")
    assert response.status == "completed"
    assert "provider_refused" in response.warnings


async def test_empty_message_is_rejected(core: JarvisCore) -> None:
    response = await _run(core, "   ")
    assert response.status == "error"
    assert response.error["code"] == "validation_error"


async def test_empty_completion_yields_honest_message(core: JarvisCore,
                                                      stub: StubProvider) -> None:
    stub.responses = [text_result("")]
    response = await _run(core, "Hello")
    assert "empty_completion" in response.warnings
    assert response.text


async def test_activity_is_recorded_for_a_request(core: JarvisCore,
                                                  stub: StubProvider) -> None:
    stub.responses = [
        tool_result("get_current_time", {}),
        text_result("It is now."),
    ]
    response = await _run(core, "What time is it?")

    async with core.database.session_factory() as session:
        from jarvis.activity.service import ActivityService

        entries = await ActivityService(session).recent(request_id=response.request_id)
    kinds = {e.kind.value for e in entries}
    assert {"REQUEST_STARTED", "MODEL_CALL", "TOOL_CALL", "PERMISSION_DECISION",
            "REQUEST_COMPLETED"} <= kinds
