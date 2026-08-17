"""Goals, commitments, and chasing (Phase D, item 12).

JARVIS was entirely reactive. The capability worth adding is not storage — the
task tree already had everything — but three questions nobody could ask: how
far along is this, what is due, and what has gone quiet.

The tests that matter most are the ones about *not* acting, and about progress
being counted rather than estimated.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from jarvis.db.base import utcnow
from jarvis.db.models import Task, TaskStatus
from jarvis.goals.service import GoalService


async def test_a_goal_is_a_task_tree_not_a_new_object(session, user) -> None:
    """A goals table would have duplicated the status machine, and the second
    copy is the one that drifts."""
    goal = await GoalService(session).create_goal(
        user.id, title="Ship Phase D", steps=["item 12", "item 10"]
    )
    assert isinstance(goal, Task)

    children = (await session.execute(
        Task.__table__.select().where(Task.parent_task_id == goal.id)
    )).all()
    assert len(children) == 2


async def test_progress_is_counted_rather_than_estimated(session, user) -> None:
    """"About 60% done" from a language model is a number with no referent, and
    a progress bar that moves for no reason is worse than none."""
    service = GoalService(session)
    goal = await service.create_goal(
        user.id, title="Ship it", steps=["a", "b", "c", "d"]
    )
    children = (await session.execute(
        Task.__table__.select().where(Task.parent_task_id == goal.id)
    )).all()

    first = await session.get(Task, children[0].id)
    first.status = TaskStatus.COMPLETED
    await session.flush()

    progress = await service.progress(goal.id)
    assert progress.total == 4
    assert progress.completed == 1
    assert progress.fraction == 0.25


async def test_a_goal_with_no_steps_still_reports_progress(session, user) -> None:
    """A goal nobody has broken down yet is a normal state, not a division by
    zero that returns nothing."""
    service = GoalService(session)
    goal = await service.create_goal(user.id, title="Think about it")
    progress = await service.progress(goal.id)
    assert progress.total == 1
    assert progress.fraction == 0.0


async def test_steps_inherit_the_deadline_of_what_they_serve(session, user) -> None:
    """Otherwise "due Friday" applies to the goal while every step looks
    open-ended, and nothing would ever be surfaced as due."""
    due = utcnow() + timedelta(days=1)
    service = GoalService(session)
    goal = await service.create_goal(
        user.id, title="Send the report", steps=["draft", "send"], due_at=due
    )
    children = (await session.execute(
        Task.__table__.select().where(Task.parent_task_id == goal.id)
    )).all()
    assert all(row.due_at is not None for row in children)


# ── chasing ──────────────────────────────────────────────────────────────────


async def test_overdue_outranks_due_soon_outranks_quiet(session, user) -> None:
    now = utcnow()
    session.add_all([
        Task(user_id=user.id, title="late", status=TaskStatus.TODO,
             due_at=now - timedelta(days=2)),
        Task(user_id=user.id, title="soon", status=TaskStatus.TODO,
             due_at=now + timedelta(days=1)),
    ])
    quiet = Task(user_id=user.id, title="quiet", status=TaskStatus.TODO)
    quiet.updated_at = now - timedelta(days=40)
    session.add(quiet)
    await session.flush()

    found = await GoalService(session).follow_ups(user.id)
    assert [f.title for f in found] == ["late", "soon", "quiet"]
    assert found[0].reason == "overdue"


async def test_a_promise_outranks_an_intention(session, user) -> None:
    """"I should tidy the vault sometime" and "I told Sam I would send it by
    Friday" are different objects, and only the second should chase you."""
    now = utcnow()
    service = GoalService(session)
    await service.create_goal(user.id, title="tidy the vault",
                              due_at=now - timedelta(days=1))
    await service.create_goal(user.id, title="send it to Sam",
                              due_at=now - timedelta(days=1), is_commitment=True)

    found = await service.follow_ups(user.id)
    assert found[0].title == "send it to Sam"
    assert found[0].is_commitment is True


async def test_a_finished_task_is_never_chased(session, user) -> None:
    session.add(
        Task(user_id=user.id, title="done", status=TaskStatus.COMPLETED,
             due_at=utcnow() - timedelta(days=5))
    )
    await session.flush()
    assert await GoalService(session).follow_ups(user.id) == []


async def test_every_follow_up_says_why(session, user) -> None:
    """A nudge with no stated cause reads as nagging."""
    session.add(
        Task(user_id=user.id, title="late", status=TaskStatus.TODO,
             due_at=utcnow() - timedelta(days=1))
    )
    await session.flush()

    found = await GoalService(session).follow_ups(user.id)
    assert found and all(f.reason for f in found)
    assert "reason" in found[0].describe()


# ── the boundary ─────────────────────────────────────────────────────────────


def test_the_goal_service_cannot_act() -> None:
    """It answers questions; the caller decides what to do about the answer.

    A goal system that could act on its own schedule is an autonomous actor,
    and autonomous actors here go through ToolExecutor like everything else.
    Making the *reminder* a tool call keeps that true; a side effect in this
    module would not.
    """
    import ast
    import inspect

    from jarvis.goals import service

    # Code only. The module docstring *explains* the boundary, so a plain text
    # scan would match its own explanation.
    tree = ast.parse(inspect.getsource(service))
    called = {
        node.func.attr if isinstance(node.func, ast.Attribute) else
        getattr(node.func, "id", "")
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
    }
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    } | {
        node.module or ""
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    }

    # ``session.execute`` is a SELECT and entirely the point; what must not
    # appear is anything that acts on the world.
    for forbidden in ("execute_safe", "notify", "send", "spawn", "start"):
        assert forbidden not in called, f"the goal service calls {forbidden}()"
    for forbidden in ("httpx", "requests", "jarvis.tools.executor"):
        assert forbidden not in imported, f"the goal service imports {forbidden}"


async def test_progress_of_an_unknown_goal_is_an_error_not_a_zero(
    session, user
) -> None:
    """Returning empty progress for a goal that does not exist would render as
    "0% done" rather than "no such thing"."""
    with pytest.raises(LookupError):
        await GoalService(session).progress("task_nope")
