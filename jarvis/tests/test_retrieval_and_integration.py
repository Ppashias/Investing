"""Retrieval ranking, context injection, and the §33 end-to-end path.

The integration test at the bottom is the one that matters most: it drives a
real request through the orchestrator and asserts that memory was retrieved,
reached the model, and that new memory was evaluated afterwards. Everything
above it exists so that when that test fails, the failure is localised.
"""

from __future__ import annotations

import pytest

from jarvis.context.manager import ContextManager
from jarvis.db.models import ActivityKind, MemoryStatus
from jarvis.memory.evaluator import MemoryEvaluator
from jarvis.memory.projects import ProjectService
from jarvis.memory.retrieval import (
    MemoryRetriever,
    RetrievalQuery,
    RetrievalWeights,
    tokenise,
)
from jarvis.memory.service import MemoryDraft, MemoryService
from jarvis.memory.types import MemorySource, MemoryType
from jarvis.providers.embeddings import LexicalEmbeddingProvider
from tests.conftest import text_result

FACTS = [
    ("The user prefers dark mode interfaces", MemoryType.USER_PREFERENCE, "theme"),
    ("The user works best in the morning", MemoryType.USER_ROUTINE, "working hours"),
    ("The user's cat is called Mia", MemoryType.USER_FACT, "cat name"),
    ("Avoid PyAutoGUI; it is unmaintained", MemoryType.LESSON_LEARNED, "automation lib"),
]

PROJECT_FACTS = [
    ("Project X is an Unreal Engine 5.8 game about deep sea exploration",
     MemoryType.PROJECT_FACT, "project x engine"),
    ("Project X moved from a monolithic level to streamed sublevels",
     MemoryType.PROJECT_DECISION, "project x level structure"),
]


@pytest.fixture
def embeddings() -> LexicalEmbeddingProvider:
    return LexicalEmbeddingProvider()


@pytest.fixture
async def populated(session, user, embeddings):
    """A user with memories, some global and some scoped to a project."""
    service = MemoryService(session, embeddings=embeddings)
    project = await ProjectService(session).create(
        user.id, name="Project X", goals=["Ship a vertical slice"]
    )

    for content, type_, subject in FACTS:
        await service.create(
            user.id, MemoryDraft(content=content, type=type_, subject=subject)
        )
    for content, type_, subject in PROJECT_FACTS:
        await service.create(
            user.id,
            MemoryDraft(
                content=content, type=type_, subject=subject, project_id=project.id
            ),
        )
    await session.commit()
    return project


# ── ranking ──────────────────────────────────────────────────────────────────


def test_tokenise_drops_question_words() -> None:
    assert tokenise("what do you remember about Project X") == {"project", "x"}


def test_weights_depend_on_whether_embeddings_are_semantic() -> None:
    """Lexical vectors must not lead the ranking — they duplicate keyword."""
    semantic = RetrievalWeights.for_provider(True)
    lexical = RetrievalWeights.for_provider(False)
    assert semantic.semantic > semantic.keyword
    assert lexical.keyword > lexical.semantic


async def test_relevant_memory_outranks_irrelevant(session, user, populated, embeddings) -> None:
    result = await MemoryRetriever(session, embeddings=embeddings).retrieve(
        RetrievalQuery(text="should I use PyAutoGUI?", user_id=user.id, limit=4)
    )
    assert result.memories
    assert "PyAutoGUI" in result.memories[0].memory.content


async def test_project_query_surfaces_project_memory(
    session, user, populated, embeddings
) -> None:
    result = await MemoryRetriever(session, embeddings=embeddings).retrieve(
        RetrievalQuery(
            text="continue working on Project X",
            user_id=user.id,
            project_id=populated.id,
            limit=3,
        )
    )
    contents = " ".join(item.memory.content for item in result.memories)
    assert "Project X" in contents


async def test_standing_preferences_surface_without_a_keyword_match(
    session, user, populated, embeddings
) -> None:
    """"Build me a website" must surface "prefers dark interfaces"."""
    result = await MemoryRetriever(session, embeddings=embeddings).retrieve(
        RetrievalQuery(text="build me a website", user_id=user.id, limit=5)
    )
    contents = " ".join(item.memory.content for item in result.memories)
    assert "dark mode" in contents


async def test_unrelated_facts_stay_out(session, user, populated, embeddings) -> None:
    result = await MemoryRetriever(session, embeddings=embeddings).retrieve(
        RetrievalQuery(text="should I use PyAutoGUI?", user_id=user.id, limit=5)
    )
    assert not any("Mia" in item.memory.content for item in result.memories)


async def test_importance_cannot_promote_an_unrelated_memory(
    session, user, embeddings
) -> None:
    """Modifiers scale a match; they never conjure one."""
    service = MemoryService(session, embeddings=embeddings)
    await service.create(
        user.id,
        MemoryDraft(
            content="Completely unrelated but extremely important",
            type=MemoryType.USER_GOAL,
            subject="unrelated goal",
            importance=1.0,
        ),
    )
    await service.create(
        user.id,
        MemoryDraft(
            content="Vectors are stored in SQLite",
            type=MemoryType.PROJECT_FACT,
            subject="vector storage",
            importance=0.1,
        ),
    )
    await session.commit()

    result = await MemoryRetriever(session, embeddings=embeddings).retrieve(
        RetrievalQuery(text="where are vectors stored", user_id=user.id, limit=2)
    )
    assert result.memories
    assert "Vectors" in result.memories[0].memory.content


async def test_retrieval_is_bounded(session, user, embeddings) -> None:
    """§19 — never inject the whole store."""
    service = MemoryService(session, embeddings=embeddings)
    for i in range(40):
        await service.create(
            user.id,
            MemoryDraft(
                content=f"Preference number {i} about interfaces",
                type=MemoryType.USER_PREFERENCE,
                subject=f"preference {i}",
            ),
        )
    await session.commit()

    result = await MemoryRetriever(session, embeddings=embeddings).retrieve(
        RetrievalQuery(text="interfaces", user_id=user.id, limit=5)
    )
    assert len(result.memories) == 5


async def test_archived_memory_is_never_retrieved(session, user, embeddings) -> None:
    service = MemoryService(session, embeddings=embeddings)
    outcome = await service.create(
        user.id, MemoryDraft(content="Forget me about widgets", subject="widgets")
    )
    await service.archive(outcome.memory.id)
    await session.commit()

    result = await MemoryRetriever(session, embeddings=embeddings).retrieve(
        RetrievalQuery(text="widgets", user_id=user.id)
    )
    assert result.memories == []


async def test_retrieval_marks_memories_as_accessed(
    session, user, populated, embeddings
) -> None:
    manager = ContextManager(session, embeddings=embeddings)
    await manager.assemble(
        user_id=user.id, conversation_id=None, query="should I use PyAutoGUI?"
    )
    service = MemoryService(session)
    from jarvis.memory.service import MemoryFilter

    accessed = [
        m for m in await service.search(user.id, MemoryFilter())
        if m.access_count > 0
    ]
    assert accessed, "retrieval must record that a memory was used"


async def test_scores_are_inspectable(session, user, populated, embeddings) -> None:
    result = await MemoryRetriever(session, embeddings=embeddings).retrieve(
        RetrievalQuery(text="PyAutoGUI", user_id=user.id, limit=2)
    )
    described = result.describe()
    assert described
    assert set(described[0]["score"]) >= {"semantic", "keyword", "structured", "total"}


# ── context assembly ─────────────────────────────────────────────────────────


async def test_context_includes_memory(session, user, populated, embeddings) -> None:
    bundle = await ContextManager(session, embeddings=embeddings).assemble(
        user_id=user.id, conversation_id=None, query="should I use PyAutoGUI?"
    )
    assert "PyAutoGUI" in bundle.memory_context
    assert bundle.stats["memories"] >= 1


async def test_memory_block_carries_confidence_hedges(
    session, user, populated, embeddings
) -> None:
    bundle = await ContextManager(session, embeddings=embeddings).assemble(
        user_id=user.id, conversation_id=None, query="PyAutoGUI"
    )
    assert "(" in bundle.memory_context, "each line must be hedged"


async def test_context_is_empty_without_a_query(session, user, populated, embeddings) -> None:
    bundle = await ContextManager(session, embeddings=embeddings).assemble(
        user_id=user.id, conversation_id=None
    )
    assert bundle.memory_context == ""


async def test_memory_can_be_disabled(session, user, populated, embeddings) -> None:
    bundle = await ContextManager(
        session, embeddings=embeddings, memory_enabled=False
    ).assemble(user_id=user.id, conversation_id=None, query="PyAutoGUI")
    assert bundle.memory_context == ""


async def test_project_context_is_real(session, user, populated, embeddings) -> None:
    """§27 — not the id Phase 1 carried through."""
    bundle = await ContextManager(session, embeddings=embeddings).assemble(
        user_id=user.id, conversation_id=None, project_id=populated.id,
        query="continue",
    )
    assert "Project X" in bundle.project_context
    assert "Ship a vertical slice" in bundle.project_context


async def test_document_memory_taints_the_bundle(session, user, embeddings) -> None:
    """The structural defence against prompt injection, at the seam."""
    await MemoryService(session, embeddings=embeddings).create(
        user.id,
        MemoryDraft(
            content="A claim extracted from an untrusted PDF about widgets",
            subject="widget claim",
            source=MemorySource.DOCUMENT,
        ),
    )
    await session.commit()

    bundle = await ContextManager(session, embeddings=embeddings).assemble(
        user_id=user.id, conversation_id=None, query="widgets"
    )
    assert bundle.tainted is True


async def test_clean_memory_does_not_taint(session, user, populated, embeddings) -> None:
    bundle = await ContextManager(session, embeddings=embeddings).assemble(
        user_id=user.id, conversation_id=None, query="PyAutoGUI"
    )
    assert bundle.tainted is False


# ── evaluator (§12, §14) ─────────────────────────────────────────────────────


async def test_evaluator_is_unavailable_without_a_provider(session) -> None:
    assert MemoryEvaluator(session, router=None).available() is False


async def test_evaluator_skips_short_exchanges(session, core, user) -> None:
    result = await MemoryEvaluator(
        session, router=core.router, embeddings=core.embeddings
    ).evaluate_exchange(
        user_id=user.id, user_message="thanks", assistant_message="No problem."
    )
    assert result.skipped_reason == "exchange_too_short"


async def test_evaluator_proposes_rather_than_storing(session, core, user, stub) -> None:
    """Default is ask, not store (§14)."""
    stub.responses = [
        text_result(
            '{"memories": [{"content": "The user prefers dark interfaces", '
            '"subject": "interface theme", "type": "USER_PREFERENCE", '
            '"importance": 0.7, "confidence": 0.8, "reason": "stated"}]}'
        )
    ]
    result = await MemoryEvaluator(
        session, router=core.router, embeddings=core.embeddings, capture_mode="ask"
    ).evaluate_exchange(
        user_id=user.id,
        user_message="I really do prefer dark interfaces for everything",
        assistant_message="Noted.",
    )

    assert result.proposed and not result.stored
    memory = await MemoryService(session).get(result.proposed[0])
    assert memory.status is MemoryStatus.PROPOSED


async def test_evaluator_can_store_directly(session, core, user, stub) -> None:
    stub.responses = [
        text_result(
            '{"memories": [{"content": "The user prefers dark interfaces", '
            '"subject": "interface theme", "type": "USER_PREFERENCE", '
            '"importance": 0.7, "confidence": 0.8}]}'
        )
    ]
    result = await MemoryEvaluator(
        session, router=core.router, embeddings=core.embeddings, capture_mode="auto"
    ).evaluate_exchange(
        user_id=user.id,
        user_message="I really do prefer dark interfaces for everything",
        assistant_message="Noted.",
    )
    assert result.stored and not result.proposed


async def test_inferred_confidence_is_capped(session, core, user, stub) -> None:
    """A model claiming certainty about an inference does not get it."""
    stub.responses = [
        text_result(
            '{"memories": [{"content": "The user definitely prefers X", '
            '"subject": "x preference", "type": "USER_PREFERENCE", '
            '"importance": 0.9, "confidence": 1.0}]}'
        )
    ]
    result = await MemoryEvaluator(
        session, router=core.router, embeddings=core.embeddings, capture_mode="auto"
    ).evaluate_exchange(
        user_id=user.id,
        user_message="I suppose I do tend to prefer X most of the time",
        assistant_message="Understood.",
    )
    memory = await MemoryService(session).get(result.stored[0])
    assert memory.confidence < 1.0


async def test_evaluator_drops_low_importance(session, core, user, stub) -> None:
    stub.responses = [
        text_result(
            '{"memories": [{"content": "The user said hello today", '
            '"subject": "greeting", "type": "USER_FACT", '
            '"importance": 0.1, "confidence": 0.9}]}'
        )
    ]
    result = await MemoryEvaluator(
        session, router=core.router, embeddings=core.embeddings, capture_mode="auto"
    ).evaluate_exchange(
        user_id=user.id,
        user_message="Hello there, how are you doing today?",
        assistant_message="Well, thanks.",
    )
    assert not result.stored


async def test_evaluator_refuses_a_credential(session, core, user, stub) -> None:
    stub.responses = [
        text_result(
            '{"memories": [{"content": "The password is Xk8$mQ2vL9pR7z", '
            '"subject": "password", "type": "USER_FACT", '
            '"importance": 0.9, "confidence": 1.0}]}'
        )
    ]
    result = await MemoryEvaluator(
        session, router=core.router, embeddings=core.embeddings, capture_mode="auto"
    ).evaluate_exchange(
        user_id=user.id,
        user_message="My login details are in the usual place as always",
        assistant_message="Understood.",
    )
    assert result.refused == 1 and not result.stored


async def test_malformed_extraction_is_dropped_not_guessed(
    session, core, user, stub
) -> None:
    stub.responses = [text_result("not json at all")]
    result = await MemoryEvaluator(
        session, router=core.router, embeddings=core.embeddings, capture_mode="auto"
    ).evaluate_exchange(
        user_id=user.id,
        user_message="Something reasonably long to get past the pre-filter",
        assistant_message="Sure.",
    )
    assert result.skipped_reason == "nothing_worth_remembering"


# ── end to end (§33, §40) ────────────────────────────────────────────────────


async def test_full_request_path_uses_memory(core, stub) -> None:
    """USER REQUEST → MEMORY RETRIEVAL → CORE → RESPONSE → EVALUATION → STORAGE."""
    from jarvis.core import JarvisCore

    async with core.database.session_factory() as session:
        user = await JarvisCore.ensure_default_user(session)
        await MemoryService(session, embeddings=core.embeddings).create(
            user.id,
            MemoryDraft(
                content="Avoid PyAutoGUI; it is unmaintained",
                type=MemoryType.LESSON_LEARNED,
                subject="desktop automation library",
            ),
        )
        await session.commit()

        stub.responses = [text_result("I would not use PyAutoGUI.")]
        response = await core.orchestrator.handle(
            session=session,
            user=user,
            message="Should I use PyAutoGUI for desktop automation?",
        )

        assert response.status == "completed"
        # The memory reached the model, not merely the database.
        system_prompt = stub.requests[0].system or ""
        assert "PyAutoGUI" in system_prompt
        assert "What you remember" in system_prompt


async def test_evaluation_stage_runs_after_the_answer(core, stub, settings) -> None:
    """The last stage of §33's pipeline, wired for real."""
    from jarvis.core import JarvisCore
    from jarvis.orchestrator.core import Orchestrator
    from jarvis.providers.retry import RetryPolicy

    orchestrator = Orchestrator(
        registry=core.tools,
        router=core.router,
        activity_bus=core.activity_bus,
        retry=RetryPolicy(max_attempts=1, base_delay=0.001),
        embeddings=core.embeddings,
        memory_capture_mode="auto",
        memory_min_importance=0.4,
    )

    async with core.database.session_factory() as session:
        user = await JarvisCore.ensure_default_user(session)
        stub.responses = [
            text_result("Understood — I will keep that in mind."),
            # The evaluator's own call, made after the answer is persisted.
            text_result(
                '{"memories": [{"content": "The user is building Project X in '
                'Unreal Engine", "subject": "project x engine", '
                '"type": "PROJECT_FACT", "importance": 0.8, "confidence": 0.9}]}'
            ),
        ]
        response = await orchestrator.handle(
            session=session,
            user=user,
            message="I am building a game called Project X in Unreal Engine 5.8",
        )
        assert response.status == "completed"

        from jarvis.memory.service import MemoryFilter

        stored = await MemoryService(session).search(user.id, MemoryFilter())
        assert any("Project X" in m.content for m in stored), (
            "the exchange should have produced a memory"
        )

        from jarvis.activity.service import ActivityService

        activity = await ActivityService(session).recent(limit=50)
        assert any(a.kind is ActivityKind.MEMORY_CAPTURED for a in activity), (
            "memory forming in the background must be visible in the feed"
        )


async def test_a_failed_evaluation_never_breaks_the_turn(core, stub) -> None:
    from jarvis.core import JarvisCore
    from jarvis.orchestrator.core import Orchestrator
    from jarvis.providers.retry import RetryPolicy

    orchestrator = Orchestrator(
        registry=core.tools,
        router=core.router,
        activity_bus=core.activity_bus,
        retry=RetryPolicy(max_attempts=1, base_delay=0.001),
        embeddings=core.embeddings,
        memory_capture_mode="auto",
    )

    async with core.database.session_factory() as session:
        user = await JarvisCore.ensure_default_user(session)
        # One response for the answer; the evaluator's call then finds the
        # queue empty and fails.
        stub.responses = [text_result("Here is your answer.")]
        response = await orchestrator.handle(
            session=session,
            user=user,
            message="A question long enough to pass the evaluator pre-filter",
        )
        assert response.status == "completed"
        assert response.text == "Here is your answer."
