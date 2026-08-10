"""Conversation and message persistence.

Messages store both rendered ``content`` (for display and search) and the
structured ``blocks`` that produced it (for replay). The distinction matters:
a turn containing tool calls cannot be faithfully reconstructed from prose, and
sending a lossy reconstruction back to a provider produces subtly wrong
behaviour that is very hard to debug.

:meth:`ConversationService.to_provider_messages` is the bridge back to the
provider-neutral types, and it is the only place that reconstruction happens.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Sequence

from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from jarvis.db.base import utcnow
from jarvis.db.models import Conversation, Message, MessageRole
from jarvis.errors import NotFoundError
from jarvis.logging import get_logger
from jarvis.providers.base import (
    ChatMessage,
    ContentBlock,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    Usage,
)

log = get_logger(__name__)

_TITLE_MAX = 60


class ConversationService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # ── conversations ────────────────────────────────────────────────────────

    async def create(
        self,
        *,
        user_id: str,
        title: str | None = None,
        project_id: str | None = None,
    ) -> Conversation:
        conversation = Conversation(
            user_id=user_id,
            title=title or "New conversation",
            project_id=project_id,
        )
        self.session.add(conversation)
        await self.session.flush()
        log.info("conversation_created", conversation_id=conversation.id)
        return conversation

    async def get(self, conversation_id: str) -> Conversation:
        conversation = await self.session.get(Conversation, conversation_id)
        if conversation is None:
            raise NotFoundError(
                f"Conversation {conversation_id} not found",
                user_message="I could not find that conversation.",
            )
        return conversation

    async def get_or_create(
        self, *, user_id: str, conversation_id: str | None
    ) -> Conversation:
        if conversation_id:
            return await self.get(conversation_id)
        return await self.create(user_id=user_id)

    async def list(
        self, user_id: str, *, limit: int = 50, include_archived: bool = False
    ) -> list[Conversation]:
        stmt = select(Conversation).where(Conversation.user_id == user_id)
        if not include_archived:
            stmt = stmt.where(Conversation.archived_at.is_(None))
        stmt = stmt.order_by(desc(Conversation.updated_at)).limit(min(limit, 200))
        return list((await self.session.execute(stmt)).scalars().all())

    async def archive(self, conversation_id: str) -> Conversation:
        conversation = await self.get(conversation_id)
        conversation.archived_at = utcnow()
        await self.session.flush()
        return conversation

    async def rename(self, conversation_id: str, title: str) -> Conversation:
        conversation = await self.get(conversation_id)
        conversation.title = title[:500]
        await self.session.flush()
        return conversation

    async def maybe_autotitle(self, conversation: Conversation, first_text: str) -> None:
        """Derive a title from the opening message.

        A cheap heuristic on purpose — spending a model call to name a
        conversation is not worth the latency on the very first turn. A
        model-generated title can replace this later if it earns its place.
        """
        if conversation.title != "New conversation":
            return
        cleaned = " ".join(first_text.split())
        if not cleaned:
            return
        title = cleaned[:_TITLE_MAX].rstrip()
        if len(cleaned) > _TITLE_MAX:
            title = title.rsplit(" ", 1)[0] + "…"
        conversation.title = title
        await self.session.flush()

    # ── messages ─────────────────────────────────────────────────────────────

    async def _next_sequence(self, conversation_id: str) -> int:
        stmt = select(func.coalesce(func.max(Message.sequence), -1)).where(
            Message.conversation_id == conversation_id
        )
        return (await self.session.execute(stmt)).scalar_one() + 1

    async def add_message(
        self,
        *,
        conversation_id: str,
        role: MessageRole,
        content: str = "",
        blocks: list[ContentBlock] | None = None,
        provider: str | None = None,
        model: str | None = None,
        usage: Usage | None = None,
        stop_reason: str | None = None,
        latency_ms: float | None = None,
        request_id: str | None = None,
        meta: dict[str, Any] | None = None,
    ) -> Message:
        message = Message(
            conversation_id=conversation_id,
            sequence=await self._next_sequence(conversation_id),
            role=role,
            content=content,
            blocks=[_block_to_json(b) for b in blocks] if blocks else None,
            provider=provider,
            model=model,
            input_tokens=usage.input_tokens if usage else None,
            output_tokens=usage.output_tokens if usage else None,
            cost_micros=usage.cost_micros if usage else None,
            stop_reason=stop_reason,
            latency_ms=latency_ms,
            request_id=request_id,
            meta=meta or {},
        )
        self.session.add(message)

        conversation = await self.session.get(Conversation, conversation_id)
        if conversation is not None:
            conversation.updated_at = utcnow()

        await self.session.flush()
        return message

    async def messages(
        self, conversation_id: str, *, limit: int | None = None
    ) -> list[Message]:
        """Chronological. ``limit`` keeps the most *recent* N."""
        stmt = select(Message).where(Message.conversation_id == conversation_id)
        if limit is None:
            stmt = stmt.order_by(Message.sequence.asc())
            return list((await self.session.execute(stmt)).scalars().all())
        stmt = stmt.order_by(Message.sequence.desc()).limit(limit)
        rows = list((await self.session.execute(stmt)).scalars().all())
        return list(reversed(rows))

    # ── provider bridge ──────────────────────────────────────────────────────

    def to_provider_messages(self, messages: Sequence[Message]) -> list[ChatMessage]:
        """Rebuild provider-neutral turns from stored rows.

        System messages are excluded — the system prompt is assembled fresh by
        the prompt builder on every turn, so replaying a stored one would
        double it and pin the conversation to a stale instruction set.

        Tool results are attached to the *user* turn that follows the assistant
        turn requesting them, which is the shape every provider expects.
        """
        out: list[ChatMessage] = []
        pending_results: list[ContentBlock] = []

        for message in messages:
            if message.role is MessageRole.SYSTEM:
                continue

            if message.role is MessageRole.TOOL:
                for raw in message.blocks or []:
                    block = _block_from_json(raw)
                    if isinstance(block, ToolResultBlock):
                        pending_results.append(block)
                continue

            blocks = self._blocks_for(message)
            if not blocks:
                continue

            if message.role is MessageRole.USER:
                if pending_results:
                    blocks = [*pending_results, *blocks]
                    pending_results = []
                out.append(ChatMessage(role="user", content=blocks))
            else:
                out.append(ChatMessage(role="assistant", content=blocks))

        # Tool results with no following user turn still have to be delivered,
        # or the provider sees an unanswered tool_use and rejects the request.
        if pending_results:
            out.append(ChatMessage(role="user", content=pending_results))

        return out

    @staticmethod
    def _blocks_for(message: Message) -> list[ContentBlock]:
        if message.blocks:
            return [_block_from_json(b) for b in message.blocks]
        return [TextBlock(text=message.content)] if message.content else []

    # ── serialisation ────────────────────────────────────────────────────────

    @staticmethod
    def to_dict(conversation: Conversation, *,
                messages: Sequence[Message] | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": conversation.id,
            "title": conversation.title,
            "project_id": conversation.project_id,
            "created_at": _iso(conversation.created_at),
            "updated_at": _iso(conversation.updated_at),
            "archived_at": _iso(conversation.archived_at),
        }
        if messages is not None:
            payload["messages"] = [
                ConversationService.message_to_dict(m) for m in messages
            ]
        return payload

    @staticmethod
    def message_to_dict(message: Message) -> dict[str, Any]:
        return {
            "id": message.id,
            "sequence": message.sequence,
            "role": message.role.value,
            "content": message.content,
            "blocks": message.blocks,
            "provider": message.provider,
            "model": message.model,
            "input_tokens": message.input_tokens,
            "output_tokens": message.output_tokens,
            "cost_micros": message.cost_micros,
            "stop_reason": message.stop_reason,
            "latency_ms": message.latency_ms,
            "created_at": _iso(message.created_at),
        }


# ── block (de)serialisation ──────────────────────────────────────────────────


def _block_to_json(block: ContentBlock) -> dict[str, Any]:
    if isinstance(block, TextBlock):
        return {"type": "text", "text": block.text}
    if isinstance(block, ToolUseBlock):
        return {
            "type": "tool_use",
            "id": block.id,
            "name": block.name,
            "input": block.input,
        }
    if isinstance(block, ToolResultBlock):
        return {
            "type": "tool_result",
            "tool_use_id": block.tool_use_id,
            "content": block.content,
            "is_error": block.is_error,
        }
    raise TypeError(f"Cannot serialise block: {type(block).__name__}")


def _block_from_json(raw: dict[str, Any]) -> ContentBlock:
    kind = raw.get("type")
    if kind == "tool_use":
        return ToolUseBlock(
            id=raw["id"], name=raw["name"], input=raw.get("input") or {}
        )
    if kind == "tool_result":
        return ToolResultBlock(
            tool_use_id=raw["tool_use_id"],
            content=raw.get("content", ""),
            is_error=bool(raw.get("is_error")),
        )
    return TextBlock(text=raw.get("text", ""))


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None
