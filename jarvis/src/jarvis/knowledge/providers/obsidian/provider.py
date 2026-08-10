"""The Obsidian knowledge provider (§3).

    JARVIS → KnowledgeService → KnowledgeProvider → ObsidianProvider
                                                        → VaultTransport
                                                            → the vault

This class is the whole of Obsidian's presence in the provider abstraction. It
translates the generic five-method surface into vault operations and back, and
it is where the contract written in Phase 2 stops being a table and starts
being code — every row of ``obsidian_contract.OPERATION_MAP`` resolves to a
method here, which is what that table existed to guarantee.

Two things it deliberately does **not** do:

* **It does not check permissions.** Authorisation is the existing
  :class:`~jarvis.permissions.engine.PermissionEngine`'s job, applied one layer
  up in :class:`~jarvis.knowledge.providers.obsidian.service.ObsidianService`.
  A provider that authorised its own writes would be a second permission
  system, which §20 forbids for good reason: two systems disagree, and the
  more permissive one wins.
* **It does not decide what to index.** Ingestion is the existing pipeline's
  job. This provider hands it bytes and provenance.

Capabilities are computed from the vault, not declared as a constant. A vault
on a read-only mount reports ``READ``/``SEARCH``/``LIST`` and genuinely does
not offer ``CREATE`` — §7 says only display capabilities that actually work,
and the honest place to enforce that is where the filesystem is visible.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Sequence

from jarvis.knowledge.base import (
    KnowledgeItem,
    KnowledgeProvider,
    KnowledgeSearchHit,
    ProviderStatus,
)
from jarvis.knowledge.providers.obsidian.vault import (
    Note,
    NoteMeta,
    VaultError,
    VaultTransport,
    json_safe,
)
from jarvis.knowledge.types import (
    KnowledgeCapability,
    ObsidianRef,
    SourceKind,
    SourceRef,
    SyncStatus,
)
from jarvis.logging import get_logger

log = get_logger(__name__)

#: Capabilities every reachable vault offers.
_READ_CAPABILITIES: frozenset[KnowledgeCapability] = frozenset(
    {
        KnowledgeCapability.SEARCH,
        KnowledgeCapability.READ,
        KnowledgeCapability.LIST,
        KnowledgeCapability.METADATA,
        KnowledgeCapability.LINKS,
        KnowledgeCapability.INGEST,
        KnowledgeCapability.SYNC,
    }
)

#: Added only when the vault directory is actually writable.
_WRITE_CAPABILITIES: frozenset[KnowledgeCapability] = frozenset(
    {
        KnowledgeCapability.CREATE,
        KnowledgeCapability.UPDATE,
        KnowledgeCapability.DELETE,
        KnowledgeCapability.MOVE,
    }
)


class ObsidianProvider(KnowledgeProvider):
    """One vault, behind the generic provider interface."""

    kind = SourceKind.OBSIDIAN

    def __init__(
        self,
        transport: VaultTransport,
        *,
        vault_id: str | None = None,
        allow_writes: bool = True,
        last_synced_at: datetime | None = None,
        document_count: int = 0,
    ) -> None:
        self.transport = transport
        self.vault_id = vault_id
        # The operator's switch, independent of the filesystem's. Both must
        # agree before a write capability is claimed.
        self.allow_writes = allow_writes
        self.last_synced_at = last_synced_at
        self.document_count = document_count
        self._last_error: str | None = None

    # ── identity ─────────────────────────────────────────────────────────────

    @property
    def key(self) -> str:  # type: ignore[override]
        return f"obsidian:{self.transport.name}"

    @property
    def name(self) -> str:  # type: ignore[override]
        return f"Obsidian — {self.transport.name}"

    @property
    def capabilities(self) -> frozenset[KnowledgeCapability]:
        if self.allow_writes and self.transport.writable:
            return _READ_CAPABILITIES | _WRITE_CAPABILITIES
        return _READ_CAPABILITIES

    async def status(self) -> ProviderStatus:
        connected = True
        detail: str
        note_count = 0
        try:
            info = self.transport.check()
            note_count = info.note_count
            detail = (
                f"{info.note_count} notes in {info.folder_count} folders"
                + ("" if info.has_obsidian_config
                   else " (no .obsidian/ — readable as a Markdown folder, but "
                        "Obsidian has not opened it)")
            )
            self._last_error = None
        except VaultError as exc:
            connected = False
            detail = exc.user_message
            self._last_error = exc.user_message

        return ProviderStatus(
            key=self.key,
            kind=self.kind,
            name=self.name,
            connected=connected,
            capabilities=sorted(self.capabilities, key=lambda c: c.value),
            detail=detail,
            document_count=self.document_count or note_count,
            last_synced_at=self.last_synced_at,
            last_error=self._last_error,
        )

    # ── reading ──────────────────────────────────────────────────────────────

    async def list_items(
        self, *, prefix: str | None = None, limit: int = 200
    ) -> list[KnowledgeItem]:
        return [
            self._meta_to_item(meta)
            for meta in self.transport.list_notes(prefix=prefix, limit=limit)
        ]

    async def list_folders(self) -> list[str]:
        """§8's ``list_folders``. Not on the base interface because only
        hierarchical providers have folders; the contract maps it onto
        ``list_items`` for callers that want one method."""
        return self.transport.list_folders()

    async def read(self, item_id: str) -> KnowledgeItem:
        return self._note_to_item(self.transport.read(item_id))

    async def metadata(self, item_id: str) -> dict[str, Any]:
        note = self.transport.read(item_id)
        return {
            "path": note.path,
            "title": note.title,
            "folder": note.folder,
            "frontmatter": json_safe(note.frontmatter),
            "tags": note.tags,
            "aliases": note.aliases,
            "links": note.links,
            "bytes": note.byte_size,
            "modified_at": note.modified_at.isoformat() if note.modified_at else None,
            "content_hash": note.content_hash,
        }

    async def links(self, item_id: str) -> dict[str, list[str]]:
        note = self.transport.read(item_id)
        return {"links": note.links, "backlinks": self.transport.backlinks(item_id)}

    async def search(
        self, query: str, *, limit: int = 20, **filters: Any
    ) -> list[KnowledgeSearchHit]:
        rows = self.transport.search(
            query,
            limit=limit,
            tag=filters.get("tag"),
            folder=filters.get("folder"),
            titles_only=bool(filters.get("titles_only")),
        )
        return [
            KnowledgeSearchHit(
                item=self._meta_to_item(meta), score=score, excerpt=excerpt or None
            )
            for meta, score, excerpt in rows
        ]

    async def iter_ingestable(self, *, limit: int = 1000) -> Sequence[KnowledgeItem]:
        """Notes offered to the ingestion pipeline.

        Metadata only — the pipeline reads the body when it decides a note
        needs re-chunking, and offering 5,000 note bodies to decide that would
        make incremental indexing pointless.
        """
        return [
            self._meta_to_item(meta)
            for meta in self.transport.list_notes(limit=limit)
        ]

    # ── writing ──────────────────────────────────────────────────────────────

    async def create(
        self, *, title: str, content: str, path: str | None = None, **metadata: Any
    ) -> KnowledgeItem:
        self._require(KnowledgeCapability.CREATE)
        note_path = path or f"{_safe_filename(title)}.md"
        if not note_path.lower().endswith((".md", ".markdown")):
            note_path = f"{note_path}.md"

        frontmatter = dict(metadata.get("frontmatter") or {})
        frontmatter.setdefault("title", title)
        # Stamped so a note JARVIS wrote is recognisable on the way back in —
        # the contract's "jarvis-id is how a JARVIS-authored note is
        # recognised". Without it, sync cannot tell its own writes from the
        # user's edits, and every push looks like a conflict.
        frontmatter.setdefault("jarvis-created", _now_iso())
        if metadata.get("project"):
            frontmatter.setdefault("jarvis-project", metadata["project"])
        if metadata.get("tags"):
            frontmatter.setdefault("tags", list(metadata["tags"]))

        note = self.transport.create(
            note_path, content, frontmatter=frontmatter,
            overwrite=bool(metadata.get("overwrite")),
        )
        return self._note_to_item(note)

    async def update(
        self, item_id: str, *, content: str, **metadata: Any
    ) -> KnowledgeItem:
        self._require(KnowledgeCapability.UPDATE)
        note = self.transport.update(
            item_id,
            content=content if metadata.get("mode") != "append" else None,
            append=content if metadata.get("mode") == "append" else None,
            section=metadata.get("section"),
            frontmatter=metadata.get("frontmatter"),
            expected_hash=metadata.get("expected_hash"),
        )
        return self._note_to_item(note)

    async def delete(self, item_id: str) -> None:
        self._require(KnowledgeCapability.DELETE)
        self.transport.delete(item_id)

    async def move(self, item_id: str, new_path: str) -> KnowledgeItem:
        self._require(KnowledgeCapability.MOVE)
        return self._note_to_item(self.transport.move(item_id, new_path))

    # ── conversion ───────────────────────────────────────────────────────────

    def _require(self, capability: KnowledgeCapability) -> None:
        if capability not in self.capabilities:
            self._unsupported(capability)

    def source_ref(self, note: Note) -> SourceRef:
        """Provenance for one note (§11).

        Built from :class:`ObsidianRef`, which Phase 2 put in the schema for
        exactly this. Every field it carries is populated from the file rather
        than defaulted, so "where did you get that?" answers with a real vault
        and a real path.
        """
        return SourceRef.from_obsidian(
            ObsidianRef(
                vault_id=self.vault_id,
                vault_name=self.transport.name,
                vault_path=str(self.transport.root),
                note_path=note.path,
                note_title=note.title,
                note_id=note.path,
                # Frontmatter crosses into a JSON column here, so dates and
                # other YAML scalars are flattened on the way.
                frontmatter=json_safe(note.frontmatter),
                tags=note.tags,
                links=note.links,
                content_hash=note.content_hash,
                last_synced_at=datetime.now(timezone.utc),
                sync_status=SyncStatus.SYNCED,
            )
        )

    def _meta_to_item(self, meta: NoteMeta) -> KnowledgeItem:
        return KnowledgeItem(
            id=meta.path,
            title=meta.title,
            ref=SourceRef.from_obsidian(
                ObsidianRef(
                    vault_id=self.vault_id,
                    vault_name=self.transport.name,
                    vault_path=str(self.transport.root),
                    note_path=meta.path,
                    note_title=meta.title,
                    note_id=meta.path,
                )
            ),
            media_type="text/markdown",
            byte_size=meta.byte_size,
            modified_at=meta.modified_at,
            metadata={"folder": str(meta.path.rsplit("/", 1)[0]) if "/" in meta.path else ""},
        )

    def _note_to_item(self, note: Note) -> KnowledgeItem:
        return KnowledgeItem(
            id=note.path,
            title=note.title,
            ref=self.source_ref(note),
            # The raw file, frontmatter included: §8 says preserve Markdown,
            # and a caller reading a note to edit it needs the bytes that are
            # actually on disk.
            content=note.raw,
            media_type="text/markdown",
            byte_size=note.byte_size,
            modified_at=note.modified_at,
            tags=note.tags,
            metadata={
                "folder": note.folder,
                "frontmatter": json_safe(note.frontmatter),
                "aliases": note.aliases,
                "links": note.links,
                "content_hash": note.content_hash,
            },
        )


def _safe_filename(title: str) -> str:
    """A title turned into a filename Obsidian will accept.

    The characters removed are those Obsidian itself forbids in note names
    (they break wikilink resolution) plus the path separators, because a title
    must never become a directory traversal.
    """
    cleaned = "".join(
        "-" if ch in '\\/:*?"<>|#^[]' else ch for ch in title.strip()
    )
    cleaned = " ".join(cleaned.split())
    # Leading dots stripped after the separators are gone: "../../escaped"
    # becomes "..-..-escaped", which cannot traverse but is a strange filename
    # and a hidden one if it starts with a dot.
    cleaned = cleaned.lstrip(".-").replace("..", "")
    return cleaned.strip("-").strip()[:120] or "Untitled"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
