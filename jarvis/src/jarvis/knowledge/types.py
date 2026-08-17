"""Knowledge vocabulary and provenance.

Like :mod:`jarvis.memory.types` this is a leaf module: no database, no service
imports, so :mod:`jarvis.db.models` can depend on it.

The centrepiece is :class:`SourceRef` — the answer to "where did you get this?"
(§26). It is one shape for every provider rather than a column per provider,
because the alternative is a schema migration every time a knowledge source is
added, and §38 asks specifically that Phase 2.5 be implementable without
redesigning the database.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


class SourceKind(str, enum.Enum):
    """Which provider a document or chunk came from.

    ``OBSIDIAN`` is declared and unimplemented, exactly as §4 and §43 require.
    :func:`jarvis.knowledge.service.KnowledgeService.providers` reports what is
    actually registered, so nothing can infer availability from this enum.
    """

    INTERNAL = "INTERNAL"
    LOCAL_FILE = "LOCAL_FILE"
    OBSIDIAN = "OBSIDIAN"
    WEB = "WEB"
    NOTION = "NOTION"
    UPLOAD = "UPLOAD"


class KnowledgeCapability(str, enum.Enum):
    """What a provider can actually do (§32).

    Capability detection rather than a fat interface with stubs: a read-only
    web source must not expose ``create_document`` at all, because a provider
    that raises ``NotImplementedError`` is indistinguishable, from the caller's
    side, from one that is broken.
    """

    SEARCH = "SEARCH"
    READ = "READ"
    LIST = "LIST"
    CREATE = "CREATE"
    UPDATE = "UPDATE"
    DELETE = "DELETE"
    MOVE = "MOVE"
    METADATA = "METADATA"
    #: Wikilink-style graphs. Obsidian has them; a PDF folder does not.
    LINKS = "LINKS"
    #: Two-way reconciliation with an external system.
    SYNC = "SYNC"
    #: Pull content in for chunking and embedding.
    INGEST = "INGEST"


class DocumentStatus(str, enum.Enum):
    """Ingestion lifecycle. Every state here is one the pipeline actually sets;
    there is no spinner without a corresponding backend state (§29)."""

    REGISTERED = "REGISTERED"
    EXTRACTING = "EXTRACTING"
    CHUNKING = "CHUNKING"
    EMBEDDING = "EMBEDDING"
    INDEXED = "INDEXED"
    FAILED = "FAILED"
    STALE = "STALE"
    ARCHIVED = "ARCHIVED"

    @property
    def is_terminal(self) -> bool:
        return self in {
            DocumentStatus.INDEXED,
            DocumentStatus.FAILED,
            DocumentStatus.ARCHIVED,
        }


class ChunkKind(str, enum.Enum):
    """What a chunk structurally *is*.

    Kept because it changes how a chunk should be treated: a code block must
    not be split mid-line, and a table loses its meaning without its header.
    """

    PROSE = "PROSE"
    HEADING_SECTION = "HEADING_SECTION"
    CODE = "CODE"
    TABLE = "TABLE"
    LIST = "LIST"
    FRONTMATTER = "FRONTMATTER"
    ROW_GROUP = "ROW_GROUP"


class SyncStatus(str, enum.Enum):
    """Sync state for a registered source (§30, §39).

    Present in the schema so the Connections UI has real states to render in
    Phase 2.5. ``NEVER_SYNCED`` is what every source reports in Phase 2,
    because nothing syncs yet.
    """

    NEVER_SYNCED = "NEVER_SYNCED"
    SYNCED = "SYNCED"
    PENDING = "PENDING"
    CONFLICT = "CONFLICT"
    ERROR = "ERROR"
    DISABLED = "DISABLED"


class SyncDirection(str, enum.Enum):
    """Configurable per source (§39). Bidirectional is not the default, because
    automatic two-way sync is how people lose hand-written notes."""

    NONE = "NONE"
    PULL = "PULL"
    PUSH = "PUSH"
    BIDIRECTIONAL = "BIDIRECTIONAL"


# ── provenance ───────────────────────────────────────────────────────────────


@dataclass(slots=True)
class ObsidianRef:
    """Everything needed to identify a note in a vault (§38).

    Written now, unused now. The point is that a Phase 2.5 connector can
    populate this and find every field already has a home — including
    ``links``/``backlinks``, which are the fields most likely to be forgotten
    until the graph features are attempted and the schema has to change.
    """

    vault_id: str | None = None
    vault_name: str | None = None
    #: Absolute path of the vault root on disk.
    vault_path: str | None = None
    #: Path of the note *within* the vault — the stable identity Obsidian uses.
    note_path: str | None = None
    note_title: str | None = None
    #: Obsidian's own UID when a plugin provides one; the vault-relative path
    #: otherwise, since base Obsidian has no note IDs.
    note_id: str | None = None
    #: YAML frontmatter, verbatim.
    frontmatter: dict[str, Any] = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)
    #: Wikilink targets found in the note.
    links: list[str] = field(default_factory=list)
    #: Notes linking *to* this one. Derived, cached here to avoid a graph walk.
    backlinks: list[str] = field(default_factory=list)
    #: Heading path of the specific section, when provenance is sub-note.
    section: str | None = None
    #: Content hash at last read, for change detection without a full re-read.
    content_hash: str | None = None
    last_synced_at: datetime | None = None
    sync_status: SyncStatus = SyncStatus.NEVER_SYNCED

    def to_dict(self) -> dict[str, Any]:
        return {
            "vault_id": self.vault_id,
            "vault_name": self.vault_name,
            "vault_path": self.vault_path,
            "note_path": self.note_path,
            "note_title": self.note_title,
            "note_id": self.note_id,
            "frontmatter": self.frontmatter,
            "tags": self.tags,
            "links": self.links,
            "backlinks": self.backlinks,
            "section": self.section,
            "content_hash": self.content_hash,
            "last_synced_at": (
                self.last_synced_at.isoformat() if self.last_synced_at else None
            ),
            "sync_status": self.sync_status.value,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "ObsidianRef":
        stamp = raw.get("last_synced_at")
        return cls(
            vault_id=raw.get("vault_id"),
            vault_name=raw.get("vault_name"),
            vault_path=raw.get("vault_path"),
            note_path=raw.get("note_path"),
            note_title=raw.get("note_title"),
            note_id=raw.get("note_id"),
            frontmatter=raw.get("frontmatter") or {},
            tags=list(raw.get("tags") or []),
            links=list(raw.get("links") or []),
            backlinks=list(raw.get("backlinks") or []),
            section=raw.get("section"),
            content_hash=raw.get("content_hash"),
            last_synced_at=datetime.fromisoformat(stamp) if stamp else None,
            sync_status=SyncStatus(raw.get("sync_status", SyncStatus.NEVER_SYNCED.value)),
        )


@dataclass(slots=True)
class SourceRef:
    """Portable provenance. One shape, every provider.

    ``locator`` is whatever uniquely identifies the thing within its source: a
    filesystem path, a URL, a vault-relative note path. ``extra`` carries
    provider-specific detail — :class:`ObsidianRef` serialises into it — so a
    new provider never needs a schema change.
    """

    kind: SourceKind
    locator: str | None = None
    title: str | None = None
    #: Heading path within the document, e.g. ``Architecture > Storage``.
    section: str | None = None
    #: Provider-assigned version, revision, or ETag.
    version: str | None = None
    content_hash: str | None = None
    ingested_at: datetime | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "locator": self.locator,
            "title": self.title,
            "section": self.section,
            "version": self.version,
            "content_hash": self.content_hash,
            "ingested_at": self.ingested_at.isoformat() if self.ingested_at else None,
            "extra": self.extra,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> "SourceRef | None":
        if not raw:
            return None
        stamp = raw.get("ingested_at")
        return cls(
            kind=SourceKind(raw.get("kind", SourceKind.INTERNAL.value)),
            locator=raw.get("locator"),
            title=raw.get("title"),
            section=raw.get("section"),
            version=raw.get("version"),
            content_hash=raw.get("content_hash"),
            ingested_at=datetime.fromisoformat(stamp) if stamp else None,
            extra=raw.get("extra") or {},
        )

    # ── Obsidian convenience ─────────────────────────────────────────────────

    @classmethod
    def from_obsidian(cls, ref: ObsidianRef) -> "SourceRef":
        """Build provenance for a note. No connector calls this yet; the
        Obsidian-readiness test does, which is the point — it proves the round
        trip works before anything depends on it."""
        return cls(
            kind=SourceKind.OBSIDIAN,
            locator=ref.note_path,
            title=ref.note_title,
            section=ref.section,
            content_hash=ref.content_hash,
            extra={"obsidian": ref.to_dict()},
        )

    @property
    def obsidian(self) -> ObsidianRef | None:
        raw = self.extra.get("obsidian")
        return ObsidianRef.from_dict(raw) if isinstance(raw, dict) else None

    def describe(self) -> str:
        """One line, for the model and the UI. This is what makes "where did
        you get this?" answerable in a sentence."""
        parts = [self.title or self.locator or self.kind.value]
        if self.section:
            parts.append(f"§{self.section}")
        return " — ".join(p for p in parts if p)
