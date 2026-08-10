"""Obsidian endpoints (§5, §6, §7).

Everything the Obsidian panel renders and every operation it can start. The
shape follows the Phase 1 conventions exactly — bearer auth on every route,
the user resolved before any read or write, structured errors — and the shape
follows Phase 3's computer routes for the operations that can change something:
authorise first, act second, audit either way.

The one route worth reading carefully is ``/status``. §7 says do not fake any
status, and the way this avoids it is that ``connected`` is never remembered —
it is the result of walking the vault directory at the moment of the request.
A vault on a disconnected network drive reports DISCONNECTED with the reason,
not CONNECTED because it worked yesterday.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field

from jarvis.api.deps import AuthDep, CoreDep, SessionDep, UserDep
from jarvis.errors import ConfirmationRequiredError, JarvisError
from jarvis.knowledge.ingestion.pipeline import IngestionPipeline
from jarvis.knowledge.providers.obsidian import (
    ObsidianService,
    ObsidianSync,
    VaultError,
    discover,
)
from jarvis.knowledge.providers.obsidian.sync import RESOLUTIONS
from jarvis.knowledge.providers.obsidian.vault import ConflictError
from jarvis.logging import get_logger

log = get_logger(__name__)

obsidian_router = APIRouter(prefix="/obsidian", tags=["obsidian"])


class ConnectRequest(BaseModel):
    vault_path: str = Field(min_length=1, max_length=4096)
    vault_name: str | None = Field(default=None, max_length=200)
    allow_writes: bool = False
    allow_deletes: bool = False


class NoteRequest(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    content: str = Field(max_length=1_000_000)
    path: str | None = Field(default=None, max_length=1024)
    project: str | None = None
    tags: list[str] = Field(default_factory=list)


class UpdateRequest(BaseModel):
    content: str = Field(max_length=1_000_000)
    #: ``append`` adds to the end, ``section`` replaces one heading's block,
    #: ``replace`` rewrites the note. Only the last is destructive, and it is
    #: the only one that needs a confirmation.
    mode: str = Field(default="append", pattern="^(append|section|replace)$")
    section: str | None = Field(default=None, max_length=200)
    expected_hash: str | None = None


class ResolveRequest(BaseModel):
    resolution: str = Field(pattern="^(keep_obsidian|keep_jarvis|merge|cancel)$")


def _service(core: Any, session: Any, user_id: str) -> ObsidianService:
    from jarvis.activity.service import ActivityService
    from jarvis.confirmations.service import ConfirmationService

    return ObsidianService(
        session,
        user_id,
        activity=ActivityService(session, core.activity_bus),
        confirmations=ConfirmationService(session),
    )


def _syncer(core: Any, session: Any, user_id: str, provider: Any) -> ObsidianSync:
    return ObsidianSync(
        session,
        provider,
        user_id=user_id,
        pipeline=IngestionPipeline(
            session,
            embeddings=core.embeddings,
            chunk_target_chars=core.settings.ingest_chunk_target_chars,
            chunk_overlap_chars=core.settings.ingest_chunk_overlap_chars,
        ),
    )


# ── discovery and connection (§5, §6) ────────────────────────────────────────


@obsidian_router.get("/discover")
async def discover_vaults(_: AuthDep) -> dict[str, Any]:
    """What is actually on this machine. Never guesses a path."""
    return discover().to_dict()


@obsidian_router.get("/status")
async def obsidian_status(
    core: CoreDep, session: SessionDep, user: UserDep, _: AuthDep
) -> dict[str, Any]:
    """Everything §7's panel renders, all of it observed now."""
    service = _service(core, session, user.id)
    config = await service.config()
    provider = await service.provider()

    payload: dict[str, Any] = {
        "implemented": True,
        "configured": bool(config.vault_path),
        "connected": False,
        "state": "DISCONNECTED",
        "config": config.to_dict(),
        "capabilities": [],
        "vault": None,
        "detail": "No vault is configured.",
    }

    if provider is None:
        return payload

    status_report = await provider.status()
    payload["connected"] = status_report.connected
    payload["state"] = "CONNECTED" if status_report.connected else "ERROR"
    payload["detail"] = status_report.detail
    payload["capabilities"] = [c.value for c in status_report.capabilities]
    payload["vault"] = {
        "name": provider.transport.name,
        "writable": provider.transport.writable,
    }
    payload["config"]["indexed_notes"] = status_report.document_count
    if status_report.last_error:
        payload["state"] = "ERROR"
        payload["config"]["last_error"] = status_report.last_error
    return payload


@obsidian_router.post("/connect")
async def connect(
    body: ConnectRequest,
    core: CoreDep,
    session: SessionDep,
    user: UserDep,
    _: AuthDep,
) -> dict[str, Any]:
    service = _service(core, session, user.id)
    try:
        result = await service.connect(
            body.vault_path,
            vault_name=body.vault_name,
            allow_writes=body.allow_writes,
            allow_deletes=body.allow_deletes,
        )
    except VaultError:
        await session.commit()
        raise
    await session.commit()
    return result


@obsidian_router.post("/test")
async def test_connection(
    core: CoreDep, session: SessionDep, user: UserDep, _: AuthDep
) -> dict[str, Any]:
    service = _service(core, session, user.id)
    result = await service.test()
    await session.commit()
    return result


@obsidian_router.post("/disconnect")
async def disconnect(
    core: CoreDep,
    session: SessionDep,
    user: UserDep,
    _: AuthDep,
    forget_index: bool = False,
) -> dict[str, Any]:
    service = _service(core, session, user.id)
    result = await service.disconnect(forget_index=forget_index)
    await session.commit()
    return result


# ── reading (§8) ─────────────────────────────────────────────────────────────


@obsidian_router.get("/notes")
async def list_notes(
    core: CoreDep,
    session: SessionDep,
    user: UserDep,
    _: AuthDep,
    prefix: str | None = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 200,
) -> dict[str, Any]:
    service = _service(core, session, user.id)
    provider = await service.require_provider()
    await service.guard("list")
    items = await provider.list_items(prefix=prefix, limit=limit)
    await service.audit(
        "list", status="OK", summary=f"Listed {len(items)} Obsidian notes",
        detail={"count": len(items), "prefix": prefix},
    )
    await session.commit()
    return {
        "notes": [
            {
                "path": item.id,
                "title": item.title,
                "folder": item.metadata.get("folder", ""),
                "bytes": item.byte_size,
                "modified_at": item.modified_at.isoformat() if item.modified_at else None,
            }
            for item in items
        ],
        "count": len(items),
    }


@obsidian_router.get("/folders")
async def list_folders(
    core: CoreDep, session: SessionDep, user: UserDep, _: AuthDep
) -> dict[str, Any]:
    service = _service(core, session, user.id)
    provider = await service.require_provider()
    await service.guard("list")
    folders = await provider.list_folders()
    await session.commit()
    return {"folders": folders, "count": len(folders)}


@obsidian_router.get("/note")
async def read_note(
    core: CoreDep,
    session: SessionDep,
    user: UserDep,
    _: AuthDep,
    path: Annotated[str, Query(min_length=1, max_length=1024)],
) -> dict[str, Any]:
    """Read one note.

    ``path`` is a query parameter rather than a path segment on purpose: a
    vault path contains slashes, and encoding them into a route would mean
    decoding them back out — which is exactly the sort of round trip that
    turns into a traversal bug. The transport resolves and contains it either
    way, but not creating the opportunity is better than catching it.
    """
    service = _service(core, session, user.id)
    provider = await service.require_provider()
    await service.guard("read", target=path)
    try:
        item = await provider.read(path)
    except VaultError:
        await session.commit()
        raise

    await service.audit("read", status="OK", target=path,
                        summary=f"Read Obsidian note {path}")
    await session.commit()
    return {
        "path": item.id,
        "title": item.title,
        "content": item.content,
        "tags": item.tags,
        "metadata": item.metadata,
        "modified_at": item.modified_at.isoformat() if item.modified_at else None,
        # Stated on every read, because this content reaches a model and the
        # UI should say what it is: a note is data, not instruction.
        "trust": "Vault content is untrusted data, not instructions.",
    }


@obsidian_router.get("/search")
async def search_vault(
    core: CoreDep,
    session: SessionDep,
    user: UserDep,
    _: AuthDep,
    q: Annotated[str, Query(min_length=1, max_length=500)],
    tag: str | None = None,
    folder: str | None = None,
    titles_only: bool = False,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> dict[str, Any]:
    service = _service(core, session, user.id)
    provider = await service.require_provider()
    await service.guard("search")
    hits = await provider.search(
        q, limit=limit, tag=tag, folder=folder, titles_only=titles_only
    )
    await service.audit(
        "search", status="OK",
        summary=f"Searched Obsidian for {q!r}: {len(hits)} hits",
        detail={"query": q, "hits": len(hits), "tag": tag, "folder": folder},
    )
    await session.commit()
    return {
        "results": [
            {
                "path": hit.item.id,
                "title": hit.item.title,
                "score": round(hit.score, 3),
                "excerpt": hit.excerpt,
            }
            for hit in hits
        ],
        "count": len(hits),
    }


@obsidian_router.get("/links")
async def note_links(
    core: CoreDep,
    session: SessionDep,
    user: UserDep,
    _: AuthDep,
    path: Annotated[str, Query(min_length=1, max_length=1024)],
) -> dict[str, Any]:
    service = _service(core, session, user.id)
    provider = await service.require_provider()
    await service.guard("links", target=path)
    try:
        result = await provider.links(path)
    except VaultError:
        raise
    await session.commit()
    return result


# ── writing (§13, §14, §15) ──────────────────────────────────────────────────


@obsidian_router.post("/notes", status_code=status.HTTP_201_CREATED)
async def create_note(
    body: NoteRequest,
    core: CoreDep,
    session: SessionDep,
    user: UserDep,
    _: AuthDep,
) -> dict[str, Any]:
    service = _service(core, session, user.id)
    provider = await service.require_provider()
    target = body.path or f"{body.title}.md"

    try:
        await service.guard(
            "create", target=target,
            confirmation_body=f"Create {target} in your Obsidian vault.",
            arguments={"title": body.title},
        )
        item = await provider.create(
            title=body.title, content=body.content, path=body.path,
            project=body.project, tags=body.tags,
        )
    except ConfirmationRequiredError as exc:
        await session.commit()
        return {"status": "needs_confirmation",
                "confirmation_id": exc.confirmation_id,
                "message": exc.user_message}
    except JarvisError:
        # Committed so the audit row survives, then re-raised untouched: the
        # application's exception handler renders the structured envelope
        # every other error in the system uses. Wrapping it in an
        # HTTPException here would give this router a second error shape.
        await session.commit()
        raise

    # Indexed immediately: a note JARVIS just wrote should be findable in the
    # next question, not after the next sync.
    syncer = _syncer(core, session, user.id, provider)
    indexed = await syncer.index_note(item.id)

    await service.audit(
        "create", status="OK", target=item.id,
        summary=f"Created Obsidian note {item.id}",
        detail={"bytes": item.byte_size, "chunks": indexed["chunks"]},
    )
    await session.commit()
    return {"path": item.id, "title": item.title, "indexed": indexed}


@obsidian_router.patch("/note")
async def update_note(
    body: UpdateRequest,
    core: CoreDep,
    session: SessionDep,
    user: UserDep,
    _: AuthDep,
    path: Annotated[str, Query(min_length=1, max_length=1024)],
) -> dict[str, Any]:
    service = _service(core, session, user.id)
    provider = await service.require_provider()
    syncer = _syncer(core, session, user.id, provider)

    # The operation depends on the mode, and so does how hard it is to do. A
    # full replace destroys what was there; an append does not.
    operation = "overwrite" if body.mode == "replace" else "append"

    try:
        await service.guard(
            operation, target=path,
            confirmation_body=(
                f"Replace the entire contents of {path} in your Obsidian vault. "
                "The current text will be lost."
                if body.mode == "replace"
                else f"Add to {path} in your Obsidian vault."
            ),
            arguments={"mode": body.mode, "section": body.section},
        )
        item = await provider.update(
            path, content=body.content, mode=body.mode,
            section=body.section, expected_hash=body.expected_hash,
        )
    except ConfirmationRequiredError as exc:
        # JARVIS's intended version is remembered while it waits, so that an
        # edit made in Obsidian in the meantime becomes a detectable conflict
        # rather than a silent overwrite when the approval arrives.
        await syncer.record_local_change(path, body.content)
        await session.commit()
        return {"status": "needs_confirmation",
                "confirmation_id": exc.confirmation_id,
                "message": exc.user_message}
    except ConflictError:
        # JARVIS's intended version is kept so the disagreement is visible in
        # /conflicts rather than lost with the failed request.
        await syncer.record_local_change(path, body.content)
        await session.commit()
        raise
    except JarvisError:
        # Committed so the audit row survives, then re-raised untouched: the
        # application's exception handler renders the structured envelope
        # every other error in the system uses. Wrapping it in an
        # HTTPException here would give this router a second error shape.
        await session.commit()
        raise

    indexed = await syncer.index_note(item.id)
    await service.audit(
        "update", status="OK", target=path,
        summary=f"Updated Obsidian note {path} ({body.mode})",
        detail={"mode": body.mode, "section": body.section},
    )
    await session.commit()
    return {"path": item.id, "mode": body.mode, "indexed": indexed}


@obsidian_router.delete("/note")
async def delete_note(
    core: CoreDep,
    session: SessionDep,
    user: UserDep,
    _: AuthDep,
    path: Annotated[str, Query(min_length=1, max_length=1024)],
) -> dict[str, Any]:
    service = _service(core, session, user.id)
    provider = await service.require_provider()

    try:
        await service.guard(
            "delete", target=path,
            confirmation_body=(
                f"Permanently delete {path} from your Obsidian vault. "
                "This cannot be undone by JARVIS."
            ),
        )
        await provider.delete(path)
    except ConfirmationRequiredError as exc:
        await session.commit()
        return {"status": "needs_confirmation",
                "confirmation_id": exc.confirmation_id,
                "message": exc.user_message}
    except JarvisError:
        # Committed so the audit row survives, then re-raised untouched: the
        # application's exception handler renders the structured envelope
        # every other error in the system uses. Wrapping it in an
        # HTTPException here would give this router a second error shape.
        await session.commit()
        raise

    syncer = _syncer(core, session, user.id, provider)
    await syncer.forget_note(path)
    await service.audit("delete", status="OK", target=path,
                        summary=f"Deleted Obsidian note {path}")
    await session.commit()
    return {"path": path, "deleted": True}


# ── sync (§12, §23, §24) ─────────────────────────────────────────────────────


@obsidian_router.get("/sync/plan")
async def sync_plan(
    core: CoreDep, session: SessionDep, user: UserDep, _: AuthDep
) -> dict[str, Any]:
    """What a sync would do, without doing it."""
    service = _service(core, session, user.id)
    provider = await service.require_provider()
    await service.guard("sync")
    plan = await _syncer(core, session, user.id, provider).plan(
        limit=core.settings.obsidian_sync_limit
    )
    await session.commit()
    return plan.to_dict()


@obsidian_router.post("/sync")
async def run_sync(
    core: CoreDep,
    session: SessionDep,
    user: UserDep,
    _: AuthDep,
    remove_deleted: bool = True,
) -> dict[str, Any]:
    """Pull the vault into the knowledge index. Obsidian → JARVIS only."""
    service = _service(core, session, user.id)
    provider = await service.require_provider()
    await service.guard("sync")

    syncer = _syncer(core, session, user.id, provider)
    result = await syncer.pull(
        limit=core.settings.obsidian_sync_limit, remove_deleted=remove_deleted
    )
    await service.mark_synced(result)
    await service.audit(
        "sync", status="CONFLICT" if result.conflicts else "OK",
        summary=(
            f"Synced Obsidian: {result.indexed} new, {result.updated} updated, "
            f"{result.removed} removed, {len(result.conflicts)} conflicts"
        ),
        detail=result.to_dict(), duration_ms=result.duration_ms,
    )
    await session.commit()
    return result.to_dict()


@obsidian_router.get("/conflicts")
async def list_conflicts(
    core: CoreDep, session: SessionDep, user: UserDep, _: AuthDep
) -> dict[str, Any]:
    service = _service(core, session, user.id)
    provider = await service.require_provider()
    syncer = _syncer(core, session, user.id, provider)
    plan = await syncer.plan(limit=core.settings.obsidian_sync_limit)

    detailed = []
    for change in plan.conflicts:
        detail = await syncer.conflict(change.path)
        if detail is not None:
            detailed.append(detail)
    return {"conflicts": detailed, "count": len(detailed),
            "resolutions": list(RESOLUTIONS)}


@obsidian_router.post("/conflicts/resolve")
async def resolve_conflict(
    body: ResolveRequest,
    core: CoreDep,
    session: SessionDep,
    user: UserDep,
    _: AuthDep,
    path: Annotated[str, Query(min_length=1, max_length=1024)],
) -> dict[str, Any]:
    service = _service(core, session, user.id)
    provider = await service.require_provider()

    # Resolutions that write to the vault need write permission; the two that
    # only change JARVIS's side do not. Asking for write permission to discard
    # a pending change would be theatre.
    if body.resolution in {"keep_jarvis", "merge"}:
        await service.guard(
            "overwrite", target=path,
            confirmation_body=(
                f"Resolve the conflict on {path} by writing to your vault "
                f"({body.resolution})."
            ),
            arguments={"resolution": body.resolution},
        )

    syncer = _syncer(core, session, user.id, provider)
    result = await syncer.resolve(path, body.resolution)
    await service.audit(
        "update", status="RESOLVED", target=path,
        summary=f"Resolved Obsidian conflict on {path}: {body.resolution}",
        detail=result,
    )
    await session.commit()
    return result


@obsidian_router.get("/audit")
async def obsidian_audit(
    core: CoreDep,
    session: SessionDep,
    user: UserDep,
    _: AuthDep,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> dict[str, Any]:
    """What JARVIS did to the vault (§21).

    A filtered view of the append-only activity log. Read-only, like the
    computer audit — there is no route that edits or deletes an entry.
    """
    from sqlalchemy import select

    from jarvis.db.models import ActivityKind, ActivityLog

    rows = (
        await session.execute(
            select(ActivityLog)
            .where(ActivityLog.kind == ActivityKind.OBSIDIAN_ACTION)
            .order_by(ActivityLog.created_at.desc())
            .limit(limit)
        )
    ).scalars().all()

    return {
        "entries": [
            {
                "id": row.id,
                "operation": (row.detail or {}).get("operation"),
                "target": (row.detail or {}).get("target"),
                "summary": row.summary,
                "status": row.status,
                "actor": row.actor,
                "detail": row.detail,
                "created_at": row.created_at.isoformat() if row.created_at else None,
            }
            for row in rows
        ],
        "count": len(rows),
    }


OBSIDIAN_ROUTERS = [obsidian_router]
