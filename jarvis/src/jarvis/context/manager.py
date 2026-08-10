"""Context assembly.

The brief (§12) is explicit that the model must not be handed the whole
database or the whole conversation. This module decides what a given request
actually needs and enforces a budget.

Phase 1 assembles four sources: recent conversation turns, open tasks, user
configuration, and (stubbed) memory. Phase 2 replaces the memory stub with real
retrieval — the seam is :meth:`ContextManager._load_memory`, and nothing else
needs to change when it does.

Budgeting is deliberately crude. Token counts are estimated at ~4 characters
per token rather than measured, because measuring means a round trip per
request and the estimate only has to be good enough to decide what to drop.
Provider usage accounting gives exact numbers after the fact.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from jarvis.db.models import Message, Task, TaskStatus, User
from jarvis.logging import get_logger
from jarvis.tasks.service import TaskFilter, TaskService

log = get_logger(__name__)

CHARS_PER_TOKEN = 4


@dataclass(slots=True)
class ContextBudget:
    """Caps on what may be assembled.

    ``max_history_messages`` bounds turn count and ``max_history_chars`` bounds
    size, because one very long turn can blow the budget that twenty short ones
    would not.
    """

    max_history_messages: int = 40
    max_history_chars: int = 60_000
    max_tasks: int = 15
    max_memories: int = 10


@dataclass(slots=True)
class ContextBundle:
    """Everything assembled for one request."""

    conversation_id: str | None = None
    history: list[Message] = field(default_factory=list)
    user_context: str = ""
    project_context: str = ""
    task_context: str = ""
    memory_context: str = ""
    #: True when history was trimmed — surfaced in the prompt so the model
    #: knows not to claim knowledge of turns it cannot see.
    truncated: bool = False
    stats: dict[str, Any] = field(default_factory=dict)

    @property
    def approx_tokens(self) -> int:
        total = sum(len(m.content or "") for m in self.history)
        total += len(self.user_context) + len(self.project_context)
        total += len(self.task_context) + len(self.memory_context)
        return total // CHARS_PER_TOKEN


class ContextManager:
    def __init__(self, session: AsyncSession, budget: ContextBudget | None = None) -> None:
        self.session = session
        self.budget = budget or ContextBudget()

    async def assemble(
        self,
        *,
        user_id: str,
        conversation_id: str | None,
        project_id: str | None = None,
        include_tasks: bool = True,
        query: str | None = None,
    ) -> ContextBundle:
        bundle = ContextBundle(conversation_id=conversation_id)

        if conversation_id:
            bundle.history, bundle.truncated = await self._load_history(conversation_id)

        bundle.user_context = await self._load_user_context(user_id)

        if include_tasks:
            bundle.task_context = await self._load_task_context(user_id)

        if project_id:
            bundle.project_context = self._load_project_context(project_id)

        bundle.memory_context = await self._load_memory(user_id, query)

        bundle.stats = {
            "history_messages": len(bundle.history),
            "history_truncated": bundle.truncated,
            "approx_tokens": bundle.approx_tokens,
        }
        log.debug("context_assembled", **bundle.stats)
        return bundle

    # ── sources ──────────────────────────────────────────────────────────────

    async def _load_history(self, conversation_id: str) -> tuple[list[Message], bool]:
        from jarvis.conversations.service import ConversationService

        service = ConversationService(self.session)
        messages = await service.messages(
            conversation_id, limit=self.budget.max_history_messages
        )

        total = await service.messages(conversation_id)
        truncated = len(total) > len(messages)

        # Trim oldest-first until the character budget is met. Dropping from the
        # front preserves the most recent turns, which are the ones the current
        # request actually depends on.
        size = sum(len(m.content or "") for m in messages)
        while messages and size > self.budget.max_history_chars:
            dropped = messages.pop(0)
            size -= len(dropped.content or "")
            truncated = True

        return messages, truncated

    async def _load_user_context(self, user_id: str) -> str:
        user = await self.session.get(User, user_id)
        if user is None:
            return ""
        lines = [f"Name: {user.display_name or user.name}."]
        preferences = (user.settings or {}).get("preferences")
        if preferences:
            lines.append(f"Stated preferences: {preferences}")
        timezone = (user.settings or {}).get("timezone")
        if timezone:
            lines.append(f"Timezone: {timezone}.")
        return "\n".join(lines)

    async def _load_task_context(self, user_id: str) -> str:
        service = TaskService(self.session)
        tasks: list[Task] = await service.list(
            user_id,
            TaskFilter(
                include_terminal=False,
                limit=self.budget.max_tasks,
            ),
        )
        if not tasks:
            return "No open tasks."

        lines = []
        for t in tasks:
            due = f" (due {t.due_at.date().isoformat()})" if t.due_at else ""
            lines.append(f"- {t.title} — {t.status.value}, {t.priority.value}{due} [{t.id}]")
        header = (
            f"{len(tasks)} open task(s). Use list_tasks for the full set or for "
            "completed items."
        )
        return header + "\n" + "\n".join(lines)

    @staticmethod
    def _load_project_context(project_id: str) -> str:
        # Projects arrive in Phase 7. Carrying the id through now means the
        # prompt shape does not change when they do.
        return f"Working within project {project_id}."

    async def _load_memory(self, user_id: str, query: str | None) -> str:
        """Phase 2 seam.

        Returns nothing today. Deliberately not stubbed with placeholder text:
        an empty block is honest, whereas fabricated "remembered" content would
        make the model claim recall it does not have.
        """
        return ""
