"""Activity recording and live event distribution.

Two responsibilities that deliberately share one entry point:

* **Durable record** — an append-only ``activity_logs`` row. This is the
  observability feed in Phase 1 and the audit trail the computer-control
  phases depend on. Nothing updates or deletes these rows.
* **Live fan-out** — an in-process pub/sub so the UI can watch work happen
  over SSE without polling.

Recording never raises into the caller. An activity write failing must not fail
the operation being recorded; a lost log line is much less bad than a lost
task. Failures are logged and swallowed.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import suppress
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from jarvis.db.models import ActivityKind, ActivityLog
from jarvis.logging import get_logger

log = get_logger(__name__)


@dataclass(slots=True)
class ActivityEvent:
    """The live form of an activity record."""

    kind: str
    actor: str
    summary: str
    detail: dict[str, Any] = field(default_factory=dict)
    request_id: str | None = None
    conversation_id: str | None = None
    task_id: str | None = None
    tool_name: str | None = None
    provider: str | None = None
    model: str | None = None
    status: str | None = None
    duration_ms: float | None = None
    error_code: str | None = None
    created_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ActivityBus:
    """In-process fan-out to SSE subscribers.

    Bounded queues with drop-oldest: a slow or abandoned browser tab must not
    apply backpressure to the orchestrator. Losing a UI frame is acceptable;
    stalling the request path is not.
    """

    def __init__(self, *, queue_size: int = 256) -> None:
        self._subscribers: set[asyncio.Queue[ActivityEvent]] = set()
        self._queue_size = queue_size
        self._lock = asyncio.Lock()

    async def subscribe(self) -> asyncio.Queue[ActivityEvent]:
        queue: asyncio.Queue[ActivityEvent] = asyncio.Queue(maxsize=self._queue_size)
        async with self._lock:
            self._subscribers.add(queue)
        return queue

    async def unsubscribe(self, queue: asyncio.Queue[ActivityEvent]) -> None:
        async with self._lock:
            self._subscribers.discard(queue)

    def publish(self, event: ActivityEvent) -> None:
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                with suppress(asyncio.QueueEmpty):
                    queue.get_nowait()  # drop oldest
                with suppress(asyncio.QueueFull):
                    queue.put_nowait(event)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    async def stream(self) -> AsyncIterator[ActivityEvent]:
        queue = await self.subscribe()
        try:
            while True:
                yield await queue.get()
        finally:
            await self.unsubscribe(queue)


class ActivityService:
    def __init__(self, session: AsyncSession, bus: ActivityBus | None = None) -> None:
        self.session = session
        self.bus = bus

    async def record(
        self,
        kind: ActivityKind,
        *,
        summary: str,
        actor: str = "system",
        detail: dict[str, Any] | None = None,
        request_id: str | None = None,
        conversation_id: str | None = None,
        task_id: str | None = None,
        execution_id: str | None = None,
        tool_name: str | None = None,
        provider: str | None = None,
        model: str | None = None,
        status: str | None = None,
        duration_ms: float | None = None,
        error_code: str | None = None,
    ) -> ActivityLog | None:
        entry = ActivityLog(
            kind=kind,
            actor=actor,
            summary=summary[:1000],
            detail=detail or {},
            request_id=request_id,
            conversation_id=conversation_id,
            task_id=task_id,
            execution_id=execution_id,
            tool_name=tool_name,
            provider=provider,
            model=model,
            status=status,
            duration_ms=duration_ms,
            error_code=error_code,
        )
        try:
            self.session.add(entry)
            await self.session.flush()
        except Exception as exc:  # never let logging break the operation
            log.warning("activity_record_failed", kind=kind.value, error=str(exc))
            return None

        if self.bus is not None:
            self.bus.publish(
                ActivityEvent(
                    kind=kind.value,
                    actor=actor,
                    summary=entry.summary,
                    detail=entry.detail,
                    request_id=request_id,
                    conversation_id=conversation_id,
                    task_id=task_id,
                    tool_name=tool_name,
                    provider=provider,
                    model=model,
                    status=status,
                    duration_ms=duration_ms,
                    error_code=error_code,
                    created_at=_iso(entry.created_at),
                )
            )
        return entry

    async def recent(
        self,
        *,
        limit: int = 100,
        request_id: str | None = None,
        conversation_id: str | None = None,
        task_id: str | None = None,
        kinds: list[ActivityKind] | None = None,
    ) -> list[ActivityLog]:
        stmt = select(ActivityLog).order_by(desc(ActivityLog.created_at)).limit(
            min(limit, 500)
        )
        if request_id:
            stmt = stmt.where(ActivityLog.request_id == request_id)
        if conversation_id:
            stmt = stmt.where(ActivityLog.conversation_id == conversation_id)
        if task_id:
            stmt = stmt.where(ActivityLog.task_id == task_id)
        if kinds:
            stmt = stmt.where(ActivityLog.kind.in_(kinds))
        return list((await self.session.execute(stmt)).scalars().all())

    @staticmethod
    def to_dict(entry: ActivityLog) -> dict[str, Any]:
        return {
            "id": entry.id,
            "kind": entry.kind.value if hasattr(entry.kind, "value") else entry.kind,
            "actor": entry.actor,
            "summary": entry.summary,
            "detail": entry.detail,
            "request_id": entry.request_id,
            "conversation_id": entry.conversation_id,
            "task_id": entry.task_id,
            "tool_name": entry.tool_name,
            "provider": entry.provider,
            "model": entry.model,
            "status": entry.status,
            "duration_ms": entry.duration_ms,
            "error_code": entry.error_code,
            "created_at": _iso(entry.created_at),
        }


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None
