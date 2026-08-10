"""Task engine (including the execution model) and conversation persistence."""

from __future__ import annotations

import pytest

from jarvis.conversations.service import ConversationService
from jarvis.db.models import ExecutionStatus, MessageRole, TaskPriority, TaskStatus
from jarvis.errors import InvalidStateTransitionError, NotFoundError, ValidationError
from jarvis.providers.base import TextBlock, ToolResultBlock, ToolUseBlock
from jarvis.tasks.service import TaskFilter, TaskService


# ── tasks ────────────────────────────────────────────────────────────────────


async def test_create_task_records_history(session, user) -> None:
    service = TaskService(session)
    task = await service.create(user_id=user.id, title="Write tests")

    assert task.status is TaskStatus.TODO
    assert task.priority is TaskPriority.NORMAL
    history = await service.history(task.id)
    assert [h.field for h in history] == ["created"]


async def test_create_rejects_blank_title(session, user) -> None:
    with pytest.raises(ValidationError):
        await TaskService(session).create(user_id=user.id, title="   ")


async def test_get_missing_task_raises_not_found(session) -> None:
    with pytest.raises(NotFoundError):
        await TaskService(session).get("task_nope")


async def test_update_records_field_level_history(session, user) -> None:
    service = TaskService(session)
    task = await service.create(user_id=user.id, title="Original")
    await service.update(task.id, title="Renamed", priority=TaskPriority.HIGH,
                         actor="user", note="bumped")

    changed = {h.field for h in await service.history(task.id)}
    assert {"created", "title", "priority"} <= changed


async def test_status_transitions_are_validated(session, user) -> None:
    service = TaskService(session)
    task = await service.create(user_id=user.id, title="T")

    await service.update(task.id, status=TaskStatus.IN_PROGRESS)
    await service.complete(task.id)
    assert task.status is TaskStatus.COMPLETED

    with pytest.raises(InvalidStateTransitionError):
        await service.update(task.id, status=TaskStatus.BLOCKED)


async def test_failed_task_can_be_retried(session, user) -> None:
    """FAILED is not terminal — that is the point of the execution model."""
    service = TaskService(session)
    task = await service.create(user_id=user.id, title="Flaky")
    await service.update(task.id, status=TaskStatus.IN_PROGRESS)
    await service.update(task.id, status=TaskStatus.FAILED)
    await service.update(task.id, status=TaskStatus.IN_PROGRESS)
    assert task.status is TaskStatus.IN_PROGRESS


async def test_cancel_sets_terminal_state(session, user) -> None:
    service = TaskService(session)
    task = await service.create(user_id=user.id, title="Abandon")
    await service.cancel(task.id, note="not needed")
    assert task.status is TaskStatus.CANCELLED


async def test_update_rejects_unknown_field(session, user) -> None:
    service = TaskService(session)
    task = await service.create(user_id=user.id, title="T")
    with pytest.raises(ValidationError):
        await service.update(task.id, completed_at="now")


async def test_filters_and_counts(session, user) -> None:
    service = TaskService(session)
    a = await service.create(user_id=user.id, title="Alpha task",
                             priority=TaskPriority.HIGH)
    await service.create(user_id=user.id, title="Beta task")
    await service.update(a.id, status=TaskStatus.IN_PROGRESS)

    assert len(await service.list(user.id, TaskFilter(search="Alpha"))) == 1
    assert len(await service.list(
        user.id, TaskFilter(status=[TaskStatus.IN_PROGRESS]))) == 1
    assert len(await service.list(user.id, TaskFilter(include_terminal=False))) == 2

    counts = await service.counts_by_status(user.id)
    assert counts["TODO"] == 1 and counts["IN_PROGRESS"] == 1


async def test_subtasks_link_to_parent(session, user) -> None:
    service = TaskService(session)
    parent = await service.create(user_id=user.id, title="Parent")
    child = await service.create(user_id=user.id, title="Child",
                                 parent_task_id=parent.id)
    assert child.parent_task_id == parent.id


# ── executions ───────────────────────────────────────────────────────────────


async def test_executions_increment_and_preserve_history(session, user) -> None:
    """Task = intent, execution = attempt. Two failures then a success leaves
    one task and three execution records."""
    service = TaskService(session)
    task = await service.create(user_id=user.id, title="Render video")

    first = await service.start_execution(task.id, trigger="manual")
    await service.finish_execution(
        first.id,
        status=ExecutionStatus.FAILED,
        error_code="editor_unavailable",
        error_message="Video editor was not running",
    )

    second = await service.start_execution(task.id, trigger="retry")
    await service.finish_execution(
        second.id, status=ExecutionStatus.SUCCEEDED, result={"path": "/out.mp4"}
    )

    executions = await service.executions(task.id)
    assert [e.attempt for e in executions] == [1, 2]
    assert executions[0].error_code == "editor_unavailable"
    assert executions[1].result == {"path": "/out.mp4"}
    assert task.status is TaskStatus.COMPLETED


async def test_execution_moves_task_to_in_progress(session, user) -> None:
    service = TaskService(session)
    task = await service.create(user_id=user.id, title="Work")
    await service.start_execution(task.id)
    assert task.status is TaskStatus.IN_PROGRESS


async def test_failed_execution_propagates_to_task(session, user) -> None:
    service = TaskService(session)
    task = await service.create(user_id=user.id, title="Work")
    execution = await service.start_execution(task.id)
    await service.finish_execution(execution.id, status=ExecutionStatus.FAILED)
    assert task.status is TaskStatus.FAILED


# ── conversations ────────────────────────────────────────────────────────────


async def test_messages_get_monotonic_sequence(session, user) -> None:
    service = ConversationService(session)
    conversation = await service.create(user_id=user.id)
    for i in range(3):
        await service.add_message(
            conversation_id=conversation.id, role=MessageRole.USER, content=f"m{i}"
        )
    messages = await service.messages(conversation.id)
    assert [m.sequence for m in messages] == [0, 1, 2]


async def test_autotitle_from_first_message(session, user) -> None:
    service = ConversationService(session)
    conversation = await service.create(user_id=user.id)
    await service.maybe_autotitle(conversation, "Plan my week around the Unreal build")
    assert conversation.title != "New conversation"

    before = conversation.title
    await service.maybe_autotitle(conversation, "Something else entirely")
    assert conversation.title == before, "should only title once"


async def test_tool_turns_replay_losslessly(session, user) -> None:
    """A turn containing tool calls must reconstruct exactly — a lossy rebuild
    from prose produces subtly wrong provider behaviour."""
    service = ConversationService(session)
    conversation = await service.create(user_id=user.id)

    await service.add_message(
        conversation_id=conversation.id, role=MessageRole.USER,
        content="What time is it?", blocks=[TextBlock(text="What time is it?")],
    )
    await service.add_message(
        conversation_id=conversation.id, role=MessageRole.ASSISTANT, content="",
        blocks=[ToolUseBlock(id="tu_1", name="get_current_time", input={"timezone": "UTC"})],
    )
    await service.add_message(
        conversation_id=conversation.id, role=MessageRole.TOOL, content="12:00",
        blocks=[ToolResultBlock(tool_use_id="tu_1", content="12:00")],
    )
    await service.add_message(
        conversation_id=conversation.id, role=MessageRole.ASSISTANT,
        content="It is 12:00 UTC.", blocks=[TextBlock(text="It is 12:00 UTC.")],
    )

    replay = service.to_provider_messages(await service.messages(conversation.id))
    roles = [m.role for m in replay]
    assert roles == ["user", "assistant", "user", "assistant"]

    tool_use = replay[1].content[0]
    assert isinstance(tool_use, ToolUseBlock)
    assert tool_use.input == {"timezone": "UTC"}

    tool_res = replay[2].content[0]
    assert isinstance(tool_res, ToolResultBlock)
    assert tool_res.tool_use_id == "tu_1"


async def test_system_messages_are_not_replayed(session, user) -> None:
    """The system prompt is rebuilt per turn; replaying a stored one would
    double it and pin the conversation to stale instructions."""
    service = ConversationService(session)
    conversation = await service.create(user_id=user.id)
    await service.add_message(
        conversation_id=conversation.id, role=MessageRole.SYSTEM, content="old prompt"
    )
    await service.add_message(
        conversation_id=conversation.id, role=MessageRole.USER, content="hi"
    )
    replay = service.to_provider_messages(await service.messages(conversation.id))
    assert len(replay) == 1 and replay[0].role == "user"


async def test_trailing_tool_result_still_delivered(session, user) -> None:
    """An unanswered tool_use makes providers reject the request, so a tool
    result with no following user turn must still be emitted."""
    service = ConversationService(session)
    conversation = await service.create(user_id=user.id)
    await service.add_message(
        conversation_id=conversation.id, role=MessageRole.ASSISTANT, content="",
        blocks=[ToolUseBlock(id="tu_9", name="x", input={})],
    )
    await service.add_message(
        conversation_id=conversation.id, role=MessageRole.TOOL, content="done",
        blocks=[ToolResultBlock(tool_use_id="tu_9", content="done")],
    )
    replay = service.to_provider_messages(await service.messages(conversation.id))
    assert replay[-1].role == "user"
    assert isinstance(replay[-1].content[0], ToolResultBlock)


async def test_history_limit_returns_most_recent(session, user) -> None:
    service = ConversationService(session)
    conversation = await service.create(user_id=user.id)
    for i in range(10):
        await service.add_message(
            conversation_id=conversation.id, role=MessageRole.USER, content=f"m{i}"
        )
    recent = await service.messages(conversation.id, limit=3)
    assert [m.content for m in recent] == ["m7", "m8", "m9"]
