"""Memory retrieval and ranking (§19, §20).

Retrieval answers one question: *which few memories does this request need?*
The emphasis is on **few**. §19 forbids injecting the store into every request,
and the reason is not only cost — a model handed forty marginally-related
memories reasons worse than one handed four relevant ones, because the
irrelevant ones are indistinguishable from context that matters.

## The three signals

Structured, semantic, and keyword search each fail in a way the others cover:

* **Structured** filtering (project, type, tags, recency) is exact and blind.
  It cannot tell you whether a memory is *about* what you asked.
* **Semantic** similarity understands paraphrase and fails on proper nouns —
  embeddings routinely place two unrelated project code names close together
  because neither means anything.
* **Keyword** overlap nails proper nouns and fails on synonyms.

"Project X" is the case that proves the point: it is exactly the query where
semantic similarity is weakest and exact term matching is strongest.

## Weighting, and the honest part

The weights depend on whether the embedding provider is actually semantic. With
a real embedding model, similarity leads. With the lexical fallback, the vector
score is a second opinion on the same evidence keyword search already provides,
so it is weighted down and keyword search leads instead. Retrieval degrades
predictably rather than pretending.

Importance, recency and confidence are **modifiers, not signals**. They scale a
match that already exists; they can never promote an unrelated memory. That
ordering matters: a highly important memory about something else is still about
something else.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Sequence

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from jarvis.db.base import utcnow
from jarvis.db.models import EmbeddingOwner, Memory
from jarvis.logging import get_logger
from jarvis.memory.service import MemoryService
from jarvis.memory.types import MemoryScore, MemoryStatus, MemoryType
from jarvis.memory.vectors import SqliteVectorIndex
from jarvis.providers.embeddings import EmbeddingProvider

log = get_logger(__name__)

_WORD_RE = re.compile(r"[a-z0-9]+")
_QUERY_STOPWORDS = frozenset(
    """a an the is are was were do does did what which who whom whose when where why how
    can could would should will shall may might must i me my you your we our it its of in
    on at to for with about from by as and or but if then so tell show me know remember
    about please""".split()
)

#: Half-life for the recency modifier. Ninety days is a compromise: short
#: enough that "current state" beats last quarter's, long enough that a
#: preference stated in spring still counts in autumn.
_RECENCY_HALF_LIFE_DAYS = 90.0


@dataclass(slots=True)
class RetrievalWeights:
    semantic: float = 0.55
    keyword: float = 0.30
    structured: float = 0.15
    #: Modifier strengths. Applied multiplicatively to the match score.
    importance: float = 0.35
    recency: float = 0.15
    confidence: float = 0.20

    @classmethod
    def for_provider(cls, semantic_embeddings: bool) -> "RetrievalWeights":
        if semantic_embeddings:
            return cls()
        # Lexical vectors and keyword overlap measure nearly the same thing.
        # Leaning on keyword search keeps the ranking interpretable rather
        # than double-counting one signal.
        return cls(semantic=0.25, keyword=0.60, structured=0.15)


@dataclass(slots=True)
class RetrievalQuery:
    text: str
    user_id: str
    project_id: str | None = None
    conversation_id: str | None = None
    types: list[MemoryType] | None = None
    limit: int = 8
    max_chars: int = 6_000
    min_score: float = 0.12
    min_similarity: float = 0.25
    #: Considered before ranking. Larger costs a dot product each and buys
    #: recall; the cost is linear and small.
    candidate_pool: int = 60


@dataclass(slots=True)
class RetrievedMemory:
    memory: Memory
    score: MemoryScore

    @property
    def id(self) -> str:
        return self.memory.id


@dataclass(slots=True)
class RetrievalResult:
    memories: list[RetrievedMemory] = field(default_factory=list)
    #: True if any retrieved memory came from untrusted content. Propagated to
    #: the permission engine, which escalates non-read capabilities (§42).
    tainted: bool = False
    considered: int = 0
    semantic_used: bool = False
    duration_ms: float = 0.0

    def as_prompt_block(self, max_chars: int = 6_000) -> str:
        if not self.memories:
            return ""
        lines: list[str] = []
        total = 0
        for item in self.memories:
            line = MemoryService.to_prompt_line(item.memory)
            if total + len(line) > max_chars:
                break
            lines.append(line)
            total += len(line)
        return "\n".join(lines)

    def describe(self) -> list[dict[str, object]]:
        return [
            {
                "id": item.memory.id,
                "type": item.memory.type.value
                if hasattr(item.memory.type, "value")
                else item.memory.type,
                "content": item.memory.summary or item.memory.content,
                "score": item.score.describe(),
            }
            for item in self.memories
        ]


def tokenise(text: str) -> set[str]:
    return {w for w in _WORD_RE.findall(text.lower()) if w not in _QUERY_STOPWORDS}


class MemoryRetriever:
    def __init__(
        self,
        session: AsyncSession,
        *,
        embeddings: EmbeddingProvider | None = None,
        weights: RetrievalWeights | None = None,
    ) -> None:
        self.session = session
        self.embeddings = embeddings
        self.index = SqliteVectorIndex(session)
        semantic = bool(embeddings and embeddings.info.semantic)
        self.weights = weights or RetrievalWeights.for_provider(semantic)
        self.semantic = semantic

    async def retrieve(self, query: RetrievalQuery) -> RetrievalResult:
        from jarvis.logging import timed

        with timed() as clock:
            result = await self._retrieve(query)
        result.duration_ms = clock.duration_ms
        log.debug(
            "memory_retrieved",
            returned=len(result.memories),
            considered=result.considered,
            duration_ms=round(result.duration_ms, 2),
            semantic=result.semantic_used,
        )
        return result

    async def _retrieve(self, query: RetrievalQuery) -> RetrievalResult:
        terms = tokenise(query.text)
        candidates = await self._candidates(query)
        if not candidates:
            return RetrievalResult(semantic_used=self.semantic)

        by_id = {m.id: m for m in candidates}
        similarity = await self._similarity(query, list(by_id))

        scored: list[RetrievedMemory] = []
        for memory in candidates:
            score = self._score(memory, terms, similarity.get(memory.id, 0.0), query)
            if score.total >= query.min_score:
                scored.append(RetrievedMemory(memory=memory, score=score))

        scored.sort(key=lambda r: r.score.total, reverse=True)
        selected = scored[: query.limit]

        return RetrievalResult(
            memories=selected,
            tainted=any(r.memory.tainted for r in selected),
            considered=len(candidates),
            semantic_used=self.semantic and bool(similarity),
        )

    async def _candidates(self, query: RetrievalQuery) -> list[Memory]:
        """Structured pre-filter.

        Everything active and unexpired that is either global or belongs to
        this project. Ordered by importance so that if the pool cap truncates,
        it truncates the least important tail rather than an arbitrary one.
        """
        stmt = select(Memory).where(
            Memory.user_id == query.user_id,
            Memory.status == MemoryStatus.ACTIVE,
            or_(Memory.expires_at.is_(None), Memory.expires_at > utcnow()),
        )
        if query.types:
            stmt = stmt.where(Memory.type.in_(query.types))
        if query.project_id:
            # Project-scoped *and* global: a preference for dark mode still
            # applies while working on a project.
            stmt = stmt.where(
                or_(
                    Memory.project_id == query.project_id,
                    Memory.project_id.is_(None),
                )
            )
        stmt = stmt.order_by(Memory.importance.desc()).limit(query.candidate_pool)
        return list((await self.session.execute(stmt)).scalars().all())

    async def _similarity(
        self, query: RetrievalQuery, memory_ids: list[str]
    ) -> dict[str, float]:
        if self.embeddings is None or not memory_ids:
            return {}
        try:
            vector = await self.embeddings.embed_one(query.text)
        except Exception as exc:
            # Retrieval degrades to keyword plus structured rather than
            # failing the request. A turn without memory is worse than a turn
            # with imperfect memory, and far better than no answer at all.
            log.warning("retrieval_embedding_failed", error=str(exc))
            return {}

        hits = await self.index.search(
            owner_kind=EmbeddingOwner.MEMORY,
            model=self.embeddings.info.model,
            query=vector,
            top_k=len(memory_ids),
            owner_ids=memory_ids,
            min_score=query.min_similarity,
        )
        return {hit.owner_id: hit.score for hit in hits}

    def _score(
        self,
        memory: Memory,
        terms: set[str],
        similarity: float,
        query: RetrievalQuery,
    ) -> MemoryScore:
        keyword = self._keyword_overlap(memory, terms)
        structured = self._structured_affinity(memory, query)

        match = (
            self.weights.semantic * similarity
            + self.weights.keyword * keyword
            + self.weights.structured * structured
        )
        if match <= 0:
            return MemoryScore(semantic=similarity, keyword=keyword,
                               structured=structured, total=0.0)

        # Modifiers scale an existing match. Centred so an average memory is
        # unchanged: importance can lift a match by ~35% or cut it by as much,
        # and cannot conjure one from nothing.
        importance = 1.0 + self.weights.importance * (memory.importance - 0.5) * 2
        confidence = 1.0 + self.weights.confidence * (memory.confidence - 0.5) * 2
        recency = 1.0 + self.weights.recency * (self._recency(memory) - 0.5) * 2

        return MemoryScore(
            semantic=similarity,
            keyword=keyword,
            structured=structured,
            importance=memory.importance,
            recency=self._recency(memory),
            confidence=memory.confidence,
            total=match * importance * confidence * recency,
        )

    @staticmethod
    def _keyword_overlap(memory: Memory, terms: set[str]) -> float:
        """Fraction of query terms present, with a bonus for the subject.

        Scoring against the query's terms rather than the memory's means a long
        memory is not penalised for containing words the query did not use.
        """
        if not terms:
            return 0.0
        haystack = f"{memory.content} {memory.summary or ''} {memory.subject or ''} " \
                   f"{' '.join(memory.tags)}".lower()
        present = {t for t in terms if t in haystack}
        if not present:
            return 0.0

        overlap = len(present) / len(terms)
        if memory.subject:
            subject_terms = set(_WORD_RE.findall(memory.subject.lower()))
            if subject_terms & terms:
                # The subject naming a query term is a strong signal: it means
                # the memory is *about* what was asked, not merely mentions it.
                overlap = min(1.0, overlap + 0.25)
        return overlap

    @staticmethod
    def _structured_affinity(memory: Memory, query: RetrievalQuery) -> float:
        """Non-textual reasons this memory belongs to this request."""
        score = 0.0
        if query.project_id and memory.project_id == query.project_id:
            score += 0.6
        if query.conversation_id and memory.conversation_id == query.conversation_id:
            score += 0.2
        if memory.pinned:
            score += 0.5
        if memory.type in _ALWAYS_RELEVANT:
            # Standing preferences apply to everything by nature — that is what
            # makes them preferences rather than facts. The bonus is sized so
            # that structured affinity alone clears ``min_score``: "build me a
            # website" must surface "prefers dark interfaces" even though the
            # two share no vocabulary and a lexical vectoriser sees no
            # similarity between them.
            score += 0.8
        return min(1.0, score)

    @staticmethod
    def _recency(memory: Memory) -> float:
        """Exponential decay on last update, in [0, 1]."""
        stamp = memory.updated_at or memory.created_at
        if stamp is None:
            return 0.5
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        age_days = max(0.0, (datetime.now(timezone.utc) - stamp).total_seconds() / 86_400)
        return math.pow(0.5, age_days / _RECENCY_HALF_LIFE_DAYS)


_ALWAYS_RELEVANT = frozenset(
    {
        MemoryType.USER_PREFERENCE,
        MemoryType.SYSTEM_PREFERENCE,
        MemoryType.USER_ROUTINE,
    }
)
