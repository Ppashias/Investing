"""Providers that exist and work in Phase 2.

Two of them:

* :class:`InternalKnowledgeProvider` — the documents JARVIS has already
  ingested. Read-only over its own store, and the provider that proves the
  abstraction is not shaped around a single external system.
* :class:`LocalFileProvider` — files under the configured knowledge roots.
  Real filesystem access, confined to an allow-list.

The allow-list is the whole security story for local files (§42). Every path
is resolved before it is checked, and containment is tested with
``relative_to`` on resolved paths — comparing string prefixes would let
``/approved/../etc/passwd`` through, and unresolved comparison would let a
symlink do the same.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from jarvis.db.models import Document, DocumentChunk
from jarvis.errors import NotFoundError, ValidationError
from jarvis.knowledge.base import (
    KnowledgeItem,
    KnowledgeProvider,
    KnowledgeSearchHit,
    ProviderStatus,
)
from jarvis.knowledge.ingestion import loaders
from jarvis.knowledge.types import (
    DocumentStatus,
    KnowledgeCapability,
    SourceKind,
    SourceRef,
)
from jarvis.logging import get_logger

log = get_logger(__name__)


class InternalKnowledgeProvider(KnowledgeProvider):
    """JARVIS's own ingested documents."""

    key = "internal"
    kind = SourceKind.INTERNAL
    name = "JARVIS knowledge base"

    def __init__(self, session: AsyncSession, user_id: str) -> None:
        self.session = session
        self.user_id = user_id

    @property
    def capabilities(self) -> frozenset[KnowledgeCapability]:
        # No CREATE/UPDATE/MOVE: documents enter through ingestion, which is a
        # pipeline, not a write. Declaring writes it does not have would make
        # the capability system meaningless on its very first provider.
        return frozenset(
            {
                KnowledgeCapability.SEARCH,
                KnowledgeCapability.READ,
                KnowledgeCapability.LIST,
                KnowledgeCapability.DELETE,
                KnowledgeCapability.METADATA,
            }
        )

    async def status(self) -> ProviderStatus:
        count = int(
            (
                await self.session.execute(
                    select(func.count())
                    .select_from(Document)
                    .where(Document.user_id == self.user_id)
                )
            ).scalar_one()
        )
        return ProviderStatus(
            key=self.key, kind=self.kind, name=self.name, connected=True,
            capabilities=sorted(self.capabilities, key=lambda c: c.value),
            detail="Documents ingested into JARVIS.", document_count=count,
        )

    async def search(
        self, query: str, *, limit: int = 20, **filters: object
    ) -> list[KnowledgeSearchHit]:
        like = f"%{query.strip()}%"
        rows = list(
            (
                await self.session.execute(
                    select(DocumentChunk, Document)
                    .join(Document, DocumentChunk.document_id == Document.id)
                    .where(
                        Document.user_id == self.user_id,
                        DocumentChunk.content.ilike(like),
                    )
                    .limit(limit)
                )
            ).all()
        )
        return [
            KnowledgeSearchHit(
                item=_document_to_item(document),
                score=1.0,
                excerpt=chunk.content[:400],
            )
            for chunk, document in rows
        ]

    async def read(self, item_id: str) -> KnowledgeItem:
        document = await self.session.get(Document, item_id)
        if document is None or document.user_id != self.user_id:
            raise NotFoundError(f"Document {item_id} not found")

        chunks = list(
            (
                await self.session.execute(
                    select(DocumentChunk)
                    .where(DocumentChunk.document_id == item_id)
                    .order_by(DocumentChunk.ordinal)
                )
            ).scalars().all()
        )
        item = _document_to_item(document)
        item.content = "\n\n".join(c.content for c in chunks)
        return item

    async def list_items(
        self, *, prefix: str | None = None, limit: int = 200
    ) -> list[KnowledgeItem]:
        stmt = select(Document).where(Document.user_id == self.user_id)
        if prefix:
            stmt = stmt.where(Document.title.ilike(f"{prefix}%"))
        rows = (await self.session.execute(stmt.limit(limit))).scalars().all()
        return [_document_to_item(d) for d in rows]

    async def metadata(self, item_id: str) -> dict[str, object]:
        document = await self.session.get(Document, item_id)
        if document is None or document.user_id != self.user_id:
            raise NotFoundError(f"Document {item_id} not found")
        return {
            "title": document.title,
            "status": document.status.value
            if hasattr(document.status, "value")
            else document.status,
            "chunks": document.chunk_count,
            "source_ref": document.source_ref,
            **(document.meta or {}),
        }

    async def delete(self, item_id: str) -> None:
        from jarvis.knowledge.ingestion.pipeline import IngestionPipeline

        document = await self.session.get(Document, item_id)
        if document is None or document.user_id != self.user_id:
            raise NotFoundError(f"Document {item_id} not found")
        await IngestionPipeline(self.session).delete_document(document)


class LocalFileProvider(KnowledgeProvider):
    """Files under the configured roots.

    Read-only by design. Write access to the filesystem is Phase 3's problem
    and needs the permission engine's ``WRITE`` capability behind it; a
    knowledge provider quietly gaining file-write would route around that.
    """

    key = "local"
    kind = SourceKind.LOCAL_FILE
    name = "Local files"

    def __init__(self, roots: list[Path]) -> None:
        self.roots = [Path(r).expanduser().resolve() for r in roots]

    @property
    def capabilities(self) -> frozenset[KnowledgeCapability]:
        return frozenset(
            {
                KnowledgeCapability.SEARCH,
                KnowledgeCapability.READ,
                KnowledgeCapability.LIST,
                KnowledgeCapability.INGEST,
            }
        )

    async def status(self) -> ProviderStatus:
        readable = [r for r in self.roots if r.is_dir()]
        return ProviderStatus(
            key=self.key, kind=self.kind, name=self.name,
            connected=bool(readable),
            capabilities=sorted(self.capabilities, key=lambda c: c.value),
            detail=(
                "Roots: " + ", ".join(str(r) for r in readable)
                if readable
                else "No readable directories configured "
                     "(set JARVIS_KNOWLEDGE_ROOTS)."
            ),
            document_count=len(await self._scan()),
        )

    async def list_items(
        self, *, prefix: str | None = None, limit: int = 200
    ) -> list[KnowledgeItem]:
        items = await self._scan(limit=limit)
        if prefix:
            items = [i for i in items if i.title.lower().startswith(prefix.lower())]
        return items

    async def search(
        self, query: str, *, limit: int = 20, **filters: object
    ) -> list[KnowledgeSearchHit]:
        """Filename matching only.

        Content search would mean reading every file on every query. Content
        becomes searchable by ingesting it, which is what ``INGEST`` is for.
        """
        needle = query.strip().lower()
        return [
            KnowledgeSearchHit(item=item, score=1.0)
            for item in await self._scan(limit=500)
            if needle in item.title.lower()
        ][:limit]

    async def read(self, item_id: str) -> KnowledgeItem:
        path = self._resolve(item_id)
        loader = loaders.loader_for(path.name)
        loaded = loader.load(path.read_bytes(), filename=path.name)
        item = _path_to_item(path)
        item.content = loaded.text
        item.media_type = loaded.media_type
        return item

    async def iter_ingestable(self, *, limit: int = 1000) -> list[KnowledgeItem]:
        return await self._scan(limit=limit)

    # ── internals ────────────────────────────────────────────────────────────

    def _resolve(self, raw: str) -> Path:
        """Resolve then check containment. Order matters."""
        path = Path(raw).expanduser().resolve()
        if not any(_contains(root, path) for root in self.roots):
            raise ValidationError(
                "Path outside the configured knowledge roots",
                user_message="That file is outside the approved directories.",
            )
        if not path.is_file():
            raise NotFoundError(f"{path.name} is not a readable file")
        return path

    async def _scan(self, limit: int = 200) -> list[KnowledgeItem]:
        extensions = loaders.available_extensions()
        found: list[KnowledgeItem] = []
        for root in self.roots:
            if not root.is_dir():
                continue
            for path in sorted(root.rglob("*")):
                if len(found) >= limit:
                    return found
                if not path.is_file() or path.suffix.lower() not in extensions:
                    continue
                # rglob follows into symlinked directories, which can point
                # anywhere. Re-check containment on the resolved path.
                resolved = path.resolve()
                if not any(_contains(r, resolved) for r in self.roots):
                    log.warning("knowledge_symlink_escape_skipped", path=str(path))
                    continue
                found.append(_path_to_item(resolved))
        return found


def _contains(root: Path, candidate: Path) -> bool:
    try:
        candidate.relative_to(root)
    except ValueError:
        return False
    return True


def _document_to_item(document: Document) -> KnowledgeItem:
    ref = SourceRef.from_dict(document.source_ref) or SourceRef(
        kind=SourceKind.INTERNAL, locator=document.uri, title=document.title
    )
    return KnowledgeItem(
        id=document.id,
        title=document.title,
        ref=ref,
        media_type=document.media_type,
        byte_size=document.byte_size,
        modified_at=document.updated_at,
        tags=document.tags,
        metadata={
            "status": document.status.value
            if hasattr(document.status, "value")
            else document.status,
            "chunks": document.chunk_count,
        },
    )


def _path_to_item(path: Path) -> KnowledgeItem:
    stat = path.stat()
    return KnowledgeItem(
        id=str(path),
        title=path.stem,
        ref=SourceRef(kind=SourceKind.LOCAL_FILE, locator=str(path), title=path.stem),
        byte_size=stat.st_size,
        modified_at=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
        metadata={"suffix": path.suffix.lower()},
    )
