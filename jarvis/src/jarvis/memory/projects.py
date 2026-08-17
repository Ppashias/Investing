"""Project memory (§27).

A project is the second addressable scope after the user. It exists so that
"continue working on Project X" resolves to something: a name, a current state,
its goals, and — through ``project_id`` — the memories, tasks, conversations
and documents already attached to it.

The division of labour is worth stating, because it is easy to collapse:

* ``Project.current_state`` is one editable paragraph — where things stand
  right now. Cheap to inject into context, and the thing that answers "what
  was I doing?".
* ``PROJECT_*`` memories are the individual remembered facts and decisions.
  Many, ranked, retrieved selectively.
* ``Task`` rows are the work itself. Already existed in Phase 1.

Keeping ``current_state`` distinct from the memories that produced it means the
summary can be corrected without rewriting history, and history can accumulate
without bloating every prompt.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from jarvis.db.base import utcnow
from jarvis.db.models import (
    Document,
    Memory,
    Project,
    ProjectStatus,
    Task,
    TaskStatus,
)
from jarvis.errors import NotFoundError, ValidationError
from jarvis.logging import get_logger
from jarvis.memory.types import MemoryStatus, MemoryType

log = get_logger(__name__)

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(name: str) -> str:
    return _SLUG_RE.sub("-", name.strip().lower()).strip("-")[:128]


@dataclass(slots=True)
class ProjectSummary:
    """Everything needed to re-enter a project cold."""

    project: Project
    open_tasks: int
    total_tasks: int
    memory_count: int
    decisions: list[Memory]
    state_memories: list[Memory]
    document_count: int


class ProjectService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(
        self,
        user_id: str,
        *,
        name: str,
        key: str | None = None,
        description: str | None = None,
        goals: list[str] | None = None,
        tags: list[str] | None = None,
    ) -> Project:
        name = name.strip()
        if not name:
            raise ValidationError("A project needs a name")

        resolved_key = slugify(key or name)
        if not resolved_key:
            raise ValidationError("Could not derive a project key from that name")

        existing = await self.by_key(user_id, resolved_key)
        if existing is not None:
            raise ValidationError(
                f"A project with key '{resolved_key}' already exists",
                user_message=f"You already have a project called {existing.name}.",
            )

        project = Project(
            user_id=user_id,
            key=resolved_key,
            name=name,
            description=description,
            goals=list(goals or []),
            tags=list(tags or []),
        )
        self.session.add(project)
        await self.session.flush()
        log.info("project_created", project_id=project.id, key=resolved_key)
        return project

    async def get(self, project_id: str) -> Project:
        project = await self.session.get(Project, project_id)
        if project is None:
            raise NotFoundError(f"Project {project_id} not found")
        return project

    async def owned(self, project_id: str, user_id: str) -> Project:
        project = await self.get(project_id)
        if project.user_id != user_id:
            raise NotFoundError(f"Project {project_id} not found")
        return project

    async def by_key(self, user_id: str, key: str) -> Project | None:
        return (
            await self.session.execute(
                select(Project).where(
                    Project.user_id == user_id, Project.key == slugify(key)
                )
            )
        ).scalar_one_or_none()

    async def resolve(self, user_id: str, reference: str) -> Project | None:
        """Find a project from whatever the user called it.

        Tries the id, then the slug, then a name match. This is what lets
        "continue working on Project X" work six months later without the user
        remembering an identifier.
        """
        reference = reference.strip()
        if not reference:
            return None

        if reference.startswith("proj_"):
            project = await self.session.get(Project, reference)
            if project is not None and project.user_id == user_id:
                return project

        exact = await self.by_key(user_id, reference)
        if exact is not None:
            return exact

        like = f"%{reference}%"
        return (
            await self.session.execute(
                select(Project)
                .where(
                    Project.user_id == user_id,
                    or_(Project.name.ilike(like), Project.key.ilike(like)),
                )
                .order_by(Project.updated_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()

    async def list(
        self, user_id: str, *, include_archived: bool = False, limit: int = 100
    ) -> list[Project]:
        stmt = select(Project).where(Project.user_id == user_id)
        if not include_archived:
            stmt = stmt.where(Project.status != ProjectStatus.ARCHIVED)
        stmt = stmt.order_by(Project.updated_at.desc()).limit(limit)
        return list((await self.session.execute(stmt)).scalars().all())

    async def update(self, project_id: str, **changes: Any) -> Project:
        project = await self.get(project_id)
        for field_name, value in changes.items():
            if value is not None and hasattr(project, field_name):
                setattr(project, field_name, value)
        if changes.get("status") is ProjectStatus.ARCHIVED:
            project.archived_at = utcnow()
        project.updated_at = utcnow()
        await self.session.flush()
        return project

    async def summarise(self, project_id: str) -> ProjectSummary:
        """Assemble the project's context. Used by the API and by retrieval."""
        project = await self.get(project_id)

        task_rows = (
            await self.session.execute(
                select(Task.status, func.count())
                .where(Task.project_id == project_id)
                .group_by(Task.status)
            )
        ).all()
        counts = {status: int(count) for status, count in task_rows}
        open_tasks = sum(
            count
            for status, count in counts.items()
            if not (status.is_terminal if hasattr(status, "is_terminal") else False)
            and status != TaskStatus.FAILED
        )

        memory_count = int(
            (
                await self.session.execute(
                    select(func.count())
                    .select_from(Memory)
                    .where(
                        Memory.project_id == project_id,
                        Memory.status == MemoryStatus.ACTIVE,
                    )
                )
            ).scalar_one()
        )

        decisions = list(
            (
                await self.session.execute(
                    select(Memory)
                    .where(
                        Memory.project_id == project_id,
                        Memory.status == MemoryStatus.ACTIVE,
                        Memory.type == MemoryType.PROJECT_DECISION,
                    )
                    .order_by(Memory.importance.desc(), Memory.created_at.desc())
                    .limit(10)
                )
            ).scalars().all()
        )

        state_memories = list(
            (
                await self.session.execute(
                    select(Memory)
                    .where(
                        Memory.project_id == project_id,
                        Memory.status == MemoryStatus.ACTIVE,
                        Memory.type == MemoryType.PROJECT_STATE,
                    )
                    .order_by(Memory.updated_at.desc())
                    .limit(5)
                )
            ).scalars().all()
        )

        document_count = int(
            (
                await self.session.execute(
                    select(func.count())
                    .select_from(Document)
                    .where(Document.project_id == project_id)
                )
            ).scalar_one()
        )

        return ProjectSummary(
            project=project,
            open_tasks=open_tasks,
            total_tasks=sum(counts.values()),
            memory_count=memory_count,
            decisions=decisions,
            state_memories=state_memories,
            document_count=document_count,
        )

    @staticmethod
    def to_dict(project: Project, summary: ProjectSummary | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": project.id,
            "key": project.key,
            "name": project.name,
            "description": project.description,
            "status": project.status.value
            if hasattr(project.status, "value")
            else project.status,
            "current_state": project.current_state,
            "goals": project.goals,
            "tags": project.tags,
            "created_at": project.created_at.isoformat() if project.created_at else None,
            "updated_at": project.updated_at.isoformat() if project.updated_at else None,
        }
        if summary is not None:
            payload["stats"] = {
                "open_tasks": summary.open_tasks,
                "total_tasks": summary.total_tasks,
                "memories": summary.memory_count,
                "documents": summary.document_count,
            }
            payload["decisions"] = [
                {"id": m.id, "content": m.content} for m in summary.decisions
            ]
            payload["state"] = [
                {"id": m.id, "content": m.content} for m in summary.state_memories
            ]
        return payload

    @staticmethod
    def to_prompt_block(summary: ProjectSummary) -> str:
        """Render a project for the system prompt.

        Deliberately compact: the state paragraph, the goals, the decisions,
        and the task counts. Everything else is retrievable on demand and does
        not belong in every request.
        """
        project = summary.project
        lines = [f"Project: {project.name} ({project.key})"]
        if project.description:
            lines.append(project.description)
        if project.current_state:
            lines.append(f"Current state: {project.current_state}")
        if project.goals:
            lines.append("Goals: " + "; ".join(project.goals))
        if summary.decisions:
            lines.append("Decisions made:")
            lines.extend(f"  - {m.content}" for m in summary.decisions[:6])
        if summary.total_tasks:
            lines.append(
                f"Tasks: {summary.open_tasks} open of {summary.total_tasks}."
            )
        return "\n".join(lines)
