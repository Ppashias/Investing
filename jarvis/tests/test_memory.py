"""Memory: lifecycle, deduplication, contradiction, confidence, importance.

Covers §40's memory and security checklists. The security-critical behaviours —
the secret guard, ownership isolation, the confidence ceiling on inference —
each have a test that fails if the rule is removed rather than merely
exercising the happy path.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from jarvis.db.base import utcnow
from jarvis.db.models import EmbeddingOwner, Memory
from jarvis.errors import NotFoundError, ValidationError
from jarvis.memory import guard
from jarvis.memory.service import (
    MemoryDraft,
    MemoryFilter,
    MemoryService,
    SecretInMemoryError,
    normalise_subject,
)
from jarvis.memory.types import (
    CONFIDENCE_EXPLICIT,
    MemoryRelation,
    MemorySource,
    MemoryStatus,
    MemoryType,
    confidence_band,
    hedge_for,
    importance_band,
)
from jarvis.memory.vectors import SqliteVectorIndex, pack_vector, unpack_vector
from jarvis.providers.embeddings import LexicalEmbeddingProvider


@pytest.fixture
def embeddings() -> LexicalEmbeddingProvider:
    return LexicalEmbeddingProvider()


@pytest.fixture
def memories(session, embeddings) -> MemoryService:
    return MemoryService(session, embeddings=embeddings)


def draft(content: str, **kwargs) -> MemoryDraft:
    kwargs.setdefault("type", MemoryType.USER_PREFERENCE)
    return MemoryDraft(content=content, **kwargs)


# ── create / read / update / delete ──────────────────────────────────────────


async def test_create_and_retrieve(memories, user) -> None:
    outcome = await memories.create(user.id, draft("The user prefers dark mode"))
    assert outcome.action == "created"
    assert outcome.memory is not None

    fetched = await memories.get(outcome.memory.id)
    assert fetched.content == "The user prefers dark mode"
    assert fetched.status is MemoryStatus.ACTIVE
    assert fetched.subject  # derived when not supplied


async def test_memory_survives_a_new_session(core) -> None:
    """§44.2 — memory must survive a restart.

    Two independent sessions against the same database. The second knows
    nothing of the first beyond what was committed, which is the closest a
    test gets to restarting the process.
    """
    from jarvis.core import JarvisCore

    async with core.database.session_factory() as first:
        owner = await JarvisCore.ensure_default_user(first)
        service = MemoryService(first, embeddings=LexicalEmbeddingProvider())
        outcome = await service.create(owner.id, draft("Persisted across sessions"))
        memory_id = outcome.memory.id
        await first.commit()

    async with core.database.session_factory() as second:
        reloaded = await MemoryService(second).get(memory_id)
        assert reloaded.content == "Persisted across sessions"
        assert isinstance(reloaded.type, MemoryType), "enum must round-trip"
        assert isinstance(reloaded.status, MemoryStatus)


async def test_update_records_a_revision(memories, user) -> None:
    outcome = await memories.create(user.id, draft("The user prefers tea"))
    await memories.update(
        outcome.memory.id, content="The user prefers coffee", note="corrected"
    )

    history = await memories.history(outcome.memory.id)
    kinds = [h.kind.value for h in history]
    assert "CORRECTED" in kinds and "CREATED" in kinds
    correction = next(h for h in history if h.kind.value == "CORRECTED")
    assert correction.changes["content"]["from"] == "The user prefers tea"


async def test_archive_then_restore(memories, user) -> None:
    outcome = await memories.create(user.id, draft("Temporary"))
    await memories.archive(outcome.memory.id)
    assert (await memories.get(outcome.memory.id)).status is MemoryStatus.ARCHIVED

    await memories.restore(outcome.memory.id)
    assert (await memories.get(outcome.memory.id)).status is MemoryStatus.ACTIVE


async def test_delete_erases_content_but_keeps_a_tombstone(memories, user) -> None:
    outcome = await memories.create(user.id, draft("Something private"))
    memory_id = outcome.memory.id

    await memories.delete(memory_id)
    tombstone = await memories.get(memory_id)

    assert tombstone.status is MemoryStatus.DELETED
    assert tombstone.content == "", "content must actually be gone (§35)"
    assert tombstone.subject is None
    # And it must not come back in any listing.
    listed = await memories.search(user.id, MemoryFilter())
    assert memory_id not in {m.id for m in listed}


async def test_delete_removes_the_vector_too(memories, session, user, embeddings) -> None:
    """A deleted memory must not remain findable by similarity."""
    outcome = await memories.create(user.id, draft("Findable by vector"))
    index = SqliteVectorIndex(session)
    query = await embeddings.embed_one("findable by vector")

    before = await index.search(
        owner_kind=EmbeddingOwner.MEMORY, model=embeddings.info.model, query=query
    )
    assert outcome.memory.id in {h.owner_id for h in before}

    await memories.delete(outcome.memory.id)
    after = await index.search(
        owner_kind=EmbeddingOwner.MEMORY, model=embeddings.info.model, query=query
    )
    assert outcome.memory.id not in {h.owner_id for h in after}


async def test_deleted_memory_cannot_be_edited(memories, user) -> None:
    outcome = await memories.create(user.id, draft("Gone"))
    await memories.delete(outcome.memory.id)
    with pytest.raises(ValidationError):
        await memories.update(outcome.memory.id, content="back again")


# ── deduplication (§15) ──────────────────────────────────────────────────────


async def test_restatement_merges_rather_than_duplicating(memories, user) -> None:
    first = await memories.create(
        user.id, draft("The user prefers dark mode", subject="interface theme")
    )
    second = await memories.create(
        user.id, draft("The user prefers dark mode", subject="interface theme")
    )

    assert second.action == "merged"
    assert second.memory.id == first.memory.id
    assert len(await memories.search(user.id, MemoryFilter())) == 1


async def test_merging_raises_confidence_but_never_to_certainty(memories, user) -> None:
    """Repetition is evidence. It must not manufacture certainty."""
    await memories.create(
        user.id, draft("Prefers dark mode", subject="theme", confidence=0.5)
    )
    for _ in range(10):
        outcome = await memories.create(
            user.id, draft("Prefers dark mode", subject="theme", confidence=0.5)
        )

    assert outcome.action == "merged"
    assert outcome.memory.confidence > 0.5, "restatement should strengthen"
    assert outcome.memory.confidence < CONFIDENCE_EXPLICIT, (
        "only an explicit instruction earns certainty"
    )


async def test_different_subjects_are_not_merged(memories, user) -> None:
    await memories.create(user.id, draft("Prefers dark mode", subject="theme"))
    outcome = await memories.create(
        user.id, draft("Prefers dark roast coffee", subject="coffee preference")
    )
    assert outcome.action == "created"
    assert len(await memories.search(user.id, MemoryFilter())) == 2


async def test_project_scope_separates_identical_subjects(memories, user, session) -> None:
    from jarvis.memory.projects import ProjectService

    project = await ProjectService(session).create(user.id, name="Project X")
    await memories.create(
        user.id, draft("Uses Unreal", subject="engine", type=MemoryType.PROJECT_FACT)
    )
    outcome = await memories.create(
        user.id,
        draft(
            "Uses Godot", subject="engine", type=MemoryType.PROJECT_FACT,
            project_id=project.id,
        ),
    )
    assert outcome.action == "created", "a project fact is not a global fact"


# ── contradiction (§16) ──────────────────────────────────────────────────────


async def test_contradiction_supersedes_and_preserves_history(memories, user) -> None:
    old = await memories.create(
        user.id, draft("The user prefers working at night", subject="working hours")
    )
    new = await memories.create(
        user.id,
        draft("The user prefers working in the morning", subject="working hours"),
    )

    assert new.action == "superseded"
    assert new.previous_id == old.memory.id

    superseded = await memories.get(old.memory.id)
    assert superseded.status is MemoryStatus.SUPERSEDED
    assert superseded.superseded_by == new.memory.id
    # The old content is preserved, not erased — "what did I used to think?"
    assert superseded.content == "The user prefers working at night"

    related = await memories.related(new.memory.id)
    assert any(r is MemoryRelation.SUPERSEDES for _, r in related)


async def test_only_the_newer_memory_is_retrievable(memories, user) -> None:
    await memories.create(user.id, draft("Prefers night", subject="hours"))
    await memories.create(user.id, draft("Prefers morning", subject="hours"))

    active = await memories.search(user.id, MemoryFilter(statuses=[MemoryStatus.ACTIVE]))
    assert [m.content for m in active] == ["Prefers morning"]


async def test_episodic_memories_never_contradict(memories, user) -> None:
    """A newer event does not falsify an older one."""
    for text in ("Completed Phase 1 of JARVIS", "Completed Phase 2 of JARVIS"):
        await memories.create(
            user.id,
            MemoryDraft(content=text, type=MemoryType.IMPORTANT_EVENT, subject="phase"),
        )

    active = await memories.search(user.id, MemoryFilter(statuses=[MemoryStatus.ACTIVE]))
    assert len(active) == 2, "history must not be rewritten by a later event"


async def test_pinned_memory_is_not_superseded(memories, user) -> None:
    await memories.create(
        user.id, draft("Prefers night", subject="hours", pinned=True)
    )
    outcome = await memories.create(user.id, draft("Prefers morning", subject="hours"))
    assert outcome is not None
    pinned = await memories.search(user.id, MemoryFilter())
    assert any(m.pinned and m.status is MemoryStatus.ACTIVE for m in pinned)


# ── confidence and importance (§17, §18) ─────────────────────────────────────


def test_confidence_bands_and_hedges() -> None:
    assert confidence_band(1.0).value == "CERTAIN"
    assert confidence_band(0.35).value == "LOW"
    assert hedge_for(1.0) == "you told me"
    assert "not sure" in hedge_for(0.3)


def test_importance_bands() -> None:
    assert importance_band(0.9).value == "CRITICAL"
    assert importance_band(0.1).value == "TRIVIAL"


async def test_prompt_line_carries_the_hedge(memories, user) -> None:
    """§17 — a low-confidence inference must never read as fact."""
    low = await memories.create(user.id, draft("Might prefer tabs", confidence=0.3))
    line = MemoryService.to_prompt_line(low.memory)
    assert "not sure" in line

    high = await memories.create(
        user.id,
        draft("Definitely prefers spaces", subject="indentation", confidence=1.0),
    )
    assert "you told me" in MemoryService.to_prompt_line(high.memory)


async def test_external_memories_are_marked_in_the_prompt(memories, user) -> None:
    outcome = await memories.create(
        user.id, draft("From a PDF", source=MemorySource.DOCUMENT)
    )
    assert outcome.memory.tainted is True
    assert "external document" in MemoryService.to_prompt_line(outcome.memory)


# ── expiry / working memory ──────────────────────────────────────────────────


async def test_expired_memory_is_excluded(memories, user) -> None:
    await memories.create(
        user.id, draft("Stale", expires_at=utcnow() - timedelta(hours=1))
    )
    assert await memories.search(user.id, MemoryFilter()) == []
    assert len(await memories.search(user.id, MemoryFilter(include_expired=True))) == 1


async def test_working_memory_expires(memories, user) -> None:
    outcome = await memories.remember_working(
        user.id, "Current build is broken", ttl_seconds=3600
    )
    assert outcome.memory.expires_at is not None
    assert "working" in outcome.memory.tags


async def test_expire_due_archives(memories, user) -> None:
    await memories.create(
        user.id, draft("Old", expires_at=utcnow() - timedelta(seconds=1))
    )
    assert await memories.expire_due(user.id) == 1


# ── bulk forget (§35) ────────────────────────────────────────────────────────


async def test_bulk_forget_requires_an_explicit_scope(memories, user) -> None:
    await memories.create(user.id, draft("Something"))
    with pytest.raises(ValidationError):
        await memories.forget_scope(user.id)


async def test_forget_project_scope(memories, session, user) -> None:
    from jarvis.memory.projects import ProjectService

    project = await ProjectService(session).create(user.id, name="Doomed")
    await memories.create(user.id, draft("Global fact", subject="global"))
    await memories.create(
        user.id, draft("Project fact", subject="proj", project_id=project.id)
    )

    assert await memories.forget_scope(user.id, project_id=project.id) == 1
    # Archived, not erased — so it is gone from what JARVIS knows, and still
    # visible (and restorable) when asked for by status.
    active = await memories.search(user.id, MemoryFilter(statuses=[MemoryStatus.ACTIVE]))
    assert [m.content for m in active] == ["Global fact"]
    archived = await memories.search(
        user.id, MemoryFilter(statuses=[MemoryStatus.ARCHIVED])
    )
    assert [m.content for m in archived] == ["Project fact"]


async def test_forget_all_is_possible(memories, user) -> None:
    """§35 — the system must never make memory impossible to remove."""
    for i in range(3):
        await memories.create(user.id, draft(f"Fact {i}", subject=f"s{i}"))
    assert await memories.forget_scope(user.id, all_memories=True) == 3
    assert await memories.search(
        user.id, MemoryFilter(statuses=[MemoryStatus.ACTIVE])
    ) == []

    # And hard forget really erases.
    assert await memories.forget_scope(user.id, all_memories=True, hard=True) == 3
    tombstones = await memories.search(
        user.id, MemoryFilter(statuses=[MemoryStatus.DELETED])
    )
    assert all(t.content == "" for t in tombstones)


# ── secrets (§34) ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "text",
    [
        "my password is Xk8$mQ2vL9pR",
        "api key sk-ant-api03-AAAABBBBCCCCDDDDEEEE1234",
        "-----BEGIN RSA PRIVATE KEY-----",
        "card 4111 1111 1111 1111",
        "postgres://user:hunter2@localhost/db",
        "token eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abcdefghijklmnop",
    ],
)
def test_guard_blocks_credentials(text: str) -> None:
    assert guard.inspect(text).blocked, text


@pytest.mark.parametrize(
    "text",
    [
        "The user prefers dark mode",
        "My password is hard to remember these days",
        "The API key lives in the .env file, not here",
        "Order number 1234567890123456",
        "Project X targets Unreal Engine 5.8",
    ],
)
def test_guard_allows_ordinary_text(text: str) -> None:
    assert not guard.inspect(text).blocked, text


async def test_secret_is_refused_at_the_service(memories, user) -> None:
    outcome = await memories.create(
        user.id, draft("my password is Xk8$mQ2vL9pR7z")
    )
    assert outcome.action == "refused"
    assert outcome.memory is None
    assert await memories.search(user.id, MemoryFilter()) == []


async def test_refusal_message_never_echoes_the_secret(memories, user) -> None:
    outcome = await memories.create(user.id, draft("my password is Xk8$mQ2vL9pR7z"))
    assert "Xk8" not in outcome.detail


async def test_secret_is_refused_on_edit(memories, user) -> None:
    outcome = await memories.create(user.id, draft("Harmless"))
    with pytest.raises(SecretInMemoryError):
        await memories.update(outcome.memory.id, content="password is Xk8$mQ2vL9pR7z")


async def test_user_can_override_the_guard_explicitly(memories, user) -> None:
    """A prohibition nobody can override is one they disable entirely."""
    outcome = await memories.create(
        user.id, draft("my password is Xk8$mQ2vL9pR7z"), allow_sensitive=True
    )
    assert outcome.action == "created"


# ── isolation ────────────────────────────────────────────────────────────────


async def test_another_users_memory_is_not_found(memories, session, user) -> None:
    from jarvis.db.models import User

    other = User(name="someone-else")
    session.add(other)
    await session.flush()

    outcome = await memories.create(other.id, draft("Their private fact"))
    with pytest.raises(NotFoundError):
        await memories.owned(outcome.memory.id, user.id)


async def test_search_is_scoped_to_the_owner(memories, session, user) -> None:
    from jarvis.db.models import User

    other = User(name="other")
    session.add(other)
    await session.flush()

    await memories.create(other.id, draft("Their fact"))
    await memories.create(user.id, draft("My fact", subject="mine"))

    mine = await memories.search(user.id, MemoryFilter())
    assert [m.content for m in mine] == ["My fact"]


# ── vectors ──────────────────────────────────────────────────────────────────


def test_vector_pack_round_trip() -> None:
    blob, norm, dim = pack_vector([3.0, 4.0])
    assert dim == 2
    assert norm == pytest.approx(5.0)
    # Stored normalised, so cosine is a plain dot product.
    assert unpack_vector(blob).tolist() == pytest.approx([0.6, 0.8])


def test_zero_vector_is_storable_and_matches_nothing() -> None:
    blob, norm, _ = pack_vector([0.0, 0.0])
    assert norm == 0.0
    assert unpack_vector(blob).tolist() == [0.0, 0.0]


async def test_empty_prefilter_means_no_candidates(session, embeddings) -> None:
    """An empty filter must mean "nothing", never "everything"."""
    index = SqliteVectorIndex(session)
    hits = await index.search(
        owner_kind=EmbeddingOwner.MEMORY,
        model=embeddings.info.model,
        query=await embeddings.embed_one("anything"),
        owner_ids=[],
    )
    assert hits == []


async def test_vectors_from_another_model_are_not_matched(
    memories, session, user, embeddings
) -> None:
    """Vectors from different models are not comparable."""
    await memories.create(user.id, draft("Something embedded"))
    hits = await SqliteVectorIndex(session).search(
        owner_kind=EmbeddingOwner.MEMORY,
        model="some-other-model",
        query=await embeddings.embed_one("something embedded"),
    )
    assert hits == []


def test_lexical_provider_declares_itself_non_semantic(embeddings) -> None:
    """The honesty requirement, as a test."""
    assert embeddings.info.semantic is False
    assert "lexical" in embeddings.info.description.lower()


async def test_lexical_embeddings_are_deterministic(embeddings) -> None:
    first = await embeddings.embed_one("the user prefers dark mode")
    second = await embeddings.embed_one("the user prefers dark mode")
    assert first == second


# ── subject derivation ───────────────────────────────────────────────────────


def test_subject_is_word_order_independent() -> None:
    assert normalise_subject("dark mode preference") == normalise_subject(
        "preference mode dark"
    )


def test_subject_drops_stopwords() -> None:
    assert "the" not in normalise_subject("The user prefers the dark mode").split()


async def test_curated_subject_survives_an_edit(memories, user) -> None:
    """An edit must not move a memory out of its own dedup group."""
    outcome = await memories.create(
        user.id, draft("Prefers dark mode", subject="interface theme preference")
    )
    await memories.update(outcome.memory.id, content="Prefers a light theme")
    assert (await memories.get(outcome.memory.id)).subject == "interface theme preference"


async def test_derived_subject_follows_the_content(memories, user) -> None:
    outcome = await memories.create(user.id, draft("Uses Unreal Engine"))
    original = outcome.memory.subject
    await memories.update(outcome.memory.id, content="Uses Godot instead")
    assert (await memories.get(outcome.memory.id)).subject != original
