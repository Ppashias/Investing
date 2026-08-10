"""Context assembly.

The brief (§12) is explicit that the model must not be handed the whole
database or the whole conversation. This module decides what a given request
actually needs and enforces a budget.

Phase 1 assembled four sources: recent conversation turns, open tasks, user
configuration, and a stubbed memory block. Phase 2 fills in the last of those
and adds two more — retrieved knowledge, and real project context — through the
same budgeted path.

Memory and knowledge are assembled here rather than in a stage of their own so
that one object owns the budget. Two components each independently deciding
they may spend 8,000 characters is how a context window overflows.

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
    max_memories: int = 8
    max_memory_chars: int = 6_000
    max_knowledge: int = 6
    max_knowledge_chars: int = 8_000


@dataclass(slots=True)
class ContextBundle:
    """Everything assembled for one request."""

    conversation_id: str | None = None
    history: list[Message] = field(default_factory=list)
    user_context: str = ""
    project_context: str = ""
    task_context: str = ""
    memory_context: str = ""
    knowledge_context: str = ""
    #: True when anything assembled came from untrusted content. Threaded to
    #: the permission engine, which escalates non-read capabilities (§42).
    tainted: bool = False
    #: Retrieval diagnostics, surfaced by /api/system/prompt and the UI so the
    #: question "why did it remember that?" has an answer.
    retrieval: dict[str, Any] = field(default_factory=dict)
    #: True when history was trimmed — surfaced in the prompt so the model
    #: knows not to claim knowledge of turns it cannot see.
    truncated: bool = False
    stats: dict[str, Any] = field(default_factory=dict)

    @property
    def approx_tokens(self) -> int:
        total = sum(len(m.content or "") for m in self.history)
        total += len(self.user_context) + len(self.project_context)
        total += len(self.task_context) + len(self.memory_context)
        total += len(self.knowledge_context)
        return total // CHARS_PER_TOKEN


class ContextManager:
    def __init__(
        self,
        session: AsyncSession,
        budget: ContextBudget | None = None,
        *,
        embeddings: Any = None,
        memory_enabled: bool = True,
        knowledge_enabled: bool = True,
    ) -> None:
        self.session = session
        self.budget = budget or ContextBudget()
        self.embeddings = embeddings
        self.memory_enabled = memory_enabled
        self.knowledge_enabled = knowledge_enabled

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
            bundle.project_context = await self._load_project_context(project_id)

        if query:
            await self._load_memory(bundle, user_id, query, project_id)
            await self._load_knowledge(bundle, user_id, query, project_id)

        bundle.stats = {
            "history_messages": len(bundle.history),
            "history_truncated": bundle.truncated,
            "approx_tokens": bundle.approx_tokens,
            "memories": len(bundle.retrieval.get("memories", [])),
            "knowledge": len(bundle.retrieval.get("knowledge", [])),
            "tainted": bundle.tainted,
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

    async def _load_project_context(self, project_id: str) -> str:
        """Real project context (§27).

        The project's own summary — state, goals, decisions, task counts —
        rather than the id Phase 1 carried through. Individual project facts
        are left to retrieval; putting them all here would spend the budget on
        things this request may not need.
        """
        from jarvis.memory.projects import ProjectService

        service = ProjectService(self.session)
        try:
            summary = await service.summarise(project_id)
        except Exception as exc:
            log.warning("project_context_failed", project_id=project_id,
                        error=str(exc))
            return f"Working within project {project_id}."
        return ProjectService.to_prompt_block(summary)

    async def _load_memory(
        self,
        bundle: ContextBundle,
        user_id: str,
        query: str,
        project_id: str | None,
    ) -> None:
        """Retrieve the few memories this request needs (§19).

        Failure is non-fatal by design: a turn answered without memory is worse
        than one answered with it, and far better than no turn at all. The
        warning is logged rather than surfaced, because the user asked a
        question, not for a status report on the retriever.
        """
        if not self.memory_enabled:
            return

        from jarvis.memory.retrieval import MemoryRetriever, RetrievalQuery

        try:
            result = await MemoryRetriever(
                self.session, embeddings=self.embeddings
            ).retrieve(
                RetrievalQuery(
                    text=query,
                    user_id=user_id,
                    project_id=project_id,
                    limit=self.budget.max_memories,
                    max_chars=self.budget.max_memory_chars,
                )
            )
        except Exception as exc:
            log.warning("memory_retrieval_failed", error=str(exc))
            return

        bundle.memory_context = result.as_prompt_block(self.budget.max_memory_chars)
        bundle.tainted = bundle.tainted or result.tainted
        bundle.retrieval["memories"] = result.describe()
        bundle.retrieval["memory_ms"] = round(result.duration_ms, 2)
        bundle.retrieval["semantic"] = result.semantic_used

        if result.memories:
            # Retrieval is an access. Recording it is what makes "which
            # memories actually get used?" answerable, and later, prunable.
            from jarvis.memory.service import MemoryService

            await MemoryService(self.session).mark_accessed(
                [item.memory for item in result.memories]
            )

    async def _load_knowledge(
        self,
        bundle: ContextBundle,
        user_id: str,
        query: str,
        project_id: str | None,
    ) -> None:
        if not self.knowledge_enabled:
            return

        from jarvis.knowledge.service import KnowledgeService

        try:
            result = await KnowledgeService(
                self.session, embeddings=self.embeddings
            ).search(
                user_id,
                query,
                limit=self.budget.max_knowledge,
                project_id=project_id,
            )
        except Exception as exc:
            log.warning("knowledge_retrieval_failed", error=str(exc))
            return

        bundle.knowledge_context = result.as_prompt_block(
            self.budget.max_knowledge_chars
        )
        bundle.retrieval["knowledge"] = result.describe()
        bundle.retrieval["knowledge_ms"] = round(result.duration_ms, 2)
        if result.hits:
            # Documents are untrusted input. Anything retrieved from one taints
            # the request whatever the memory retriever concluded.
            bundle.tainted = True
