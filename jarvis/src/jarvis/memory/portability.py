"""Memory export and import (§36, §37).

The format is JSON with a Markdown companion, and the reasoning is §37's: the
memory archive must not be a proprietary blob. If JARVIS stops existing, the
export should still be readable — which means the JSON has to be obvious enough
to hand-edit and the Markdown has to be readable with no tooling at all.

**Export** is lossless: every field needed to reconstruct a memory, including
provenance and revision history. Vectors are deliberately *not* exported. They
are derived data, they are large, and they are only valid for the model that
produced them — an import into an installation with a different embedding
provider would carry vectors that silently never match. Re-embedding on import
is cheap and always correct.

**Import** re-runs the secret guard. An archive is untrusted input like any
other file: it may have been edited, generated elsewhere, or handed over by
someone else.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable

from sqlalchemy.ext.asyncio import AsyncSession

from jarvis.db.models import Memory
from jarvis.errors import ValidationError
from jarvis.knowledge.types import SourceRef
from jarvis.logging import get_logger
from jarvis.memory.service import MemoryDraft, MemoryFilter, MemoryService
from jarvis.memory.types import (
    MemorySource,
    MemoryStatus,
    MemoryType,
    confidence_band,
    importance_band,
)

log = get_logger(__name__)

EXPORT_VERSION = 1


@dataclass(slots=True)
class ImportReport:
    created: int = 0
    merged: int = 0
    superseded: int = 0
    skipped: int = 0
    refused: int = 0
    errors: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.errors is None:
            self.errors = []

    def to_dict(self) -> dict[str, Any]:
        return {
            "created": self.created,
            "merged": self.merged,
            "superseded": self.superseded,
            "skipped": self.skipped,
            "refused": self.refused,
            "errors": self.errors[:50],
        }


def memory_to_export(memory: Memory, history: list[dict[str, Any]] | None = None) -> dict[str, Any]:
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
        "source_ref": memory.source_ref,
        "tainted": memory.tainted,
        "confidence": memory.confidence,
        "importance": memory.importance,
        "pinned": memory.pinned,
        "project_id": memory.project_id,
        "conversation_id": memory.conversation_id,
        "task_id": memory.task_id,
        "tags": memory.tags,
        "metadata": memory.meta,
        "revision": memory.revision,
        "created_at": memory.created_at.isoformat() if memory.created_at else None,
        "updated_at": memory.updated_at.isoformat() if memory.updated_at else None,
        "expires_at": memory.expires_at.isoformat() if memory.expires_at else None,
        "history": history or [],
    }


async def export_memories(
    session: AsyncSession,
    user_id: str,
    *,
    project_id: str | None = None,
    include_archived: bool = False,
    include_history: bool = True,
) -> dict[str, Any]:
    service = MemoryService(session)
    statuses = None if include_archived else [MemoryStatus.ACTIVE]
    memories = await service.search(
        user_id,
        MemoryFilter(
            statuses=statuses,
            project_id=project_id,
            include_expired=True,
            limit=500,
        ),
    )

    rows: list[dict[str, Any]] = []
    for memory in memories:
        history: list[dict[str, Any]] = []
        if include_history:
            history = [
                {
                    "kind": rev.kind.value if hasattr(rev.kind, "value") else rev.kind,
                    "actor": rev.actor,
                    "changes": rev.changes,
                    "note": rev.note,
                    "at": rev.created_at.isoformat() if rev.created_at else None,
                }
                for rev in await service.history(memory.id)
            ]
        rows.append(memory_to_export(memory, history))

    return {
        "format": "jarvis.memory",
        "version": EXPORT_VERSION,
        "exported_at": datetime.now().astimezone().isoformat(),
        "project_id": project_id,
        # Named so an importer knows what it is missing rather than guessing.
        "excludes": ["embeddings"],
        "count": len(rows),
        "memories": rows,
    }


def to_markdown(archive: dict[str, Any]) -> str:
    """Human-readable companion (§36).

    Grouped by type, because a flat list of 300 memories is a wall. Confidence
    and importance are rendered as bands rather than numbers — nobody reading
    their own memory dump wants to interpret 0.63.
    """
    lines = [
        "# JARVIS memory export",
        "",
        f"Exported {archive.get('exported_at', 'unknown')} — "
        f"{archive.get('count', 0)} memories.",
        "",
        "Vectors are not included; they are regenerated on import.",
        "",
    ]

    by_type: dict[str, list[dict[str, Any]]] = {}
    for row in archive.get("memories", []):
        by_type.setdefault(row.get("type", "UNKNOWN"), []).append(row)

    for type_name in sorted(by_type):
        lines.append(f"## {type_name.replace('_', ' ').title()}")
        lines.append("")
        for row in by_type[type_name]:
            confidence = confidence_band(row.get("confidence", 0.5)).value
            importance = importance_band(row.get("importance", 0.5)).value
            lines.append(f"- {row.get('content', '')}")
            details = [f"confidence {confidence.lower()}",
                       f"importance {importance.lower()}"]
            if row.get("tags"):
                details.append("tags: " + ", ".join(row["tags"]))
            ref = SourceRef.from_dict(row.get("source_ref"))
            if ref:
                details.append(f"source: {ref.describe()}")
            elif row.get("source"):
                details.append(f"source: {row['source'].lower()}")
            if row.get("created_at"):
                details.append(f"since {row['created_at'][:10]}")
            lines.append(f"  <sub>{' · '.join(details)}</sub>")
        lines.append("")

    return "\n".join(lines)


async def import_memories(
    session: AsyncSession,
    user_id: str,
    archive: dict[str, Any],
    *,
    embeddings: Any = None,
    actor: str = "user",
    reconcile: bool = False,
) -> ImportReport:
    """Restore an archive.

    ``reconcile`` defaults to false: an archive is a coherent set the user
    expects back as-is, and running dedup across it would quietly drop rows
    that look similar. Turn it on when merging an archive into a store that
    already has content.
    """
    if archive.get("format") != "jarvis.memory":
        raise ValidationError(
            "Not a JARVIS memory archive",
            user_message="That file is not a JARVIS memory export.",
        )
    version = archive.get("version")
    if version != EXPORT_VERSION:
        raise ValidationError(
            f"Unsupported archive version {version}",
            user_message=(
                f"That archive is version {version}; this build reads version "
                f"{EXPORT_VERSION}."
            ),
        )

    service = MemoryService(session, embeddings=embeddings)
    report = ImportReport()

    for row in archive.get("memories", []):
        try:
            draft = _draft_from_row(row)
        except Exception as exc:
            report.skipped += 1
            report.errors.append(f"malformed row: {exc}")
            continue

        outcome = await service.create(
            user_id, draft, actor=actor, reconcile=reconcile
        )
        if outcome.action == "created":
            report.created += 1
        elif outcome.action == "merged":
            report.merged += 1
        elif outcome.action == "superseded":
            report.superseded += 1
        elif outcome.action == "refused":
            report.refused += 1
            report.errors.append(f"refused: {outcome.detail}")

    log.info("memory_import", **report.to_dict())
    return report


def _draft_from_row(row: dict[str, Any]) -> MemoryDraft:
    content = (row.get("content") or "").strip()
    if not content:
        raise ValueError("empty content")

    return MemoryDraft(
        content=content,
        type=MemoryType(row.get("type", MemoryType.USER_FACT.value)),
        subject=row.get("subject"),
        summary=row.get("summary"),
        source=MemorySource(row.get("source", MemorySource.USER.value)),
        source_ref=SourceRef.from_dict(row.get("source_ref")),
        confidence=float(row.get("confidence", 0.55)),
        importance=float(row["importance"]) if row.get("importance") is not None else None,
        tags=list(row.get("tags") or []),
        meta=dict(row.get("metadata") or {}),
        project_id=row.get("project_id"),
        pinned=bool(row.get("pinned", False)),
        tainted=bool(row.get("tainted", False)),
        # Imported archives arrive ACTIVE unless they were archived; a
        # PROPOSED memory from another installation has no pending prompt here.
        status=(
            MemoryStatus.ARCHIVED
            if row.get("status") == MemoryStatus.ARCHIVED.value
            else MemoryStatus.ACTIVE
        ),
    )


def dumps(archive: dict[str, Any]) -> str:
    return json.dumps(archive, indent=2, ensure_ascii=False)
