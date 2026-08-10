"""Memory, project, and knowledge endpoints (§28, §29, §31, §35).

Split from :mod:`jarvis.api.routes` because Phase 1's module was already at the
size where finding anything meant scrolling. Same conventions throughout:
bearer auth on everything, ownership checked before any read or write, and 404
rather than 403 for someone else's row so an id cannot be probed for existence.

The destructive endpoints are the ones worth reading carefully. §35 requires
that memory always be removable, and the API honours that literally — including
"clear everything" — but every bulk path demands an explicit scope, and hard
deletion is a separate verb from archiving. Nothing here can erase content by
accident.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Body, File, HTTPException, Query, UploadFile, status
from pydantic import BaseModel, Field

from jarvis.api.deps import AuthDep, CoreDep, SessionDep, UserDep
from jarvis.db.models import ActivityKind, ProjectStatus
from jarvis.errors import ValidationError
from jarvis.knowledge.ingestion.pipeline import (
    IngestionPipeline,
    IngestRequest,
    ingest_path,
)
from jarvis.knowledge.ingestion.loaders import available_formats
from jarvis.knowledge.providers.internal import (
    InternalKnowledgeProvider,
    LocalFileProvider,
)
from jarvis.knowledge.service import KnowledgeService
from jarvis.knowledge.types import DocumentStatus, SourceKind
from jarvis.logging import get_logger
from jarvis.memory.portability import (
    export_memories,
    import_memories,
    to_markdown,
)
from jarvis.memory.projects import ProjectService
from jarvis.memory.retrieval import MemoryRetriever, RetrievalQuery
from jarvis.memory.service import (
    MemoryDraft,
    MemoryFilter,
    MemoryService,
    SecretInMemoryError,
)
from jarvis.memory.types import MemorySource, MemoryStatus, MemoryType

log = get_logger(__name__)


# ── request models ───────────────────────────────────────────────────────────


class CreateMemoryRequest(BaseModel):
    content: str = Field(min_length=1, max_length=4000)
    type: MemoryType = MemoryType.USER_FACT
    subject: str | None = None
    summary: str | None = None
    importance: float | None = Field(default=None, ge=0, le=1)
    confidence: float = Field(default=1.0, ge=0, le=1)
    project_id: str | None = None
    tags: list[str] = Field(default_factory=list)
    pinned: bool = False
    #: Explicit override for the secret guard, recorded in the revision log.
    #: A prohibition the user cannot override is one they route around by
    #: turning the feature off.
    allow_sensitive: bool = False


class UpdateMemoryRequest(BaseModel):
    content: str | None = None
    summary: str | None = None
    subject: str | None = None
    type: MemoryType | None = None
    importance: float | None = Field(default=None, ge=0, le=1)
    confidence: float | None = Field(default=None, ge=0, le=1)
    pinned: bool | None = None
    tags: list[str] | None = None
    note: str | None = None
    allow_sensitive: bool = False


class ForgetScopeRequest(BaseModel):
    project_id: str | None = None
    #: Must be asked for by name. There is no code path where an empty filter
    #: means "everything".
    all_memories: bool = False
    #: Archive (default) versus erase content permanently.
    hard: bool = False


class CreateProjectRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    key: str | None = None
    description: str | None = None
    goals: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)


class UpdateProjectRequest(BaseModel):
    name: str | None = None
    description: str | None = None
    current_state: str | None = None
    status: ProjectStatus | None = None
    goals: list[str] | None = None
    tags: list[str] | None = None


class IngestPathRequest(BaseModel):
    path: str = Field(min_length=1)
    project_id: str | None = None
    tags: list[str] = Field(default_factory=list)


# ── memory ───────────────────────────────────────────────────────────────────

memory_router = APIRouter(prefix="/memories", tags=["memory"])


def _service(core: Any, session: Any) -> MemoryService:
    return MemoryService(
        session,
        embeddings=core.embeddings,
        duplicate_threshold=core.settings.memory_duplicate_threshold,
    )


@memory_router.get("")
async def list_memories(
    core: CoreDep,
    session: SessionDep,
    user: UserDep,
    _: AuthDep,
    q: str | None = None,
    type: Annotated[list[MemoryType] | None, Query()] = None,
    memory_status: Annotated[list[MemoryStatus] | None, Query(alias="status")] = None,
    source: Annotated[list[MemorySource] | None, Query()] = None,
    project_id: str | None = None,
    tags: Annotated[list[str] | None, Query()] = None,
    min_importance: Annotated[float | None, Query(ge=0, le=1)] = None,
    min_confidence: Annotated[float | None, Query(ge=0, le=1)] = None,
    include_expired: bool = False,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    service = _service(core, session)
    filters = MemoryFilter(
        types=type,
        statuses=memory_status,
        sources=source,
        project_id=project_id,
        tags=tags,
        search=q,
        min_importance=min_importance,
        min_confidence=min_confidence,
        include_expired=include_expired,
        limit=limit,
        offset=offset,
    )
    rows = await service.search(user.id, filters)
    return {
        "memories": [MemoryService.to_dict(m) for m in rows],
        "total": await service.count(user.id, filters),
        "limit": limit,
        "offset": offset,
    }


@memory_router.get("/search")
async def search_memories(
    core: CoreDep,
    session: SessionDep,
    user: UserDep,
    _: AuthDep,
    q: Annotated[str, Query(min_length=1)],
    project_id: str | None = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
) -> dict[str, Any]:
    """Ranked retrieval — the same path the orchestrator uses.

    Scores are returned so the ranking is inspectable: "why did it remember
    that?" should be answerable from the UI rather than from the logs.
    """
    result = await MemoryRetriever(session, embeddings=core.embeddings).retrieve(
        RetrievalQuery(text=q, user_id=user.id, project_id=project_id, limit=limit)
    )
    return {
        "results": [
            MemoryService.to_dict(item.memory, score=item.score.describe())
            for item in result.memories
        ],
        "considered": result.considered,
        "semantic": result.semantic_used,
        "duration_ms": round(result.duration_ms, 2),
    }


@memory_router.get("/stats")
async def memory_stats(
    core: CoreDep, session: SessionDep, user: UserDep, _: AuthDep
) -> dict[str, Any]:
    from sqlalchemy import func, select

    from jarvis.db.models import Memory

    by_type = {
        (t.value if hasattr(t, "value") else t): int(count)
        for t, count in (
            await session.execute(
                select(Memory.type, func.count())
                .where(Memory.user_id == user.id, Memory.status == MemoryStatus.ACTIVE)
                .group_by(Memory.type)
            )
        ).all()
    }
    by_status = {
        (s.value if hasattr(s, "value") else s): int(count)
        for s, count in (
            await session.execute(
                select(Memory.status, func.count())
                .where(Memory.user_id == user.id)
                .group_by(Memory.status)
            )
        ).all()
    }
    return {
        "by_type": by_type,
        "by_status": by_status,
        "total_active": sum(by_type.values()),
        "capture_mode": core.settings.memory_capture_mode,
        "semantic_search": core.embeddings.info.semantic,
        "embedding_model": core.embeddings.info.model,
    }


@memory_router.post("", status_code=status.HTTP_201_CREATED)
async def create_memory(
    body: CreateMemoryRequest,
    core: CoreDep,
    session: SessionDep,
    user: UserDep,
    _: AuthDep,
) -> dict[str, Any]:
    outcome = await _service(core, session).create(
        user.id,
        MemoryDraft(
            content=body.content,
            type=body.type,
            subject=body.subject,
            summary=body.summary,
            source=MemorySource.USER,
            confidence=body.confidence,
            importance=body.importance,
            project_id=body.project_id,
            tags=body.tags,
            pinned=body.pinned,
        ),
        actor="user",
        allow_sensitive=body.allow_sensitive,
    )
    if outcome.action == "refused":
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, outcome.detail)

    await session.commit()
    assert outcome.memory is not None
    return {
        **MemoryService.to_dict(outcome.memory),
        "action": outcome.action,
        "detail": outcome.detail,
    }


@memory_router.get("/{memory_id}")
async def get_memory(
    memory_id: str, core: CoreDep, session: SessionDep, user: UserDep, _: AuthDep
) -> dict[str, Any]:
    service = _service(core, session)
    memory = await service.owned(memory_id, user.id)
    related = await service.related(memory_id)
    return {
        **MemoryService.to_dict(memory),
        "related": [
            {"relation": relation.value, **MemoryService.to_dict(other)}
            for other, relation in related
        ],
        "history": [
            {
                "kind": r.kind.value if hasattr(r.kind, "value") else r.kind,
                "actor": r.actor,
                "changes": r.changes,
                "note": r.note,
                "at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in await service.history(memory_id)
        ],
    }


@memory_router.patch("/{memory_id}")
async def update_memory_endpoint(
    memory_id: str,
    body: UpdateMemoryRequest,
    core: CoreDep,
    session: SessionDep,
    user: UserDep,
    _: AuthDep,
) -> dict[str, Any]:
    service = _service(core, session)
    await service.owned(memory_id, user.id)

    changes = body.model_dump(exclude_none=True, exclude={"note", "allow_sensitive"})
    if not changes:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "No fields to update")

    try:
        memory = await service.update(
            memory_id, actor="user", note=body.note,
            allow_sensitive=body.allow_sensitive, **changes,
        )
    except SecretInMemoryError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, exc.user_message)

    await session.commit()
    return MemoryService.to_dict(memory)


@memory_router.post("/{memory_id}/confirm")
async def confirm_memory(
    memory_id: str,
    core: CoreDep,
    session: SessionDep,
    user: UserDep,
    _: AuthDep,
    approved: Annotated[bool, Body(embed=True)] = True,
) -> dict[str, Any]:
    """Resolve a PROPOSED memory (§14)."""
    service = _service(core, session)
    await service.owned(memory_id, user.id)
    memory = await service.confirm(memory_id, approved=approved, actor="user")
    await session.commit()
    return MemoryService.to_dict(memory)


@memory_router.post("/{memory_id}/archive")
async def archive_memory(
    memory_id: str, core: CoreDep, session: SessionDep, user: UserDep, _: AuthDep
) -> dict[str, Any]:
    service = _service(core, session)
    await service.owned(memory_id, user.id)
    memory = await service.archive(memory_id, actor="user")
    await session.commit()
    return MemoryService.to_dict(memory)


@memory_router.post("/{memory_id}/restore")
async def restore_memory(
    memory_id: str, core: CoreDep, session: SessionDep, user: UserDep, _: AuthDep
) -> dict[str, Any]:
    service = _service(core, session)
    await service.owned(memory_id, user.id)
    memory = await service.restore(memory_id, actor="user")
    await session.commit()
    return MemoryService.to_dict(memory)


@memory_router.delete("/{memory_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_memory(
    memory_id: str, core: CoreDep, session: SessionDep, user: UserDep, _: AuthDep
) -> None:
    """Hard delete: content erased, tombstone kept. §35's floor — memory must
    never be impossible to remove."""
    service = _service(core, session)
    await service.owned(memory_id, user.id)
    await service.delete(memory_id, actor="user")
    await session.commit()


@memory_router.post("/forget")
async def forget_scope(
    body: ForgetScopeRequest,
    core: CoreDep,
    session: SessionDep,
    user: UserDep,
    _: AuthDep,
) -> dict[str, Any]:
    """Bulk forget. Requires an explicit scope; refuses an empty one."""
    try:
        count = await _service(core, session).forget_scope(
            user.id,
            project_id=body.project_id,
            all_memories=body.all_memories,
            hard=body.hard,
            actor="user",
        )
    except ValidationError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, exc.user_message)

    await session.commit()
    log.warning(
        "memory_bulk_forget_api", count=count, project_id=body.project_id,
        all_memories=body.all_memories, hard=body.hard,
    )
    return {"forgotten": count, "hard": body.hard}


@memory_router.get("/export/archive")
async def export_archive(
    session: SessionDep,
    user: UserDep,
    _: AuthDep,
    project_id: str | None = None,
    include_archived: bool = False,
    format: Annotated[str, Query(pattern="^(json|markdown)$")] = "json",
) -> Any:
    """Portable export (§36). JSON is lossless; Markdown is for humans."""
    from fastapi.responses import PlainTextResponse

    archive = await export_memories(
        session, user.id, project_id=project_id, include_archived=include_archived
    )
    if format == "markdown":
        return PlainTextResponse(
            to_markdown(archive),
            media_type="text/markdown; charset=utf-8",
            headers={
                "Content-Disposition": 'attachment; filename="jarvis-memory.md"'
            },
        )
    return archive


@memory_router.post("/import")
async def import_archive(
    core: CoreDep,
    session: SessionDep,
    user: UserDep,
    _: AuthDep,
    archive: Annotated[dict[str, Any], Body()],
    reconcile: bool = False,
) -> dict[str, Any]:
    """Restore an archive (§37). The secret guard runs on every row."""
    try:
        report = await import_memories(
            session, user.id, archive, embeddings=core.embeddings, reconcile=reconcile
        )
    except ValidationError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, exc.user_message)
    await session.commit()
    return report.to_dict()


# ── projects ─────────────────────────────────────────────────────────────────

project_router = APIRouter(prefix="/projects", tags=["projects"])


@project_router.get("")
async def list_projects(
    session: SessionDep, user: UserDep, _: AuthDep, include_archived: bool = False
) -> dict[str, Any]:
    rows = await ProjectService(session).list(
        user.id, include_archived=include_archived
    )
    return {"projects": [ProjectService.to_dict(p) for p in rows]}


@project_router.post("", status_code=status.HTTP_201_CREATED)
async def create_project(
    body: CreateProjectRequest, session: SessionDep, user: UserDep, _: AuthDep
) -> dict[str, Any]:
    try:
        project = await ProjectService(session).create(
            user.id,
            name=body.name,
            key=body.key,
            description=body.description,
            goals=body.goals,
            tags=body.tags,
        )
    except ValidationError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, exc.user_message)
    await session.commit()
    return ProjectService.to_dict(project)


@project_router.get("/{project_id}")
async def get_project(
    project_id: str, session: SessionDep, user: UserDep, _: AuthDep
) -> dict[str, Any]:
    service = ProjectService(session)
    project = await service.owned(project_id, user.id)
    return ProjectService.to_dict(project, await service.summarise(project.id))


@project_router.patch("/{project_id}")
async def update_project(
    project_id: str,
    body: UpdateProjectRequest,
    session: SessionDep,
    user: UserDep,
    _: AuthDep,
) -> dict[str, Any]:
    service = ProjectService(session)
    await service.owned(project_id, user.id)
    changes = body.model_dump(exclude_none=True)
    if not changes:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "No fields to update")
    project = await service.update(project_id, **changes)
    await session.commit()
    return ProjectService.to_dict(project)


# ── knowledge ────────────────────────────────────────────────────────────────

knowledge_router = APIRouter(prefix="/knowledge", tags=["knowledge"])


def _knowledge(core: Any, session: Any, user_id: str) -> KnowledgeService:
    return KnowledgeService(
        session,
        embeddings=core.embeddings,
        providers=[
            InternalKnowledgeProvider(session, user_id),
            LocalFileProvider(core.settings.knowledge_roots),
        ],
    )


async def _knowledge_with_sources(
    core: Any, session: Any, user_id: str
) -> KnowledgeService:
    """The registry plus any external source the user has connected.

    Separate from :func:`_knowledge` because building it needs a database read
    — the Obsidian connection lives on a ``knowledge_sources`` row — and the
    retrieval paths that do not care about provider registration should not
    pay for it. ``/knowledge/sources`` is the caller that does.
    """
    from jarvis.knowledge.providers.obsidian import ObsidianService

    service = _knowledge(core, session, user_id)
    provider = await ObsidianService(session, user_id).provider()
    if provider is not None:
        service.register(provider)
    return service


@knowledge_router.get("/documents")
async def list_documents(
    core: CoreDep,
    session: SessionDep,
    user: UserDep,
    _: AuthDep,
    project_id: str | None = None,
    document_status: Annotated[DocumentStatus | None, Query(alias="status")] = None,
    q: str | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> dict[str, Any]:
    service = _knowledge(core, session, user.id)
    rows = await service.list_documents(
        user.id, project_id=project_id, status=document_status, search=q, limit=limit
    )
    return {
        "documents": [KnowledgeService.document_to_dict(d) for d in rows],
        "stats": await service.stats(user.id),
    }


@knowledge_router.get("/documents/{document_id}")
async def get_document(
    document_id: str, core: CoreDep, session: SessionDep, user: UserDep, _: AuthDep
) -> dict[str, Any]:
    service = _knowledge(core, session, user.id)
    document = await service.document(document_id, user.id)
    return {
        **KnowledgeService.document_to_dict(document),
        "chunks": [
            {
                "id": c.id,
                "ordinal": c.ordinal,
                "kind": c.kind.value if hasattr(c.kind, "value") else c.kind,
                "heading_path": c.heading_path,
                "content": c.content,
                "token_estimate": c.token_estimate,
            }
            for c in await service.chunks(document_id)
        ],
    }


@knowledge_router.delete(
    "/documents/{document_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def delete_document(
    document_id: str, core: CoreDep, session: SessionDep, user: UserDep, _: AuthDep
) -> None:
    service = _knowledge(core, session, user.id)
    document = await service.document(document_id, user.id)
    await IngestionPipeline(session, embeddings=core.embeddings).delete_document(
        document
    )
    await session.commit()


@knowledge_router.get("/search")
async def search_knowledge_endpoint(
    core: CoreDep,
    session: SessionDep,
    user: UserDep,
    _: AuthDep,
    q: Annotated[str, Query(min_length=1)],
    project_id: str | None = None,
    limit: Annotated[int, Query(ge=1, le=20)] = 6,
) -> dict[str, Any]:
    result = await _knowledge(core, session, user.id).search(
        user.id, q, limit=limit, project_id=project_id
    )
    return {
        "results": [
            {
                "chunk_id": hit.chunk.id,
                "document_id": hit.document.id,
                "citation": hit.citation(),
                "content": hit.chunk.content,
                "score": round(hit.score, 4),
                "provenance": hit.provenance.to_dict() if hit.provenance else None,
            }
            for hit in result.hits
        ],
        "considered": result.considered,
        "semantic": result.semantic_used,
        "duration_ms": round(result.duration_ms, 2),
    }


@knowledge_router.post("/ingest/upload", status_code=status.HTTP_201_CREATED)
async def ingest_upload(
    core: CoreDep,
    session: SessionDep,
    user: UserDep,
    _: AuthDep,
    file: Annotated[UploadFile, File()],
    project_id: str | None = None,
) -> dict[str, Any]:
    """Ingest an uploaded file.

    Uploads need no path allow-list — the bytes arrive over an authenticated
    request rather than being read off disk — but the size ceiling and the
    format check still apply, and the document is tainted like any other.
    """
    data = await file.read()
    pipeline = IngestionPipeline(
        session,
        embeddings=core.embeddings,
        max_bytes=core.settings.ingest_max_bytes,
        chunk_target_chars=core.settings.ingest_chunk_target_chars,
        chunk_overlap_chars=core.settings.ingest_chunk_overlap_chars,
    )
    try:
        result = await pipeline.ingest(
            IngestRequest(
                user_id=user.id,
                filename=file.filename or "upload",
                data=data,
                source_kind=SourceKind.UPLOAD,
                media_type=file.content_type,
                project_id=project_id,
            )
        )
    except ValidationError as exc:
        await session.rollback()
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, exc.user_message)

    await _record_ingest(session, core, user.id, result)
    await session.commit()
    return result.to_dict()


@knowledge_router.post("/ingest/path", status_code=status.HTTP_201_CREATED)
async def ingest_from_path(
    body: IngestPathRequest,
    core: CoreDep,
    session: SessionDep,
    user: UserDep,
    _: AuthDep,
) -> dict[str, Any]:
    """Ingest a file from disk, inside the configured roots.

    The allow-list is the security boundary. With no roots configured this
    refuses everything, which is the correct default for an endpoint that
    otherwise reads arbitrary files.
    """
    from pathlib import Path

    pipeline = IngestionPipeline(
        session,
        embeddings=core.embeddings,
        max_bytes=core.settings.ingest_max_bytes,
        chunk_target_chars=core.settings.ingest_chunk_target_chars,
        chunk_overlap_chars=core.settings.ingest_chunk_overlap_chars,
    )
    try:
        result = await ingest_path(
            pipeline,
            Path(body.path),
            user_id=user.id,
            allowed_roots=core.settings.knowledge_roots,
            project_id=body.project_id,
            tags=body.tags,
        )
    except ValidationError as exc:
        await session.rollback()
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, exc.user_message)

    await _record_ingest(session, core, user.id, result)
    await session.commit()
    return result.to_dict()


@knowledge_router.get("/sources")
async def knowledge_sources(
    core: CoreDep, session: SessionDep, user: UserDep, _: AuthDep
) -> dict[str, Any]:
    """Registered providers, each reporting real state.

    Obsidian appears with ``implemented: true`` from Phase 2.5, and
    ``connected`` reflecting whether a vault is actually reachable right now —
    the two are different facts and the panel renders both. A vault that is
    configured but unplugged reports connected=false with the reason, which is
    the case that would otherwise be papered over.
    """
    return {
        "sources": await (
            await _knowledge_with_sources(core, session, user.id)
        ).provider_status(),
        "formats": available_formats(),
        "roots": [str(p) for p in core.settings.knowledge_roots],
        "semantic_search": core.embeddings.info.semantic,
    }


async def _record_ingest(session: Any, core: Any, user_id: str, result: Any) -> None:
    from jarvis.activity.service import ActivityService

    await ActivityService(session, core.activity_bus).record(
        ActivityKind.KNOWLEDGE_INGESTED,
        summary=f"Ingested {result.document.title} ({result.chunks_created} chunks)",
        actor="user",
        detail=result.to_dict(),
        status="INDEXED",
    )


MEMORY_ROUTERS = [memory_router, project_router, knowledge_router]
