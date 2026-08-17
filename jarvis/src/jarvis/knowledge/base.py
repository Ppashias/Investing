"""Knowledge provider interface (§22, §32).

The rule this file exists to enforce is §22's: **nothing outside
``jarvis/knowledge/providers/`` may import a provider-specific API.** Obsidian,
Notion, a web fetcher and the local filesystem are all reached through the same
five-method surface, so adding one never touches the memory system, the
orchestrator, or the UI.

## Capability detection instead of a fat interface

Providers differ genuinely. A vault supports create, move and backlinks; a web
source supports search and read and nothing else. Two ways to model that:

* One large interface where unsupported methods raise ``NotImplementedError``.
  From the caller's side a provider that raises is indistinguishable from one
  that is broken, and the UI has to hard-code which provider does what.
* A small required core plus declared capabilities, which is what this is.
  :meth:`KnowledgeProvider.supports` is the single question a caller asks, and
  the UI renders affordances from the answer rather than from a provider name.

## The Obsidian contract

§4 lists thirteen operations a future connector should support. They are mapped
onto this interface in :mod:`jarvis.knowledge.providers.obsidian_contract` —
which is documentation and type signatures with **no implementation**, because
§4 also says not to build fake versions. The mapping is written now so that
Phase 2.5 discovers any interface gap while it is still cheap to fix.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Sequence

from jarvis.knowledge.types import KnowledgeCapability, SourceKind, SourceRef


@dataclass(slots=True)
class KnowledgeItem:
    """A document as a provider sees it, before ingestion.

    ``content`` may be absent from a search result and present after a read —
    listing a thousand notes should not read a thousand files.
    """

    id: str
    title: str
    ref: SourceRef
    content: str | None = None
    media_type: str | None = None
    byte_size: int | None = None
    modified_at: datetime | None = None
    tags: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "ref": self.ref.to_dict(),
            "has_content": self.content is not None,
            "media_type": self.media_type,
            "byte_size": self.byte_size,
            "modified_at": self.modified_at.isoformat() if self.modified_at else None,
            "tags": self.tags,
            "metadata": self.metadata,
        }


@dataclass(slots=True)
class KnowledgeSearchHit:
    item: KnowledgeItem
    score: float
    #: The matching fragment, when the provider can produce one.
    excerpt: str | None = None


@dataclass(slots=True)
class ProviderStatus:
    """What the Connections UI renders (§30). Every field is real state a
    provider reports; nothing here is a placeholder for a future spinner."""

    key: str
    kind: SourceKind
    name: str
    connected: bool
    capabilities: list[KnowledgeCapability]
    detail: str = ""
    document_count: int = 0
    last_synced_at: datetime | None = None
    last_error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "kind": self.kind.value,
            "name": self.name,
            "connected": self.connected,
            "capabilities": [c.value for c in self.capabilities],
            "detail": self.detail,
            "document_count": self.document_count,
            "last_synced_at": (
                self.last_synced_at.isoformat() if self.last_synced_at else None
            ),
            "last_error": self.last_error,
        }


class KnowledgeProvider(abc.ABC):
    """A source of documents.

    Only :meth:`status` and :meth:`capabilities` are required. Everything else
    is guarded by :meth:`supports`, and the base class raises a clear,
    catchable error rather than ``NotImplementedError`` so an unsupported
    operation is a *decision* the caller can report, not a crash.
    """

    #: Stable handle: ``local:docs``, ``obsidian:main-vault``.
    key: str = "provider"
    kind: SourceKind = SourceKind.INTERNAL
    name: str = "Knowledge provider"

    # ── required ─────────────────────────────────────────────────────────────

    @property
    @abc.abstractmethod
    def capabilities(self) -> frozenset[KnowledgeCapability]: ...

    @abc.abstractmethod
    async def status(self) -> ProviderStatus: ...

    def supports(self, capability: KnowledgeCapability) -> bool:
        return capability in self.capabilities

    # ── optional, capability-gated ───────────────────────────────────────────

    async def search(
        self, query: str, *, limit: int = 20, **filters: Any
    ) -> list[KnowledgeSearchHit]:
        self._unsupported(KnowledgeCapability.SEARCH)

    async def read(self, item_id: str) -> KnowledgeItem:
        self._unsupported(KnowledgeCapability.READ)

    async def list_items(
        self, *, prefix: str | None = None, limit: int = 200
    ) -> list[KnowledgeItem]:
        self._unsupported(KnowledgeCapability.LIST)

    async def create(
        self, *, title: str, content: str, path: str | None = None, **metadata: Any
    ) -> KnowledgeItem:
        self._unsupported(KnowledgeCapability.CREATE)

    async def update(self, item_id: str, *, content: str, **metadata: Any) -> KnowledgeItem:
        self._unsupported(KnowledgeCapability.UPDATE)

    async def delete(self, item_id: str) -> None:
        self._unsupported(KnowledgeCapability.DELETE)

    async def move(self, item_id: str, new_path: str) -> KnowledgeItem:
        self._unsupported(KnowledgeCapability.MOVE)

    async def metadata(self, item_id: str) -> dict[str, Any]:
        self._unsupported(KnowledgeCapability.METADATA)

    async def links(self, item_id: str) -> dict[str, list[str]]:
        """``{"links": [...], "backlinks": [...]}`` for graph-shaped sources."""
        self._unsupported(KnowledgeCapability.LINKS)

    async def sync(self, *, direction: str = "pull") -> dict[str, Any]:
        self._unsupported(KnowledgeCapability.SYNC)

    async def iter_ingestable(
        self, *, limit: int = 1000
    ) -> Sequence[KnowledgeItem]:
        """Items this provider offers for ingestion."""
        self._unsupported(KnowledgeCapability.INGEST)

    # ── helpers ──────────────────────────────────────────────────────────────

    def _unsupported(self, capability: KnowledgeCapability) -> None:
        from jarvis.errors import ValidationError

        raise ValidationError(
            f"Provider '{self.key}' does not support {capability.value}",
            user_message=(
                f"{self.name} cannot do that — it supports "
                f"{', '.join(sorted(c.value.lower() for c in self.capabilities))}."
            ),
        )
