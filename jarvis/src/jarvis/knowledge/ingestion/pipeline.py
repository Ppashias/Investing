"""The ingestion pipeline (§24).

    SOURCE → LOAD → EXTRACT → CLEAN → CHUNK → METADATA → EMBED → STORE → INDEX

Implemented as named stages against a shared context, mirroring the
orchestrator's pipeline for the same reason: the stages need to be separately
testable, and Phase 2.5's Obsidian sync will re-run a subset of them (a note
whose bytes changed needs re-chunking, not re-loading from disk).

Status is written to the document row as each stage completes, so
``/api/knowledge/documents`` reflects real progress. §29 forbids fake indexing
indicators; the states in :class:`~jarvis.knowledge.types.DocumentStatus` are
the states the pipeline actually passes through.

## Provenance is not optional

Every chunk can name its document, its section, and its byte range, and every
document carries a :class:`SourceRef`. "Where did you get this?" is answerable
for any retrieved fragment (§26) — which is also the mechanism that lets a user
judge whether to believe it.

## Re-ingestion is cheap

Content is hashed. A document whose bytes are unchanged skips straight to
``INDEXED`` without re-chunking or re-embedding, which is what makes rescanning
a folder something you can do routinely rather than something you avoid.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import delete as sql_delete
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from jarvis.db.base import utcnow
from jarvis.db.models import Document, DocumentChunk, EmbeddingOwner
from jarvis.errors import JarvisError, ValidationError
from jarvis.knowledge.ingestion import loaders
from jarvis.knowledge.ingestion.chunking import Chunk, chunk_document
from jarvis.knowledge.types import DocumentStatus, SourceKind, SourceRef
from jarvis.logging import get_logger, timed
from jarvis.memory.vectors import SqliteVectorIndex
from jarvis.providers.embeddings import EmbeddingProvider

log = get_logger(__name__)


@dataclass(slots=True)
class IngestRequest:
    user_id: str
    filename: str
    data: bytes
    source_kind: SourceKind = SourceKind.UPLOAD
    source_ref: SourceRef | None = None
    source_id: str | None = None
    media_type: str | None = None
    project_id: str | None = None
    tags: list[str] = field(default_factory=list)
    title: str | None = None
    #: Re-ingest even when the content hash is unchanged.
    force: bool = False


@dataclass(slots=True)
class IngestResult:
    document: Document
    chunks_created: int
    chunks_embedded: int
    skipped_unchanged: bool = False
    duration_ms: float = 0.0
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "document_id": self.document.id,
            "title": self.document.title,
            "status": self.document.status.value
            if hasattr(self.document.status, "value")
            else self.document.status,
            "chunks": self.chunks_created,
            "embedded": self.chunks_embedded,
            "skipped_unchanged": self.skipped_unchanged,
            "duration_ms": round(self.duration_ms, 1),
            "warnings": self.warnings,
        }


class IngestionPipeline:
    def __init__(
        self,
        session: AsyncSession,
        *,
        embeddings: EmbeddingProvider | None = None,
        max_bytes: int = 25 * 1024 * 1024,
        chunk_target_chars: int = 1_400,
        chunk_overlap_chars: int = 160,
    ) -> None:
        self.session = session
        self.embeddings = embeddings
        self.index = SqliteVectorIndex(session)
        self.max_bytes = max_bytes
        self.chunk_target_chars = chunk_target_chars
        self.chunk_overlap_chars = chunk_overlap_chars

    async def ingest(self, request: IngestRequest) -> IngestResult:
        with timed() as clock:
            result = await self._ingest(request)
        result.duration_ms = clock.duration_ms
        log.info(
            "document_ingested",
            document_id=result.document.id,
            chunks=result.chunks_created,
            embedded=result.chunks_embedded,
            skipped=result.skipped_unchanged,
            duration_ms=round(result.duration_ms, 1),
        )
        return result

    async def _ingest(self, request: IngestRequest) -> IngestResult:
        warnings: list[str] = []

        # ── SOURCE ───────────────────────────────────────────────────────────
        if not request.data:
            raise ValidationError(
                "Empty file", user_message="That file is empty."
            )
        if len(request.data) > self.max_bytes:
            raise ValidationError(
                f"File exceeds {self.max_bytes} bytes",
                user_message=(
                    f"That file is {len(request.data) // 1_048_576} MB; the limit "
                    f"is {self.max_bytes // 1_048_576} MB."
                ),
            )

        content_hash = hashlib.sha256(request.data).hexdigest()
        uri = request.source_ref.locator if request.source_ref else request.filename

        document = await self._find_existing(request.user_id, uri, request.source_kind)
        if (
            document is not None
            and document.content_hash == content_hash
            and document.status is DocumentStatus.INDEXED
            and not request.force
        ):
            return IngestResult(document, document.chunk_count, 0, skipped_unchanged=True)

        # ── LOAD + EXTRACT + CLEAN ───────────────────────────────────────────
        # Sanitisation happens inside the loader, against the decoded text, so
        # every format gets it whether or not a future loader remembers to.
        loader = loaders.loader_for(request.filename, request.media_type)
        loaded = loader.load(request.data, filename=request.filename)

        if document is None:
            document = Document(
                user_id=request.user_id,
                source_id=request.source_id,
                title=request.title or loaded.title,
                source_kind=request.source_kind,
                uri=uri,
                project_id=request.project_id,
                tags=list(request.tags),
            )
            self.session.add(document)
            await self.session.flush()

        document.status = DocumentStatus.EXTRACTING
        document.title = request.title or loaded.title
        document.media_type = loaded.media_type
        document.byte_size = len(request.data)
        document.content_hash = content_hash
        document.error = None
        # Every ingested document is untrusted input, whatever its source.
        document.tainted = True

        ref = request.source_ref or SourceRef(
            kind=request.source_kind, locator=uri, title=document.title
        )
        ref.content_hash = content_hash
        ref.ingested_at = utcnow()
        document.source_ref = ref.to_dict()
        document.meta = {**(document.meta or {}), **loaded.metadata}
        await self.session.flush()

        if not loaded.text.strip():
            document.status = DocumentStatus.FAILED
            document.error = "No extractable text."
            await self.session.flush()
            # A scanned PDF is the usual cause, and saying so is more useful
            # than a bare failure.
            raise ValidationError(
                "No extractable text",
                user_message=(
                    "I could not extract any text from that file. If it is a "
                    "scanned PDF, it needs OCR, which is not implemented."
                ),
            )

        # ── CHUNK ────────────────────────────────────────────────────────────
        document.status = DocumentStatus.CHUNKING
        await self.session.flush()

        chunks = chunk_document(
            loaded.text,
            media_type=loaded.media_type,
            target_chars=self.chunk_target_chars,
            overlap_chars=self.chunk_overlap_chars,
        )
        if not chunks:
            document.status = DocumentStatus.FAILED
            document.error = "Produced no chunks."
            await self.session.flush()
            raise ValidationError(
                "Produced no chunks",
                user_message="That file had no content I could index.",
            )

        # Replace wholesale rather than diffing: chunk boundaries move when
        # content changes, so a positional diff would mostly produce spurious
        # updates. Their embeddings go too, or they would be orphaned.
        await self._clear_chunks(document.id)

        # ── METADATA + STORE ─────────────────────────────────────────────────
        rows: list[DocumentChunk] = []
        for ordinal, chunk in enumerate(chunks):
            row = DocumentChunk(
                document_id=document.id,
                ordinal=ordinal,
                kind=chunk.kind,
                content=chunk.content,
                heading_path=chunk.heading_path,
                char_start=chunk.char_start,
                char_end=chunk.char_end,
                token_estimate=chunk.token_estimate,
                meta={
                    "document_title": document.title,
                    "source_kind": document.source_kind.value
                    if hasattr(document.source_kind, "value")
                    else document.source_kind,
                    **chunk.metadata,
                },
            )
            rows.append(row)
            self.session.add(row)
        await self.session.flush()

        # ── EMBED + INDEX ────────────────────────────────────────────────────
        document.status = DocumentStatus.EMBEDDING
        await self.session.flush()

        embedded = 0
        if self.embeddings is not None:
            embedded, embed_warnings = await self._embed_chunks(rows)
            warnings.extend(embed_warnings)
        else:
            warnings.append(
                "No embedding provider configured; this document is searchable "
                "by keyword only."
            )

        document.chunk_count = len(rows)
        document.token_estimate = sum(r.token_estimate for r in rows)
        document.status = DocumentStatus.INDEXED
        document.indexed_at = utcnow()
        await self.session.flush()

        return IngestResult(
            document=document,
            chunks_created=len(rows),
            chunks_embedded=embedded,
            warnings=warnings,
        )

    async def _embed_chunks(
        self, rows: list[DocumentChunk]
    ) -> tuple[int, list[str]]:
        """Embed in batches.

        A failure degrades the document to keyword-searchable rather than
        failing the ingestion: a document that is stored and partly indexed is
        more useful than one that is rejected because an endpoint blipped.
        """
        assert self.embeddings is not None
        warnings: list[str] = []
        embedded = 0
        batch_size = 32

        for start in range(0, len(rows), batch_size):
            batch = rows[start : start + batch_size]
            # The heading path is prepended so the vector reflects where the
            # text sits, not just what it says — "Storage" and "Retrieval"
            # sections of one document should not embed identically.
            texts = [
                f"{r.heading_path}\n{r.content}" if r.heading_path else r.content
                for r in batch
            ]
            try:
                vectors = await self.embeddings.embed(texts)
            except Exception as exc:
                log.warning("chunk_embedding_failed", error=str(exc),
                            batch_start=start)
                warnings.append(
                    "Some chunks could not be embedded; they remain searchable "
                    "by keyword."
                )
                continue

            for row, vector in zip(batch, vectors):
                await self.index.upsert(
                    owner_kind=EmbeddingOwner.CHUNK,
                    owner_id=row.id,
                    model=self.embeddings.info.model,
                    vector=vector,
                )
                embedded += 1

        return embedded, warnings

    async def _find_existing(
        self, user_id: str, uri: str | None, kind: SourceKind
    ) -> Document | None:
        if not uri:
            return None
        return (
            await self.session.execute(
                select(Document).where(
                    Document.user_id == user_id,
                    Document.uri == uri,
                    Document.source_kind == kind,
                )
            )
        ).scalars().first()

    async def _clear_chunks(self, document_id: str) -> None:
        existing = list(
            (
                await self.session.execute(
                    select(DocumentChunk.id).where(
                        DocumentChunk.document_id == document_id
                    )
                )
            ).scalars().all()
        )
        for chunk_id in existing:
            await self.index.delete(
                owner_kind=EmbeddingOwner.CHUNK, owner_id=chunk_id
            )
        await self.session.execute(
            sql_delete(DocumentChunk).where(DocumentChunk.document_id == document_id)
        )
        await self.session.flush()

    async def delete_document(self, document: Document) -> None:
        """Remove a document, its chunks, and their vectors."""
        await self._clear_chunks(document.id)
        await self.session.delete(document)
        await self.session.flush()
        log.info("document_deleted", document_id=document.id)


async def ingest_path(
    pipeline: IngestionPipeline,
    path: Path,
    *,
    user_id: str,
    allowed_roots: list[Path],
    project_id: str | None = None,
    source_kind: SourceKind = SourceKind.LOCAL_FILE,
    tags: list[str] | None = None,
) -> IngestResult:
    """Ingest a file from disk, inside an allow-list.

    The allow-list is the security boundary (§42). ``resolve()`` first, then
    containment: comparing unresolved paths lets ``docs/../../.ssh/id_rsa``
    through, and symlinks make it worse. Resolving both sides and using
    ``relative_to`` is the check that actually holds.
    """
    resolved = path.expanduser().resolve()

    if not allowed_roots:
        raise ValidationError(
            "No knowledge roots configured",
            user_message=(
                "No directories are approved for ingestion. Set "
                "JARVIS_KNOWLEDGE_ROOTS before ingesting from disk."
            ),
        )

    if not any(_contains(root, resolved) for root in allowed_roots):
        # The message names the roots, not the rejected path: echoing an
        # arbitrary path back is a small information disclosure and unhelpful.
        raise ValidationError(
            "Path outside the configured knowledge roots",
            user_message=(
                "That file is outside the approved directories. Approved: "
                + ", ".join(str(r) for r in allowed_roots)
            ),
        )

    if not resolved.is_file():
        raise ValidationError(
            "Not a file", user_message="That path is not a readable file."
        )

    try:
        data = resolved.read_bytes()
    except OSError as exc:
        raise ValidationError(
            f"Could not read {resolved.name}: {exc}",
            user_message=f"I could not read {resolved.name}.",
        ) from exc

    return await pipeline.ingest(
        IngestRequest(
            user_id=user_id,
            filename=resolved.name,
            data=data,
            source_kind=source_kind,
            source_ref=SourceRef(
                kind=source_kind, locator=str(resolved), title=resolved.stem
            ),
            project_id=project_id,
            tags=list(tags or []),
        )
    )


def _contains(root: Path, candidate: Path) -> bool:
    try:
        candidate.relative_to(root)
    except ValueError:
        return False
    return True
