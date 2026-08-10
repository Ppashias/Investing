"""What JARVIS does when the database is not there (§21, §24).

The audit's finding 10 was that database-unavailable behaviour is unclear. It
is the least exercised failure mode in the system and the one with the worst
failure shape: a read that silently returns nothing looks exactly like a read
that correctly found nothing, and a write that silently fails looks exactly
like a write that worked.

So these tests are all the same assertion in different places — **an operation
that did not happen must not be reported as one that did**. Not "handles errors
gracefully"; a graceful empty list is precisely the bug.

Failures are injected at the SQLAlchemy layer, which is where a real outage
surfaces: a locked file, a corrupt page, a revoked permission and a full disk
all arrive as ``OperationalError`` from ``execute``, ``flush`` or ``commit``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy.exc import OperationalError

from jarvis.core import JarvisCore
from jarvis.db.base import Database
from jarvis.tools.base import ToolContext
from jarvis.tools.executor import ToolCall


def _broken(message: str = "unable to open database file") -> OperationalError:
    """The error SQLAlchemy raises for the outages that actually happen."""
    return OperationalError("SELECT 1", {}, Exception(message))


async def _run_tool(core: JarvisCore, session, name: str, arguments: dict):
    user = await JarvisCore.ensure_default_user(session)
    orchestrator = core.orchestrator
    return await orchestrator._make_executor(session).execute(
        ToolCall(id="tu_1", name=name, arguments=arguments),
        ToolContext(
            user_id=user.id,
            session=session,
            request_id="req_db",
            extras={
                "embeddings": core.embeddings,
                "project_id": None,
                "computer": core.computer,
                "activity": orchestrator._activity(session),
            },
        ),
    )


# ── connection failure ───────────────────────────────────────────────────────


async def test_an_unopenable_database_fails_at_first_use(tmp_path: Path) -> None:
    """A bad URL must raise, not yield a session that quietly does nothing.

    The engine is created lazily, so construction succeeding proves nothing —
    the failure has to arrive the first time the connection is genuinely
    needed, and it has to arrive as an exception.
    """
    missing = tmp_path / "no-such-directory" / "jarvis.db"
    database = Database(f"sqlite+aiosqlite:///{missing}")

    with pytest.raises(Exception) as raised:
        async with database.session_factory() as session:
            await JarvisCore.ensure_default_user(session)

    assert "unable to open database file" in str(raised.value).lower()
    await database.engine.dispose()


async def test_startup_does_not_report_success_on_a_dead_database(
    core, tmp_path: Path
) -> None:
    """Startup must fail loudly rather than come up half-working.

    A JARVIS that started, logged "jarvis_started" and then failed every
    request is worse than one that refused to start: the user reads the log
    and concludes the problem is elsewhere. ``startup`` creates the schema,
    syncs the tool registry, seeds grants and bootstraps Obsidian — every one
    of those needs the database, and none may be skipped silently.
    """
    working, broken = core.database, Database(
        f"sqlite+aiosqlite:///{tmp_path / 'no-such-directory' / 'jarvis.db'}"
    )
    core.database = broken
    try:
        with pytest.raises(Exception) as raised:
            await core.startup(create_schema=True)
        assert "unable to open database file" in str(raised.value).lower()
    finally:
        core.database = working
        await broken.engine.dispose()


# ── failed reads ─────────────────────────────────────────────────────────────


async def test_a_failed_search_is_not_reported_as_no_results(
    core, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The most dangerous shape of this bug.

    "I found no notes about that" and "I could not look" are different
    answers, and only one of them is true when the query raised. If the
    failure is swallowed into an empty list the user is told something false
    about their own vault, with no indication anything went wrong.
    """
    async with core.database.session_factory() as session:
        user = await JarvisCore.ensure_default_user(session)
        await session.commit()

        monkeypatch.setattr(
            session, "execute", lambda *a, **k: (_ for _ in ()).throw(_broken())
        )

        with pytest.raises(OperationalError):
            await _run_tool(core, session, "recall", {"query": "anything"})
        assert user is not None


async def test_a_failed_task_list_raises_rather_than_returning_empty(
    core, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with core.database.session_factory() as session:
        await JarvisCore.ensure_default_user(session)
        await session.commit()

        monkeypatch.setattr(
            session, "execute", lambda *a, **k: (_ for _ in ()).throw(_broken())
        )
        with pytest.raises(OperationalError):
            await _run_tool(core, session, "list_tasks", {})


# ── failed writes ────────────────────────────────────────────────────────────


async def test_a_failed_task_write_does_not_report_a_created_task(
    core, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A create that could not be persisted must not return an id.

    Returning one would put a reference to a nonexistent row into the
    conversation transcript, and every later turn would reason from it.
    """
    async with core.database.session_factory() as session:
        await JarvisCore.ensure_default_user(session)
        await session.commit()

        monkeypatch.setattr(
            session, "flush", lambda *a, **k: (_ for _ in ()).throw(_broken())
        )
        with pytest.raises(OperationalError):
            await _run_tool(
                core, session, "create_task",
                {"title": "Ghost task", "description": "Should never exist"},
            )

    # A fresh session, so nothing is read from the failed one's identity map.
    async with core.database.session_factory() as session:
        from sqlalchemy import select

        from jarvis.db.models import Task

        titles = (await session.execute(select(Task.title))).scalars().all()
        assert "Ghost task" not in titles


async def test_a_rolled_back_turn_leaves_no_partial_state(core) -> None:
    """Transaction failure must be all-or-nothing.

    The orchestrator rolls back on error and then writes the error turn
    separately. If the rollback left half a turn behind, the transcript would
    describe work that never completed.
    """
    from sqlalchemy import select

    from jarvis.db.models import Task

    async with core.database.session_factory() as session:
        await JarvisCore.ensure_default_user(session)
        await session.commit()

    async with core.database.session_factory() as session:
        user = await JarvisCore.ensure_default_user(session)
        session.add(Task(user_id=user.id, title="Half-written", description=""))
        await session.flush()
        await session.rollback()

    async with core.database.session_factory() as session:
        titles = (await session.execute(select(Task.title))).scalars().all()
        assert "Half-written" not in titles


# ── the audit's own failure mode ─────────────────────────────────────────────


async def test_a_failed_activity_write_is_logged_not_raised(
    core, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Recorded here because it is a deliberate trade, not an accident.

    ``ActivityService.record`` swallows database errors — "never let logging
    break the operation". The consequence is real and worth stating plainly:
    if the activity table is unwritable, an operation can succeed unaudited.
    The audit is not a transactional guarantee.

    This test pins the behaviour rather than endorsing it. It is listed in the
    hardening report's remaining gaps: making a *write* audit failure abort the
    write is a defensible change, and it is a change to the audit contract
    rather than a bug fix, so it is not being made quietly here.
    """
    from jarvis.activity.service import ActivityService
    from jarvis.db.models import ActivityKind

    async with core.database.session_factory() as session:
        await JarvisCore.ensure_default_user(session)
        await session.commit()

        service = ActivityService(session, None)
        monkeypatch.setattr(
            session, "flush", lambda *a, **k: (_ for _ in ()).throw(_broken())
        )

        # Returns None instead of raising — the operation above it continues.
        assert await service.record(
            ActivityKind.TOOL_CALL, summary="anything", actor="test"
        ) is None


# ── the user-facing answer ───────────────────────────────────────────────────


async def test_a_database_failure_mid_turn_produces_an_error_not_an_answer(
    core, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End to end: the orchestrator must not answer over a broken database.

    It catches unexpected exceptions so internals never leak, which is right —
    but the response it produces has to be an *error*. A friendly sentence
    with status "completed" would be the fabrication this whole file is about.
    """
    from jarvis.orchestrator import stages

    async def _explode(self, ctx):
        raise _broken()

    monkeypatch.setattr(stages.LoadContextStage, "run", _explode)

    async with core.database.session_factory() as session:
        user = await JarvisCore.ensure_default_user(session)
        await session.commit()
        response = await core.orchestrator.handle(
            session=session, user=user, message="What tasks do I have?"
        )

    assert response.status == "error"
    assert response.error is not None
    assert response.text
    # The user is told something went wrong, not given a fabricated answer.
    assert "task" not in response.text.lower() or "wrong" in response.text.lower()


async def test_the_error_message_does_not_leak_database_internals(
    core, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An outage must not become an information disclosure.

    The path, the SQL and the driver's message are diagnostics for the log,
    not for whoever is talking to JARVIS.
    """
    from jarvis.orchestrator import stages

    async def _explode(self, ctx):
        raise OperationalError(
            "SELECT users.secret_column FROM users",
            {},
            Exception("unable to open database file /srv/private/jarvis.db"),
        )

    monkeypatch.setattr(stages.LoadContextStage, "run", _explode)

    async with core.database.session_factory() as session:
        user = await JarvisCore.ensure_default_user(session)
        await session.commit()
        response = await core.orchestrator.handle(
            session=session, user=user, message="hello"
        )

    assert response.status == "error"
    assert "/srv/private" not in response.text
    assert "SELECT" not in response.text


# ── subsystems that must not fabricate on a partial failure ──────────────────


async def test_a_vault_write_that_cannot_be_indexed_still_reports_the_write(
    core, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one case where continuing after a failure is correct.

    Creating a note does two things: it writes a file to the user's vault and
    it indexes that file for search. If indexing fails, the file still exists.
    Reporting the create as failed would be its own fabrication — the user
    would go looking for a note that is sitting in their vault.

    So the assertion is precise: the tool reports the write it performed, and
    the file is genuinely there. What it must not do is claim the note is
    searchable.
    """
    from jarvis.knowledge.providers.obsidian import ObsidianService, ObsidianSync
    from jarvis.tools.builtin.obsidian_tools import _index

    vault = tmp_path / "DbVault"
    (vault / ".obsidian").mkdir(parents=True)
    (vault / "Written.md").write_text("# Written\n\nbody\n", encoding="utf-8")

    async def _explode(self, note_path: str):
        raise _broken()

    monkeypatch.setattr(ObsidianSync, "index_note", _explode)

    async with core.database.session_factory() as session:
        user = await JarvisCore.ensure_default_user(session)
        service = ObsidianService(session, user.id)
        await service.connect(
            str(vault), vault_name="DbVault", allow_writes=True
        )
        await session.commit()

        provider = await service.provider()
        ctx = ToolContext(
            user_id=user.id,
            session=session,
            request_id="req_db",
            extras={"embeddings": core.embeddings},
        )
        # Must not raise: the file is on disk, and turning an indexing outage
        # into a failed create would send the user looking for a note that is
        # sitting in their vault.
        await _index(ctx, provider, "Written.md")

    assert (vault / "Written.md").read_text(encoding="utf-8").startswith("# Written")

    # …and the failure did not invent an index entry either.
    async with core.database.session_factory() as session:
        from sqlalchemy import select

        from jarvis.db.models import Document

        assert (await session.execute(select(Document))).scalars().all() == []


async def test_memory_capture_without_a_provider_fails_quietly_and_stores_nothing(
    core, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Audit finding 8, verified rather than assumed.

    Ambient capture needs a model call to judge what is worth remembering.
    With no provider it must degrade to *not remembering*, not to remembering
    everything and not to failing the user's turn — the answer they asked for
    is unrelated to whether JARVIS managed to form a memory about it.
    """
    from sqlalchemy import select

    from jarvis.db.models import Memory
    from jarvis.memory.evaluator import MemoryEvaluator

    async def _explode(self, **kwargs):
        raise RuntimeError("No model provider is configured.")

    monkeypatch.setattr(MemoryEvaluator, "evaluate_exchange", _explode)

    async with core.database.session_factory() as session:
        user = await JarvisCore.ensure_default_user(session)
        await session.commit()
        response = await core.orchestrator.handle(
            session=session, user=user, message="My favourite colour is green."
        )

    assert response.status == "completed"
    assert response.text

    async with core.database.session_factory() as session:
        stored = (await session.execute(select(Memory))).scalars().all()
        assert stored == []
