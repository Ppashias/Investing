"""Knowledge service — the abstraction boundary (§22).

    JARVIS → KnowledgeService → KnowledgeProvider
                                 ├── internal (ingested documents)
                                 ├── local files
                                 ├── future: Obsidian
                                 ├── future: Notion
                                 └── future: web

Two responsibilities:

1. **Provider registry.** Register, list, and route by capability. Nothing
   outside this package needs to know which providers exist, and no caller
   imports a provider module.

2. **Retrieval over ingested knowledge.** Chunk-level hybrid search, the
   knowledge counterpart to :mod:`jarvis.memory.retrieval`.

Retrieval deliberately queries the *internal* store rather than fanning out to
every provider live. Ingested chunks are already embedded and local; asking a
remote provider on every turn would put network latency in the request path and
return unchunked results the ranker cannot compare. Providers feed the index
through ingestion; the index serves requests.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from jarvis.db.models import Document, DocumentChunk, EmbeddingOwner
from jarvis.errors import NotFoundError
from jarvis.knowledge.base import KnowledgeProvider, ProviderStatus
from jarvis.knowledge.providers import obsidian_contract
from jarvis.knowledge.types import (
    DocumentStatus,
    KnowledgeCapability,
    SourceKind,
    SourceRef,
)
from jarvis.logging import get_logger
from jarvis.memory.vectors import SqliteVectorIndex
from jarvis.providers.embeddings import EmbeddingProvider

log = get_logger(__name__)

_WORD_RE = re.compile(r"[a-z0-9]+")
_STOPWORDS = frozenset(
    """a an the is are was were be do does did what which who how can could would should
    of in on at to for with about from by as and or but if then i me my you your we our
    it its tell show me know""".split()
)


@dataclass(slots=True)
class KnowledgeHit:
    chunk: DocumentChunk
    document: Document
    score: float
    semantic: float = 0.0
    keyword: float = 0.0

    @property
    def provenance(self) -> SourceRef | None:
        return SourceRef.from_dict(self.document.source_ref)

    def citation(self) -> str:
        """How this fragment identifies itself to the model and the user."""
        parts = [self.document.title]
        if self.chunk.heading_path:
            parts.append(self.chunk.heading_path)
        return " › ".join(p for p in parts if p)


@dataclass(slots=True)
class KnowledgeResult:
    hits: list[KnowledgeHit] = field(default_factory=list)
    considered: int = 0
    semantic_used: bool = False
    duration_ms: float = 0.0

    def as_prompt_block(self, max_chars: int = 8_000) -> str:
        """Render for the system prompt.

        Each fragment is labelled with its source and explicitly marked as
        reference material. The label is the anti-injection measure that
        survives paraphrase: text that arrives inside a block announced as
        untrusted document content is much harder to mistake for an
        instruction than the same text pasted in bare.
        """
        if not self.hits:
            return ""
        lines: list[str] = []
        total = 0
        for hit in self.hits:
            body = hit.chunk.content.strip()
            entry = f"[{hit.citation()}]\n{body}"
            if total + len(entry) > max_chars:
                break
            lines.append(entry)
            total += len(entry)
        return "\n\n".join(lines)

    def describe(self) -> list[dict[str, object]]:
        return [
            {
                "chunk_id": hit.chunk.id,
                "document_id": hit.document.id,
                "citation": hit.citation(),
                "score": round(hit.score, 4),
                "semantic": round(hit.semantic, 4),
                "keyword": round(hit.keyword, 4),
            }
            for hit in self.hits
        ]


class KnowledgeService:
    def __init__(
        self,
        session: AsyncSession,
        *,
        embeddings: EmbeddingProvider | None = None,
        providers: Sequence[KnowledgeProvider] = (),
    ) -> None:
        self.session = session
        self.embeddings = embeddings
        self.index = SqliteVectorIndex(session)
        self._providers: dict[str, KnowledgeProvider] = {
            provider.key: provider for provider in providers
        }

    # ── provider registry ────────────────────────────────────────────────────

    def register(self, provider: KnowledgeProvider) -> None:
        self._providers[provider.key] = provider
        log.info(
            "knowledge_provider_registered",
            key=provider.key,
            kind=provider.kind.value,
            capabilities=sorted(c.value for c in provider.capabilities),
        )

    def get_provider(self, key: str) -> KnowledgeProvider:
        provider = self._providers.get(key)
        if provider is None:
            raise NotFoundError(f"Knowledge provider '{key}' is not registered")
        return provider

    def providers(self) -> list[KnowledgeProvider]:
        return list(self._providers.values())

    def providers_supporting(
        self, capability: KnowledgeCapability
    ) -> list[KnowledgeProvider]:
        return [p for p in self._providers.values() if p.supports(capability)]

    async def provider_status(self) -> list[dict[str, object]]:
        """What ``/api/knowledge/sources`` returns.

        Registered providers report real state, walked live. A source that is
        implemented but not configured is still listed, because omission is
        indistinguishable from "not built" — and after Phase 2.5 those are
        genuinely different facts about Obsidian: the connector exists
        (``implemented``) and may or may not have a vault behind it
        (``connected``). Collapsing them is what made the previous report
        useless.
        """
        rows: list[dict[str, object]] = []
        for provider in self._providers.values():
            status: ProviderStatus = await provider.status()
            payload = status.to_dict()
            payload["available"] = True
            payload["implemented"] = True
            rows.append(payload)

        if not any(row["kind"] == SourceKind.OBSIDIAN.value for row in rows):
            rows.append(
                {
                    "key": "obsidian",
                    "kind": SourceKind.OBSIDIAN.value,
                    "name": "Obsidian vault",
                    "connected": False,
                    "available": obsidian_contract.IMPLEMENTED,
                    "implemented": obsidian_contract.IMPLEMENTED,
                    "capabilities": sorted(
                        c.value
                        for c in obsidian_contract.EXPECTED_OBSIDIAN_CAPABILITIES
                    ),
                    "detail": (
                        "The connector is implemented; no vault is connected. "
                        "Point JARVIS at a vault folder in the Obsidian panel."
                    ),
                    "document_count": 0,
                    "last_synced_at": None,
                    "last_error": None,
                }
            )
        return rows

    # ── retrieval ────────────────────────────────────────────────────────────

    async def search(
        self,
        user_id: str,
        query: str,
        *,
        limit: int = 6,
        project_id: str | None = None,
        document_ids: list[str] | None = None,
        min_similarity: float = 0.2,
        candidate_pool: int = 120,
    ) -> KnowledgeResult:
        from jarvis.logging import timed

        with timed() as clock:
            result = await self._search(
                user_id, query, limit=limit, project_id=project_id,
                document_ids=document_ids, min_similarity=min_similarity,
                candidate_pool=candidate_pool,
            )
        result.duration_ms = clock.duration_ms
        log.debug(
            "knowledge_searched",
            hits=len(result.hits), considered=result.considered,
            duration_ms=round(result.duration_ms, 2),
        )
        return result

    async def _search(
        self,
        user_id: str,
        query: str,
        *,
        limit: int,
        project_id: str | None,
        document_ids: list[str] | None,
        min_similarity: float,
        candidate_pool: int,
    ) -> KnowledgeResult:
        terms = {w for w in _WORD_RE.findall(query.lower()) if w not in _STOPWORDS}

        stmt = (
            select(DocumentChunk, Document)
            .join(Document, DocumentChunk.document_id == Document.id)
            .where(
                Document.user_id == user_id,
                Document.status == DocumentStatus.INDEXED,
            )
        )
        if project_id:
            stmt = stmt.where(Document.project_id == project_id)
        if document_ids:
            stmt = stmt.where(Document.id.in_(document_ids))
        stmt = stmt.limit(candidate_pool)

        rows = list((await self.session.execute(stmt)).all())
        if not rows:
            return KnowledgeResult()

        chunk_ids = [chunk.id for chunk, _ in rows]
        similarity = await self._similarity(query, chunk_ids, min_similarity)

        semantic = bool(self.embeddings and self.embeddings.info.semantic)
        # Same reasoning as memory retrieval: a lexical vector is a second
        # opinion on evidence keyword matching already has, so it does not get
        # to lead.
        w_semantic, w_keyword = (0.7, 0.3) if semantic else (0.35, 0.65)

        hits: list[KnowledgeHit] = []
        for chunk, document in rows:
            sem = similarity.get(chunk.id, 0.0)
            kw = _keyword_score(chunk, terms)
            score = w_semantic * sem + w_keyword * kw
            if score <= 0.0:
                continue
            hits.append(
                KnowledgeHit(chunk=chunk, document=document, score=score,
                             semantic=sem, keyword=kw)
            )

        hits.sort(key=lambda h: h.score, reverse=True)
        return KnowledgeResult(
            hits=_diversify(hits, limit),
            considered=len(rows),
            semantic_used=semantic and bool(similarity),
        )

    async def _similarity(
        self, query: str, chunk_ids: list[str], min_similarity: float
    ) -> dict[str, float]:
        if self.embeddings is None or not chunk_ids:
            return {}
        try:
            vector = await self.embeddings.embed_one(query)
        except Exception as exc:
            log.warning("knowledge_embedding_failed", error=str(exc))
            return {}

        hits = await self.index.search(
            owner_kind=EmbeddingOwner.CHUNK,
            model=self.embeddings.info.model,
            query=vector,
            top_k=len(chunk_ids),
            owner_ids=chunk_ids,
            min_score=min_similarity,
        )
        return {hit.owner_id: hit.score for hit in hits}

    # ── documents ────────────────────────────────────────────────────────────

    async def document(self, document_id: str, user_id: str) -> Document:
        document = await self.session.get(Document, document_id)
        if document is None or document.user_id != user_id:
            raise NotFoundError(f"Document {document_id} not found")
        return document

    async def list_documents(
        self,
        user_id: str,
        *,
        project_id: str | None = None,
        status: DocumentStatus | None = None,
        search: str | None = None,
        limit: int = 100,
    ) -> list[Document]:
        stmt = select(Document).where(Document.user_id == user_id)
        if project_id:
            stmt = stmt.where(Document.project_id == project_id)
        if status:
            stmt = stmt.where(Document.status == status)
        if search:
            stmt = stmt.where(Document.title.ilike(f"%{search.strip()}%"))
        stmt = stmt.order_by(Document.created_at.desc()).limit(min(limit, 500))
        return list((await self.session.execute(stmt)).scalars().all())

    async def chunks(self, document_id: str, limit: int = 200) -> list[DocumentChunk]:
        stmt = (
            select(DocumentChunk)
            .where(DocumentChunk.document_id == document_id)
            .order_by(DocumentChunk.ordinal)
            .limit(limit)
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def stats(self, user_id: str) -> dict[str, object]:
        by_status = {
            (status.value if hasattr(status, "value") else status): int(count)
            for status, count in (
                await self.session.execute(
                    select(Document.status, func.count())
                    .where(Document.user_id == user_id)
                    .group_by(Document.status)
                )
            ).all()
        }
        chunk_count = int(
            (
                await self.session.execute(
                    select(func.count())
                    .select_from(DocumentChunk)
                    .join(Document, DocumentChunk.document_id == Document.id)
                    .where(Document.user_id == user_id)
                )
            ).scalar_one()
        )
        embedded = 0
        if self.embeddings is not None:
            embedded = await self.index.count(
                owner_kind=EmbeddingOwner.CHUNK, model=self.embeddings.info.model
            )
        return {
            "documents": sum(by_status.values()),
            "by_status": by_status,
            "chunks": chunk_count,
            "chunks_embedded": embedded,
        }

    @staticmethod
    def document_to_dict(document: Document) -> dict[str, object]:
        ref = SourceRef.from_dict(document.source_ref)
        return {
            "id": document.id,
            "title": document.title,
            "source_kind": document.source_kind.value
            if hasattr(document.source_kind, "value")
            else document.source_kind,
            "uri": document.uri,
            "provenance": ref.describe() if ref else None,
            "media_type": document.media_type,
            "byte_size": document.byte_size,
            "status": document.status.value
            if hasattr(document.status, "value")
            else document.status,
            "error": document.error,
            "tainted": document.tainted,
            "project_id": document.project_id,
            "tags": document.tags,
            "chunk_count": document.chunk_count,
            "token_estimate": document.token_estimate,
            "created_at": document.created_at.isoformat()
            if document.created_at
            else None,
            "indexed_at": document.indexed_at.isoformat()
            if document.indexed_at
            else None,
        }


def _keyword_score(chunk: DocumentChunk, terms: set[str]) -> float:
    if not terms:
        return 0.0
    # Tokenised, not substring: "use" must not match "user". See the same
    # reasoning in jarvis.memory.retrieval._keyword_overlap.
    haystack = set(_WORD_RE.findall(f"{chunk.content} {chunk.heading_path or ''}".lower()))
    present = terms & haystack
    if not present:
        return 0.0
    overlap = len(present) / len(terms)
    # Density normalisation: a 4,000-character chunk should not outrank a
    # focused one merely by containing more words.
    density = min(1.0, 400 / max(len(chunk.content), 1)) * 0.25
    return min(1.0, overlap + density * overlap)


def _diversify(hits: list[KnowledgeHit], limit: int) -> list[KnowledgeHit]:
    """Cap how much of the result set one document can occupy.

    Without this, a long document dominates: its twenty chunks all score
    similarly and crowd out the one relevant paragraph from somewhere else.
    Two per document until the limit is reached, then backfill.
    """
    per_document: dict[str, int] = {}
    primary: list[KnowledgeHit] = []
    overflow: list[KnowledgeHit] = []

    for hit in hits:
        count = per_document.get(hit.document.id, 0)
        if count < 2:
            per_document[hit.document.id] = count + 1
            primary.append(hit)
        else:
            overflow.append(hit)
        if len(primary) >= limit:
            break

    return (primary + overflow)[:limit]
