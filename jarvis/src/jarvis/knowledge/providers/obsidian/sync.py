"""Incremental indexing and conflict handling (§10, §12, §23, §24).

    OBSIDIAN NOTE → INGESTION → CHUNKING → METADATA → INDEX → JARVIS RETRIEVAL

The pipeline is the existing one. What this module adds is the two things a
vault needs that an upload does not: knowing *which* notes changed, and knowing
what to do when both sides changed the same one.

## Incremental (§12)

A full re-index of a 5,000-note vault is minutes of embedding calls, so it must
not be what "sync" means. Three cheap signals, in increasing cost:

1. **Modification time** against the last sync — skips the vast majority
   without opening a file.
2. **Content hash** against the stored hash — catches a touched-but-unchanged
   file and a file whose mtime is unreliable (network shares, restored
   backups, some sync clients).
3. Only then, ingestion.

Deletions are found by the opposite walk: documents indexed from this vault
whose note no longer exists.

## Conflicts (§24)

Three hashes tell the whole story for a note:

* ``base`` — what the note contained when JARVIS last synced it.
* ``vault`` — what it contains now.
* ``local`` — what JARVIS wants it to contain, recorded when a write was
  prepared but not applied.

``vault != base`` alone is an ordinary modification: the user edited their
note, and JARVIS re-indexes. ``local != base`` alone is an ordinary push.
**Both** is a conflict, and the resolution is the user's — never a silent
overwrite in either direction.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from jarvis.db.base import utcnow
from jarvis.db.models import Document
from jarvis.knowledge.ingestion.pipeline import IngestRequest, IngestionPipeline
from jarvis.knowledge.providers.obsidian.provider import ObsidianProvider
from jarvis.knowledge.providers.obsidian.vault import Note, content_hash
from jarvis.knowledge.types import DocumentStatus, SourceKind
from jarvis.logging import get_logger

log = get_logger(__name__)

#: How a vault note is addressed as a document. Stable, unique per vault, and
#: readable in a log line — which matters, because this string is what a user
#: sees when they ask where a retrieved fragment came from.
URI_SCHEME = "obsidian"


def document_uri(vault_name: str, note_path: str) -> str:
    return f"{URI_SCHEME}://{vault_name}/{note_path}"


class Resolution(str):
    """Conflict resolutions offered by §24."""

    KEEP_OBSIDIAN = "keep_obsidian"
    KEEP_JARVIS = "keep_jarvis"
    MERGE = "merge"
    CANCEL = "cancel"


RESOLUTIONS = (
    Resolution.KEEP_OBSIDIAN,
    Resolution.KEEP_JARVIS,
    Resolution.MERGE,
    Resolution.CANCEL,
)


@dataclass(slots=True)
class NoteChange:
    path: str
    change: str  # NEW | MODIFIED | DELETED | UNCHANGED | CONFLICT
    document_id: str | None = None
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "change": self.change,
            "document_id": self.document_id,
            "reason": self.reason,
        }


@dataclass(slots=True)
class SyncPlan:
    """What a sync *would* do. Produced before anything is written, so the UI
    can show it and a dry run is a real option rather than a comment."""

    new: list[NoteChange] = field(default_factory=list)
    modified: list[NoteChange] = field(default_factory=list)
    deleted: list[NoteChange] = field(default_factory=list)
    unchanged: list[NoteChange] = field(default_factory=list)
    conflicts: list[NoteChange] = field(default_factory=list)

    @property
    def actionable(self) -> int:
        return len(self.new) + len(self.modified) + len(self.deleted)

    def to_dict(self) -> dict[str, Any]:
        return {
            "new": [c.to_dict() for c in self.new],
            "modified": [c.to_dict() for c in self.modified],
            "deleted": [c.to_dict() for c in self.deleted],
            "conflicts": [c.to_dict() for c in self.conflicts],
            "unchanged_count": len(self.unchanged),
            "actionable": self.actionable,
        }


@dataclass(slots=True)
class SyncResult:
    indexed: int = 0
    updated: int = 0
    removed: int = 0
    skipped: int = 0
    failed: int = 0
    conflicts: list[NoteChange] = field(default_factory=list)
    chunks: int = 0
    embedded: int = 0
    warnings: list[str] = field(default_factory=list)
    duration_ms: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "indexed": self.indexed,
            "updated": self.updated,
            "removed": self.removed,
            "skipped": self.skipped,
            "failed": self.failed,
            "conflicts": [c.to_dict() for c in self.conflicts],
            "chunks": self.chunks,
            "embedded": self.embedded,
            "warnings": self.warnings,
            "duration_ms": round(self.duration_ms, 1),
        }


class ObsidianSync:
    """Pulls a vault into the knowledge index, incrementally."""

    def __init__(
        self,
        session: AsyncSession,
        provider: ObsidianProvider,
        *,
        user_id: str,
        pipeline: IngestionPipeline,
        project_id: str | None = None,
    ) -> None:
        self.session = session
        self.provider = provider
        self.user_id = user_id
        self.pipeline = pipeline
        self.project_id = project_id

    # ── planning ─────────────────────────────────────────────────────────────

    async def plan(self, *, limit: int = 5_000) -> SyncPlan:
        """Decide what changed, without writing anything."""
        plan = SyncPlan()
        indexed = await self._indexed_documents()
        seen: set[str] = set()

        for meta in self.provider.transport.list_notes(limit=limit):
            seen.add(meta.path)
            document = indexed.get(meta.path)

            if document is None:
                plan.new.append(NoteChange(meta.path, "NEW", reason="not indexed"))
                continue

            state = _obsidian_meta(document)
            base = state.get("base_hash") or document.content_hash
            local = state.get("local_hash")

            # Cheapest signal first: an unchanged mtime means an unchanged
            # file often enough to be worth checking before opening it.
            last_seen = state.get("modified_at")
            if (
                last_seen
                and meta.modified_at.isoformat() == last_seen
                and document.status is DocumentStatus.INDEXED
                and not local
            ):
                plan.unchanged.append(
                    NoteChange(meta.path, "UNCHANGED", document.id, "mtime unchanged")
                )
                continue

            try:
                note = self.provider.transport.read(meta.path)
            except Exception as exc:  # unreadable note: report, do not crash
                plan.modified.append(
                    NoteChange(meta.path, "MODIFIED", document.id, f"unreadable: {exc}")
                )
                continue

            vault_changed = note.content_hash != base
            local_changed = bool(local) and local != base

            if vault_changed and local_changed:
                plan.conflicts.append(
                    NoteChange(
                        meta.path, "CONFLICT", document.id,
                        "changed in Obsidian and in JARVIS since the last sync",
                    )
                )
            elif vault_changed:
                plan.modified.append(
                    NoteChange(meta.path, "MODIFIED", document.id, "content hash differs")
                )
            else:
                plan.unchanged.append(
                    NoteChange(meta.path, "UNCHANGED", document.id, "hash unchanged")
                )

        for path, document in indexed.items():
            if path not in seen:
                plan.deleted.append(
                    NoteChange(path, "DELETED", document.id, "note no longer in vault")
                )

        return plan

    # ── execution ────────────────────────────────────────────────────────────

    async def pull(
        self, *, limit: int = 5_000, remove_deleted: bool = True
    ) -> SyncResult:
        """Apply the plan. Obsidian → JARVIS only.

        Pull is the only direction a sync runs automatically. Writing back
        happens through an explicit, permission-checked, audited operation —
        §23 is unambiguous that unrestricted bidirectional sync is not to be
        built, and the reason is that it is how people lose hand-written notes.
        """
        from jarvis.logging import timed

        result = SyncResult()
        with timed() as clock:
            plan = await self.plan(limit=limit)
            result.conflicts = plan.conflicts

            for change in [*plan.new, *plan.modified]:
                try:
                    outcome = await self.index_note(change.path)
                except Exception as exc:
                    result.failed += 1
                    result.warnings.append(f"{change.path}: {exc}")
                    log.warning("obsidian_note_index_failed",
                                path=change.path, error=str(exc))
                    continue
                if outcome["skipped"]:
                    result.skipped += 1
                elif change.change == "NEW":
                    result.indexed += 1
                else:
                    result.updated += 1
                result.chunks += outcome["chunks"]
                result.embedded += outcome["embedded"]

            result.skipped += len(plan.unchanged)

            if remove_deleted:
                for change in plan.deleted:
                    document = await self.session.get(Document, change.document_id)
                    if document is not None:
                        await self.pipeline.delete_document(document)
                        result.removed += 1

        result.duration_ms = clock.duration_ms
        log.info(
            "obsidian_sync_completed",
            vault=self.provider.transport.name,
            indexed=result.indexed, updated=result.updated,
            removed=result.removed, skipped=result.skipped,
            conflicts=len(result.conflicts), duration_ms=round(result.duration_ms, 1),
        )
        return result

    async def index_note(self, note_path: str) -> dict[str, Any]:
        """Ingest one note, with full provenance."""
        note = self.provider.transport.read(note_path)
        ref = self.provider.source_ref(note)

        outcome = await self.pipeline.ingest(
            IngestRequest(
                user_id=self.user_id,
                # The filename decides the loader; a note is Markdown.
                filename=note_path.rsplit("/", 1)[-1] or "note.md",
                data=note.raw.encode("utf-8"),
                source_kind=SourceKind.OBSIDIAN,
                source_ref=ref,
                media_type="text/markdown",
                project_id=await self._project_for(note),
                tags=list(note.tags),
                title=note.title,
            )
        )

        document = outcome.document
        # The pipeline addresses documents by ``source_ref.locator``, which for
        # a note is its vault-relative path. Two vaults could hold the same
        # path, so the URI is namespaced by vault here.
        document.uri = document_uri(self.provider.transport.name, note.path)
        document.meta = {
            **(document.meta or {}),
            "obsidian": {
                "vault": self.provider.transport.name,
                "note_path": note.path,
                "base_hash": note.content_hash,
                "local_hash": None,
                "modified_at": note.modified_at.isoformat() if note.modified_at else None,
                "synced_at": utcnow().isoformat(),
                "tags": note.tags,
                "aliases": note.aliases,
                "links": note.links,
            },
        }
        await self.session.flush()

        return {
            "document_id": document.id,
            "chunks": outcome.chunks_created,
            "embedded": outcome.chunks_embedded,
            "skipped": outcome.skipped_unchanged,
        }

    async def forget_note(self, note_path: str) -> bool:
        """Drop a note from the index. Does **not** touch the vault."""
        document = (await self._indexed_documents()).get(note_path)
        if document is None:
            return False
        await self.pipeline.delete_document(document)
        return True

    # ── conflicts (§24) ──────────────────────────────────────────────────────

    async def record_local_change(self, note_path: str, content: str) -> None:
        """Remember that JARVIS intends a note to say something else.

        Called when a write is prepared but not applied — a refused write, or
        one waiting on confirmation. It is what makes a later conflict
        detectable: without it, JARVIS's side of the disagreement is not
        recorded anywhere and the conflict silently resolves to "whatever the
        vault says".
        """
        document = (await self._indexed_documents()).get(note_path)
        if document is None:
            return
        state = _obsidian_meta(document)
        state["local_hash"] = content_hash(content)
        state["local_content"] = content
        document.meta = {**(document.meta or {}), "obsidian": state}
        await self.session.flush()

    async def conflict(self, note_path: str) -> dict[str, Any] | None:
        """The two versions, side by side, for the user to choose between."""
        document = (await self._indexed_documents()).get(note_path)
        if document is None:
            return None
        state = _obsidian_meta(document)
        base = state.get("base_hash")
        local = state.get("local_hash")
        if not local or local == base:
            return None

        note = self.provider.transport.read(note_path)
        if note.content_hash == base:
            return None

        return {
            "note_path": note_path,
            "document_id": document.id,
            "obsidian": {
                "content": note.raw,
                "hash": note.content_hash,
                "modified_at": note.modified_at.isoformat() if note.modified_at else None,
            },
            "jarvis": {
                "content": state.get("local_content", ""),
                "hash": local,
            },
            "base_hash": base,
            "resolutions": list(RESOLUTIONS),
        }

    async def resolve(self, note_path: str, resolution: str) -> dict[str, Any]:
        """Apply the user's decision. Never chooses on their behalf."""
        detail = await self.conflict(note_path)
        if detail is None:
            return {"note_path": note_path, "resolved": False,
                    "detail": "There is no conflict on that note."}

        if resolution == Resolution.CANCEL:
            await self._clear_local(note_path)
            return {"note_path": note_path, "resolved": True,
                    "resolution": resolution,
                    "detail": "Kept the note as it is in Obsidian and discarded "
                              "JARVIS's pending version."}

        if resolution == Resolution.KEEP_OBSIDIAN:
            await self._clear_local(note_path)
            await self.index_note(note_path)
            return {"note_path": note_path, "resolved": True,
                    "resolution": resolution,
                    "detail": "Obsidian's version kept and re-indexed."}

        if resolution == Resolution.KEEP_JARVIS:
            self.provider.transport.update(
                note_path, content=_strip_frontmatter(detail["jarvis"]["content"])
            )
            await self._clear_local(note_path)
            await self.index_note(note_path)
            return {"note_path": note_path, "resolved": True,
                    "resolution": resolution,
                    "detail": "JARVIS's version written to the vault."}

        if resolution == Resolution.MERGE:
            # Both versions are preserved verbatim under headings, with the
            # Obsidian one first. A silent three-way merge of prose is a good
            # way to produce a document neither side wrote; keeping both and
            # marking them is the honest operation, and the user finishes it
            # in Obsidian where editing is what they are already doing.
            merged = (
                _strip_frontmatter(detail["obsidian"]["content"]).rstrip("\n")
                + "\n\n---\n\n## Merged from JARVIS\n\n"
                + _strip_frontmatter(detail["jarvis"]["content"]).strip()
                + "\n"
            )
            self.provider.transport.update(note_path, content=merged)
            await self._clear_local(note_path)
            await self.index_note(note_path)
            return {"note_path": note_path, "resolved": True,
                    "resolution": resolution,
                    "detail": "Both versions kept in the note, JARVIS's under a "
                              "'Merged from JARVIS' heading for you to review."}

        raise ValueError(f"Unknown resolution {resolution!r}")

    # ── internals ────────────────────────────────────────────────────────────

    async def _clear_local(self, note_path: str) -> None:
        document = (await self._indexed_documents()).get(note_path)
        if document is None:
            return
        state = _obsidian_meta(document)
        state["local_hash"] = None
        state.pop("local_content", None)
        document.meta = {**(document.meta or {}), "obsidian": state}
        await self.session.flush()

    async def _indexed_documents(self) -> dict[str, Document]:
        """Documents this vault has produced, keyed by note path."""
        prefix = f"{URI_SCHEME}://{self.provider.transport.name}/"
        rows = (
            await self.session.execute(
                select(Document).where(
                    Document.user_id == self.user_id,
                    Document.source_kind == SourceKind.OBSIDIAN,
                    Document.uri.startswith(prefix),
                )
            )
        ).scalars().all()
        return {str(d.uri)[len(prefix):]: d for d in rows if d.uri}

    async def _project_for(self, note: Note) -> str | None:
        """``jarvis-project:`` in frontmatter wins over the sync's default.

        §17 wants a project to reference notes; this is the other direction —
        a note declaring which project it belongs to. Frontmatter is the join
        key the Phase 2 contract chose, and honouring it here is what makes
        that choice real.

        The declared value is a name or key the *user* wrote, so it is
        resolved through :class:`ProjectService` rather than used as an id. It
        is worth being blunt about why: ``project_id`` is a foreign key, and
        writing an arbitrary string from a note's frontmatter into it makes
        every note with a typo — or a deliberately crafted one — a failed
        insert. A name that matches nothing is ignored, not invented.
        """
        declared = note.frontmatter.get("jarvis-project")
        if not declared:
            return self.project_id

        from jarvis.memory.projects import ProjectService

        project = await ProjectService(self.session).resolve(
            self.user_id, str(declared)
        )
        if project is None:
            log.debug(
                "obsidian_unknown_project_in_frontmatter",
                note=note.path, declared=str(declared),
            )
            return self.project_id
        return project.id


def _obsidian_meta(document: Document) -> dict[str, Any]:
    raw = (document.meta or {}).get("obsidian")
    return dict(raw) if isinstance(raw, dict) else {}


def _strip_frontmatter(raw: str) -> str:
    from jarvis.knowledge.providers.obsidian.vault import split_frontmatter

    return split_frontmatter(raw)[1]


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
