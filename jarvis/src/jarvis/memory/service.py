"""Memory service — the only way memories are created, changed, or removed.

Everything in §31 lands here. The service owns four things that must not be
separable from a write, because a caller that can skip any of them produces a
memory the rest of the system cannot reason about:

1. **The secret guard.** Checked on every write path (§34).
2. **The embedding.** Written in the same transaction as the row, so a memory
   that exists is a memory that can be found.
3. **Deduplication and contradiction handling.** Run *before* insert, so the
   store converges instead of accumulating near-copies (§15, §16).
4. **A revision record.** Every state change is logged, so corrections are
   auditable and reversible (§11).

## The subject key

Dedup and contradiction both hinge on ``Memory.subject`` — a short normalised
phrase naming what the memory is *about*, not what it says. "interface theme
preference" is a subject; "the user prefers dark mode" is content.

This exists because similarity cannot distinguish agreement from disagreement.
"I prefer dark mode" and "I no longer prefer dark mode" sit almost on top of
each other in any embedding space and mean opposite things, while "I like dark
interfaces" and "dark mode please" are far apart in a lexical space and mean
the same thing. Subject plus similarity separates the cases that similarity
alone cannot:

* same subject, similar content  → duplicate: merge, keep the stronger
* same subject, dissimilar content → conflict: supersede, keep the newer
* different subject → unrelated, whatever the similarity says
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Sequence

from sqlalchemy import Select, Text, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from jarvis.db.base import utcnow
from jarvis.db.models import (
    Embedding,
    EmbeddingOwner,
    Memory,
    MemoryLink,
    MemoryRevision,
    Project,
)
from jarvis.errors import NotFoundError, ValidationError
from jarvis.knowledge.types import SourceRef
from jarvis.logging import get_logger
from jarvis.memory import guard
from jarvis.memory.types import (
    DEFAULT_IMPORTANCE,
    MemoryRelation,
    MemorySource,
    MemoryStatus,
    MemoryType,
    RevisionKind,
    confidence_band,
    hedge_for,
    importance_band,
)
from jarvis.memory.vectors import SqliteVectorIndex
from jarvis.providers.embeddings import EmbeddingProvider

log = get_logger(__name__)

_STOPWORDS = frozenset(
    """a an the is are was were be been being do does did have has had i me my mine you
    your yours he she it we they them his her its our their this that these those of in
    on at to for with about from by as and or but if then than so very just really
    would could should will shall can may might must want like prefer""".split()
)
_WORD_RE = re.compile(r"[a-z0-9]+")

#: Metadata flag marking a subject the caller chose rather than one derived
#: from the content. Lives in ``meta`` rather than as a column because it is a
#: property of how the value arrived, not of the memory itself.
_SUBJECT_EXPLICIT = "_subject_explicit"


class SecretInMemoryError(ValidationError):
    """Raised when a write is refused by the guard. A distinct type so callers
    can tell "refused on principle" from "malformed"."""

    code = "memory_contains_secret"


@dataclass(slots=True)
class MemoryDraft:
    """A memory about to be written. Separate from the ORM row so the
    evaluator can produce candidates without touching the database."""

    content: str
    type: MemoryType = MemoryType.USER_FACT
    subject: str | None = None
    summary: str | None = None
    source: MemorySource = MemorySource.CONVERSATION
    source_ref: SourceRef | None = None
    confidence: float = 0.55
    importance: float | None = None
    tags: list[str] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)
    project_id: str | None = None
    conversation_id: str | None = None
    task_id: str | None = None
    expires_at: datetime | None = None
    pinned: bool = False
    tainted: bool = False
    status: MemoryStatus = MemoryStatus.ACTIVE

    def resolved_importance(self) -> float:
        if self.importance is not None:
            return self.importance
        return DEFAULT_IMPORTANCE.get(self.type, 0.5)


@dataclass(slots=True)
class MemoryFilter:
    types: list[MemoryType] | None = None
    statuses: list[MemoryStatus] | None = None
    sources: list[MemorySource] | None = None
    project_id: str | None = None
    conversation_id: str | None = None
    tags: list[str] | None = None
    search: str | None = None
    min_importance: float | None = None
    min_confidence: float | None = None
    created_after: datetime | None = None
    created_before: datetime | None = None
    include_expired: bool = False
    limit: int = 100
    offset: int = 0


@dataclass(slots=True)
class WriteOutcome:
    """What actually happened, so callers can tell the user the truth rather
    than always saying "remembered"."""

    memory: Memory | None
    action: str  # created | merged | superseded | duplicate_ignored | refused
    detail: str = ""
    previous_id: str | None = None

    @property
    def stored(self) -> bool:
        return self.memory is not None and self.action != "refused"


def normalise_subject(text: str) -> str:
    """Derive a subject key from free text.

    Crude on purpose: lowercase, drop stopwords, keep the first few content
    words, sort them so word order does not create two subjects for one topic.
    A model-generated subject is better and the evaluator supplies one when it
    can; this is the floor, used when a memory arrives without one.
    """
    words = [w for w in _WORD_RE.findall(text.lower()) if w not in _STOPWORDS]
    if not words:
        return ""
    return " ".join(sorted(words[:6]))


class MemoryService:
    def __init__(
        self,
        session: AsyncSession,
        *,
        embeddings: EmbeddingProvider | None = None,
        duplicate_threshold: float = 0.87,
    ) -> None:
        self.session = session
        self.embeddings = embeddings
        self.index = SqliteVectorIndex(session)
        self.duplicate_threshold = duplicate_threshold

    # ── create ───────────────────────────────────────────────────────────────

    async def create(
        self,
        user_id: str,
        draft: MemoryDraft,
        *,
        actor: str = "user",
        request_id: str | None = None,
        allow_sensitive: bool = False,
        reconcile: bool = True,
    ) -> WriteOutcome:
        """Store a memory, reconciling it against what is already known.

        ``reconcile=False`` skips dedup and contradiction handling. It exists
        for import (§37), where the incoming set is already internally
        consistent and re-reconciling would silently drop rows the user
        expected to get back.
        """
        content = draft.content.strip()
        if not content:
            raise ValidationError("A memory needs content")

        if not allow_sensitive:
            verdict = guard.inspect(content)
            if verdict.blocked:
                log.warning(
                    "memory_refused_secret", reason=verdict.reason, actor=actor
                )
                return WriteOutcome(None, "refused", verdict.detail)

        subject_explicit = bool(draft.subject and draft.subject.strip())
        subject = (draft.subject or normalise_subject(content)).strip().lower()
        vector = await self._embed(content, draft.summary)

        if reconcile:
            outcome = await self._reconcile(
                user_id, draft, subject, vector, actor=actor, request_id=request_id
            )
            if outcome is not None:
                return outcome

        memory = Memory(
            user_id=user_id,
            type=draft.type,
            status=draft.status,
            content=content,
            summary=draft.summary,
            subject=subject or None,
            source=draft.source,
            source_ref=draft.source_ref.to_dict() if draft.source_ref else None,
            tainted=draft.tainted or draft.source.is_external,
            confidence=_clamp(draft.confidence),
            importance=_clamp(draft.resolved_importance()),
            pinned=draft.pinned,
            project_id=draft.project_id,
            conversation_id=draft.conversation_id,
            task_id=draft.task_id,
            tags=list(draft.tags),
            meta={**draft.meta, _SUBJECT_EXPLICIT: subject_explicit},
            expires_at=draft.expires_at,
        )
        self.session.add(memory)
        await self.session.flush()

        if vector is not None:
            await self._store_vector(memory.id, vector)

        await self._revise(
            memory, RevisionKind.CREATED, actor=actor, request_id=request_id,
            changes={"content": {"to": content}, "type": {"to": draft.type.value}},
        )
        log.info(
            "memory_created", memory_id=memory.id, type=draft.type.value,
            subject=subject or None, confidence=memory.confidence,
            importance=memory.importance,
        )
        return WriteOutcome(memory, "created")

    # ── reconciliation ───────────────────────────────────────────────────────

    async def _reconcile(
        self,
        user_id: str,
        draft: MemoryDraft,
        subject: str,
        vector: Sequence[float] | None,
        *,
        actor: str,
        request_id: str | None,
    ) -> WriteOutcome | None:
        """Merge into, or supersede, an existing memory. ``None`` means insert.

        Episodic memories are exempt: a newer event does not make an older one
        wrong. "The build failed in March" stays true after April's succeeds,
        so treating them as contradictory would erase history.
        """
        if draft.type.is_episodic or not subject:
            return None

        candidates = await self._same_subject(user_id, subject, draft)
        if not candidates:
            return None

        similarity = await self._similarity_map(
            [c.id for c in candidates], vector
        )
        content = draft.content.strip()

        for existing in candidates:
            score = similarity.get(existing.id, 0.0)
            if score >= self.duplicate_threshold or _texts_equivalent(
                existing.content, content
            ):
                return await self._merge_into(
                    existing, draft, actor=actor, request_id=request_id, score=score
                )

        # Same subject, different content: the newer statement wins, but the
        # old one is kept as SUPERSEDED rather than deleted, because a wrong
        # supersession has to be reversible and "what did I used to think?" is
        # a legitimate question.
        newest = max(candidates, key=lambda m: m.updated_at or m.created_at)
        if newest.pinned:
            log.info("memory_conflict_pinned", memory_id=newest.id, subject=subject)
            return None
        return await self._supersede(
            newest, draft, subject, vector, actor=actor, request_id=request_id
        )

    async def _same_subject(
        self, user_id: str, subject: str, draft: MemoryDraft
    ) -> list[Memory]:
        stmt = select(Memory).where(
            Memory.user_id == user_id,
            Memory.subject == subject,
            Memory.status == MemoryStatus.ACTIVE,
            Memory.type == draft.type,
        )
        # A project fact and a personal fact can share wording without being
        # the same memory, so scope has to match too.
        stmt = stmt.where(
            Memory.project_id == draft.project_id
            if draft.project_id
            else Memory.project_id.is_(None)
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def _merge_into(
        self,
        existing: Memory,
        draft: MemoryDraft,
        *,
        actor: str,
        request_id: str | None,
        score: float,
    ) -> WriteOutcome:
        """Fold a duplicate into the memory that already exists.

        Confidence rises because independent restatement is evidence; it is
        capped below certainty so that repetition alone never manufactures the
        certainty that only an explicit instruction earns.
        """
        before = {
            "confidence": existing.confidence,
            "importance": existing.importance,
        }
        existing.confidence = _clamp(max(existing.confidence, draft.confidence) + 0.05,
                                     high=0.95)
        existing.importance = max(existing.importance, _clamp(draft.resolved_importance()))
        existing.tags = sorted(set(existing.tags) | set(draft.tags))
        if draft.confidence >= existing.confidence and len(draft.content) > len(
            existing.content
        ):
            # Prefer the fuller phrasing when the new one is at least as
            # trustworthy — more context is more useful six months later.
            existing.content = draft.content.strip()
        existing.revision += 1
        existing.updated_at = utcnow()
        await self.session.flush()

        await self._revise(
            existing, RevisionKind.MERGED, actor=actor, request_id=request_id,
            changes={
                "confidence": {"from": before["confidence"], "to": existing.confidence},
                "importance": {"from": before["importance"], "to": existing.importance},
            },
            note=f"Merged a restatement (similarity {score:.2f}).",
        )
        log.info("memory_merged", memory_id=existing.id, similarity=round(score, 3))
        return WriteOutcome(
            existing, "merged",
            "I already knew that — I have strengthened what I had.",
        )

    async def _supersede(
        self,
        old: Memory,
        draft: MemoryDraft,
        subject: str,
        vector: Sequence[float] | None,
        *,
        actor: str,
        request_id: str | None,
    ) -> WriteOutcome:
        replacement = Memory(
            user_id=old.user_id,
            type=draft.type,
            status=MemoryStatus.ACTIVE,
            content=draft.content.strip(),
            summary=draft.summary,
            subject=subject,
            source=draft.source,
            source_ref=draft.source_ref.to_dict() if draft.source_ref else None,
            tainted=draft.tainted or draft.source.is_external,
            confidence=_clamp(draft.confidence),
            importance=_clamp(max(draft.resolved_importance(), old.importance)),
            project_id=draft.project_id,
            conversation_id=draft.conversation_id,
            task_id=draft.task_id,
            tags=sorted(set(old.tags) | set(draft.tags)),
            meta={**draft.meta, _SUBJECT_EXPLICIT: bool(draft.subject or old.meta.get(
                _SUBJECT_EXPLICIT))},
            expires_at=draft.expires_at,
            revision=1,
        )
        self.session.add(replacement)
        await self.session.flush()

        if vector is not None:
            await self._store_vector(replacement.id, vector)

        old.status = MemoryStatus.SUPERSEDED
        old.superseded_by = replacement.id
        old.updated_at = utcnow()

        self.session.add(
            MemoryLink(
                from_memory_id=replacement.id,
                to_memory_id=old.id,
                relation=MemoryRelation.SUPERSEDES,
                note="Newer statement on the same subject.",
            )
        )
        await self.session.flush()

        await self._revise(
            old, RevisionKind.SUPERSEDED, actor=actor, request_id=request_id,
            changes={"status": {"from": MemoryStatus.ACTIVE.value,
                                "to": MemoryStatus.SUPERSEDED.value}},
            note=f"Replaced by {replacement.id}.",
        )
        await self._revise(
            replacement, RevisionKind.CREATED, actor=actor, request_id=request_id,
            changes={"content": {"from": old.content, "to": replacement.content}},
            note=f"Supersedes {old.id}.",
        )
        log.info(
            "memory_superseded", old_id=old.id, new_id=replacement.id, subject=subject
        )
        return WriteOutcome(
            replacement, "superseded",
            "That contradicts what I had — I have updated it and kept the old "
            "version in the history.",
            previous_id=old.id,
        )

    # ── read ─────────────────────────────────────────────────────────────────

    async def get(self, memory_id: str) -> Memory:
        memory = await self.session.get(Memory, memory_id)
        if memory is None:
            raise NotFoundError(f"Memory {memory_id} not found")
        return memory

    async def owned(self, memory_id: str, user_id: str) -> Memory:
        """Fetch with an ownership check, indistinguishable from absent.

        Returning 404 rather than 403 for someone else's memory is deliberate:
        403 confirms the id exists.
        """
        memory = await self.get(memory_id)
        if memory.user_id != user_id:
            raise NotFoundError(f"Memory {memory_id} not found")
        return memory

    async def search(self, user_id: str, f: MemoryFilter) -> list[Memory]:
        """Structured search. Semantic ranking lives in
        :mod:`jarvis.memory.retrieval`; this is the filter half."""
        stmt = self._filtered(user_id, f).order_by(
            Memory.importance.desc(), Memory.updated_at.desc()
        )
        stmt = stmt.limit(min(f.limit, 500)).offset(max(f.offset, 0))
        return list((await self.session.execute(stmt)).scalars().all())

    async def count(self, user_id: str, f: MemoryFilter) -> int:
        stmt = select(func.count()).select_from(self._filtered(user_id, f).subquery())
        return int((await self.session.execute(stmt)).scalar_one())

    def _filtered(self, user_id: str, f: MemoryFilter) -> Select[Any]:
        stmt = select(Memory).where(Memory.user_id == user_id)

        if f.statuses:
            stmt = stmt.where(Memory.status.in_(f.statuses))
        else:
            # Tombstones hold no content. Superseded rows are history: their
            # replacement is active and already listed, and showing both would
            # put two contradictory memories side by side as though equally
            # current — the thing §16 exists to prevent. Both remain reachable
            # by asking for them explicitly, and from a memory's own history.
            stmt = stmt.where(
                Memory.status.not_in([MemoryStatus.DELETED, MemoryStatus.SUPERSEDED])
            )
        if f.types:
            stmt = stmt.where(Memory.type.in_(f.types))
        if f.sources:
            stmt = stmt.where(Memory.source.in_(f.sources))
        if f.project_id:
            stmt = stmt.where(Memory.project_id == f.project_id)
        if f.conversation_id:
            stmt = stmt.where(Memory.conversation_id == f.conversation_id)
        if f.min_importance is not None:
            stmt = stmt.where(Memory.importance >= f.min_importance)
        if f.min_confidence is not None:
            stmt = stmt.where(Memory.confidence >= f.min_confidence)
        if f.created_after:
            stmt = stmt.where(Memory.created_at >= f.created_after)
        if f.created_before:
            stmt = stmt.where(Memory.created_at <= f.created_before)
        if f.search:
            like = f"%{f.search.strip()}%"
            stmt = stmt.where(
                or_(
                    Memory.content.ilike(like),
                    Memory.summary.ilike(like),
                    Memory.subject.ilike(like),
                )
            )
        if f.tags:
            # SQLite has no array containment; JSON is stored as text, and a
            # quoted substring match is exact enough for tag tokens.
            for tag in f.tags:
                stmt = stmt.where(
                    func.lower(Memory.tags.cast(Text)).contains(f'"{tag.lower()}"')
                )
        if not f.include_expired:
            stmt = stmt.where(
                or_(Memory.expires_at.is_(None), Memory.expires_at > utcnow())
            )
        return stmt

    async def related(self, memory_id: str, limit: int = 10) -> list[tuple[Memory, MemoryRelation]]:
        stmt = select(MemoryLink).where(
            or_(
                MemoryLink.from_memory_id == memory_id,
                MemoryLink.to_memory_id == memory_id,
            )
        ).limit(limit)
        links = list((await self.session.execute(stmt)).scalars().all())

        out: list[tuple[Memory, MemoryRelation]] = []
        for link in links:
            other_id = (
                link.to_memory_id
                if link.from_memory_id == memory_id
                else link.from_memory_id
            )
            other = await self.session.get(Memory, other_id)
            if other is not None:
                out.append((other, link.relation))
        return out

    async def history(self, memory_id: str, limit: int = 50) -> list[MemoryRevision]:
        stmt = (
            select(MemoryRevision)
            .where(MemoryRevision.memory_id == memory_id)
            .order_by(MemoryRevision.created_at.desc())
            .limit(limit)
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def mark_accessed(self, memories: Sequence[Memory]) -> None:
        """Record retrieval. Not a revision row — retrieval happens on every
        request and would bury the history that matters under noise."""
        now = utcnow()
        for memory in memories:
            memory.last_accessed_at = now
            memory.access_count += 1

    # ── update ───────────────────────────────────────────────────────────────

    async def update(
        self,
        memory_id: str,
        *,
        actor: str = "user",
        request_id: str | None = None,
        note: str | None = None,
        allow_sensitive: bool = False,
        **changes: Any,
    ) -> Memory:
        memory = await self.get(memory_id)
        if memory.status is MemoryStatus.DELETED:
            raise ValidationError("That memory was deleted; it cannot be edited.")

        if "content" in changes and changes["content"] and not allow_sensitive:
            verdict = guard.inspect(str(changes["content"]))
            if verdict.blocked:
                raise SecretInMemoryError(
                    f"Refused memory edit: {verdict.reason}",
                    user_message=verdict.detail,
                )

        applied: dict[str, Any] = {}
        for field_name, value in changes.items():
            if value is None or not hasattr(memory, field_name):
                continue
            before = getattr(memory, field_name)
            if isinstance(before, (MemoryType, MemoryStatus, MemorySource)):
                before = before.value
            if before == value:
                continue
            setattr(memory, field_name, value)
            applied[field_name] = {
                "from": before,
                "to": value.value if hasattr(value, "value") else value,
            }

        if not applied:
            return memory

        if "subject" in applied:
            memory.meta = {**memory.meta, _SUBJECT_EXPLICIT: True}

        memory.revision += 1
        memory.updated_at = utcnow()

        if "content" in applied:
            # An auto-derived subject follows the content; a curated one does
            # not. Recomputing a curated subject on every edit would silently
            # move the memory out of its own dedup group, which is the group
            # that makes duplicate and contradiction detection work at all.
            if "subject" not in applied and not memory.meta.get(_SUBJECT_EXPLICIT):
                memory.subject = normalise_subject(memory.content) or None
            vector = await self._embed(memory.content, memory.summary)
            if vector is not None:
                await self._store_vector(memory.id, vector)

        await self.session.flush()
        kind = RevisionKind.CORRECTED if "content" in applied else RevisionKind.UPDATED
        await self._revise(memory, kind, actor=actor, request_id=request_id,
                           changes=applied, note=note)
        log.info("memory_updated", memory_id=memory.id, fields=sorted(applied))
        return memory

    async def confirm(
        self, memory_id: str, *, approved: bool, actor: str = "user",
        request_id: str | None = None,
    ) -> Memory:
        """Resolve a PROPOSED memory (§14)."""
        memory = await self.get(memory_id)
        if memory.status is not MemoryStatus.PROPOSED:
            raise ValidationError("That memory is not awaiting confirmation.")

        memory.status = MemoryStatus.ACTIVE if approved else MemoryStatus.REJECTED
        if approved:
            # The user saying yes is a stronger signal than the inference was.
            memory.confidence = max(memory.confidence, 0.9)
        memory.updated_at = utcnow()
        await self.session.flush()
        await self._revise(
            memory,
            RevisionKind.CONFIRMED if approved else RevisionKind.ARCHIVED,
            actor=actor, request_id=request_id,
            changes={"status": {"to": memory.status.value}},
        )
        return memory

    # ── removal ──────────────────────────────────────────────────────────────

    async def archive(
        self, memory_id: str, *, actor: str = "user", request_id: str | None = None
    ) -> Memory:
        """Soft delete. Reversible, and the default for "forget that"."""
        memory = await self.get(memory_id)
        memory.status = MemoryStatus.ARCHIVED
        memory.updated_at = utcnow()
        await self.session.flush()
        await self._revise(memory, RevisionKind.ARCHIVED, actor=actor,
                           request_id=request_id)
        log.info("memory_archived", memory_id=memory_id)
        return memory

    async def restore(
        self, memory_id: str, *, actor: str = "user", request_id: str | None = None
    ) -> Memory:
        memory = await self.get(memory_id)
        if memory.status is MemoryStatus.DELETED:
            raise ValidationError("A deleted memory cannot be restored.")
        memory.status = MemoryStatus.ACTIVE
        memory.updated_at = utcnow()
        await self.session.flush()
        await self._revise(memory, RevisionKind.RESTORED, actor=actor,
                           request_id=request_id)
        return memory

    async def delete(
        self, memory_id: str, *, actor: str = "user", request_id: str | None = None
    ) -> None:
        """Hard delete: content erased, tombstone kept.

        The tombstone holds an id and a timestamp and no content. It exists so
        that "was there something here?" is answerable and so an import cannot
        silently resurrect what the user deleted. §35 is satisfied — the
        content is genuinely gone, including its vector.
        """
        memory = await self.get(memory_id)
        await self.index.delete(owner_kind=EmbeddingOwner.MEMORY, owner_id=memory.id)

        memory.status = MemoryStatus.DELETED
        memory.content = ""
        memory.summary = None
        memory.subject = None
        memory.tags = []
        memory.meta = {}
        memory.source_ref = None
        memory.updated_at = utcnow()
        await self.session.flush()
        await self._revise(memory, RevisionKind.DELETED, actor=actor,
                           request_id=request_id, note="Content erased.")
        log.info("memory_deleted", memory_id=memory_id)

    async def forget_scope(
        self,
        user_id: str,
        *,
        project_id: str | None = None,
        all_memories: bool = False,
        hard: bool = False,
        actor: str = "user",
    ) -> int:
        """Bulk forget — "forget everything about Project X" (§13, §35).

        Requires an explicit scope. There is no code path where an empty filter
        means "everything"; ``all_memories`` has to be asked for by name.
        """
        if not all_memories and project_id is None:
            raise ValidationError(
                "Bulk forget needs a scope: a project, or all_memories=True."
            )

        stmt = select(Memory).where(
            Memory.user_id == user_id, Memory.status != MemoryStatus.DELETED
        )
        if project_id is not None:
            stmt = stmt.where(Memory.project_id == project_id)

        rows = list((await self.session.execute(stmt)).scalars().all())
        for memory in rows:
            if hard:
                await self.delete(memory.id, actor=actor)
            else:
                await self.archive(memory.id, actor=actor)

        log.info(
            "memory_bulk_forget", count=len(rows), project_id=project_id,
            all_memories=all_memories, hard=hard,
        )
        return len(rows)

    async def expire_due(self, user_id: str) -> int:
        """Archive memories past their expiry. Working memory uses this."""
        stmt = select(Memory).where(
            Memory.user_id == user_id,
            Memory.status == MemoryStatus.ACTIVE,
            Memory.expires_at.is_not(None),
            Memory.expires_at <= utcnow(),
        )
        rows = list((await self.session.execute(stmt)).scalars().all())
        for memory in rows:
            memory.status = MemoryStatus.ARCHIVED
            await self._revise(memory, RevisionKind.ARCHIVED, actor="system",
                               note="Expired.")
        return len(rows)

    # ── working memory ───────────────────────────────────────────────────────

    async def remember_working(
        self,
        user_id: str,
        content: str,
        *,
        task_id: str | None = None,
        conversation_id: str | None = None,
        ttl_seconds: int = 86_400,
        actor: str = "orchestrator",
    ) -> WriteOutcome:
        """Scratch memory for an in-flight workflow (§8).

        Same table, an expiry, and low importance — rather than a separate
        store. Working memory that outlives its task is the failure mode worth
        preventing, and an expiry prevents it; a second table would only add a
        second thing to query at retrieval time.
        """
        return await self.create(
            user_id,
            MemoryDraft(
                content=content,
                type=MemoryType.PROJECT_STATE,
                source=MemorySource.AGENT,
                confidence=0.7,
                importance=0.3,
                task_id=task_id,
                conversation_id=conversation_id,
                expires_at=utcnow() + timedelta(seconds=ttl_seconds),
                tags=["working"],
            ),
            actor=actor,
            reconcile=False,
        )

    # ── internals ────────────────────────────────────────────────────────────

    async def _embed(self, content: str, summary: str | None) -> list[float] | None:
        if self.embeddings is None:
            return None
        text = f"{summary}\n{content}" if summary else content
        try:
            return await self.embeddings.embed_one(text)
        except Exception as exc:
            # A memory without a vector is still a memory: findable by keyword
            # and structured filter, just not by similarity. Losing the write
            # because the embedding endpoint is down would be worse.
            log.warning("memory_embedding_failed", error=str(exc))
            return None

    async def _store_vector(self, memory_id: str, vector: Sequence[float]) -> None:
        assert self.embeddings is not None
        await self.index.upsert(
            owner_kind=EmbeddingOwner.MEMORY,
            owner_id=memory_id,
            model=self.embeddings.info.model,
            vector=vector,
        )

    async def _similarity_map(
        self, memory_ids: list[str], vector: Sequence[float] | None
    ) -> dict[str, float]:
        if vector is None or not memory_ids or self.embeddings is None:
            return {}
        hits = await self.index.search(
            owner_kind=EmbeddingOwner.MEMORY,
            model=self.embeddings.info.model,
            query=vector,
            top_k=len(memory_ids),
            owner_ids=memory_ids,
        )
        return {hit.owner_id: hit.score for hit in hits}

    async def _revise(
        self,
        memory: Memory,
        kind: RevisionKind,
        *,
        actor: str,
        request_id: str | None = None,
        changes: dict[str, Any] | None = None,
        note: str | None = None,
    ) -> None:
        self.session.add(
            MemoryRevision(
                memory_id=memory.id,
                kind=kind,
                actor=actor,
                changes=changes or {},
                note=note,
                request_id=request_id,
            )
        )
        await self.session.flush()

    # ── presentation ─────────────────────────────────────────────────────────

    @staticmethod
    def to_dict(memory: Memory, *, score: dict[str, float] | None = None) -> dict[str, Any]:
        source_ref = SourceRef.from_dict(memory.source_ref)
        return {
            "id": memory.id,
            "type": memory.type.value if hasattr(memory.type, "value") else memory.type,
            "status": memory.status.value
            if hasattr(memory.status, "value")
            else memory.status,
            "content": memory.content,
            "summary": memory.summary,
            "subject": memory.subject,
            "source": memory.source.value
            if hasattr(memory.source, "value")
            else memory.source,
            "provenance": source_ref.describe() if source_ref else None,
            "source_ref": memory.source_ref,
            "tainted": memory.tainted,
            "confidence": round(memory.confidence, 3),
            "confidence_band": confidence_band(memory.confidence).value,
            "importance": round(memory.importance, 3),
            "importance_band": importance_band(memory.importance).value,
            "pinned": memory.pinned,
            "project_id": memory.project_id,
            "conversation_id": memory.conversation_id,
            "task_id": memory.task_id,
            "tags": memory.tags,
            "metadata": {k: v for k, v in memory.meta.items()
                         if not k.startswith("_")},
            "revision": memory.revision,
            "access_count": memory.access_count,
            "superseded_by": memory.superseded_by,
            "created_at": memory.created_at.isoformat() if memory.created_at else None,
            "updated_at": memory.updated_at.isoformat() if memory.updated_at else None,
            "last_accessed_at": (
                memory.last_accessed_at.isoformat() if memory.last_accessed_at else None
            ),
            "expires_at": memory.expires_at.isoformat() if memory.expires_at else None,
            "score": score,
        }

    @staticmethod
    def to_prompt_line(memory: Memory) -> str:
        """One memory as the model should see it.

        The hedge is included rather than left to the model's judgement: §17
        requires that a low-confidence inference is never presented as fact,
        and supplying the wording is more reliable than hoping for calibration.
        """
        hedge = hedge_for(memory.confidence)
        text = memory.summary or memory.content
        marker = " [from an external document]" if memory.tainted else ""
        return f"- ({hedge}) {text}{marker}"


def _clamp(value: float, *, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, float(value)))


def _texts_equivalent(a: str, b: str) -> bool:
    """Same content ignoring case, punctuation and word order.

    Catches the exact restatement that an embedding threshold might just miss,
    and costs nothing.
    """
    return _bag(a) == _bag(b)


def _bag(text: str) -> frozenset[str]:
    return frozenset(w for w in _WORD_RE.findall(text.lower()) if w not in _STOPWORDS)


def _naive_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
