"""Confirmation infrastructure.

Built as a service rather than a UI component so that agents in later phases
use the same mechanism: anything that needs a human decision creates a
:class:`Confirmation` and the caller suspends.

**Flow.** When the permission engine returns ``ASK``, the tool executor creates
a pending confirmation and raises
:class:`~jarvis.errors.ConfirmationRequiredError`. The orchestrator catches it,
records the pause, and returns the confirmation to the client. The user decides
via the API. On the next turn the executor finds an approved confirmation that
matches the action and proceeds.

**Why resume-on-next-turn rather than holding the request open.** A held HTTP
request dies with the process, the browser tab, or a network blip, and the
half-approved action is then in an unknown state. Persisting the decision and
resuming makes approval survive a restart — which matters much more once
Phase 10 runs long autonomous work.

**Approvals are bound to the exact action.** The action fingerprint is a hash
of the tool name plus its canonicalised arguments, so an approval for
"delete file A" cannot be replayed to authorise "delete file B". Approvals are
also single-use.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from jarvis.db.base import utcnow
from jarvis.db.models import Confirmation, ConfirmationStatus, RiskLevel
from jarvis.errors import NotFoundError, ValidationError
from jarvis.logging import get_logger

log = get_logger(__name__)


def action_fingerprint(tool_name: str, arguments: dict[str, Any]) -> str:
    """Stable hash of an action.

    ``sort_keys`` matters: dict ordering must not change the fingerprint, or an
    approval would fail to match the very action it approved.
    """
    payload = json.dumps(
        {"tool": tool_name, "arguments": arguments}, sort_keys=True, default=str
    )
    return hashlib.sha256(payload.encode()).hexdigest()


@dataclass(slots=True)
class ConfirmationRequest:
    user_id: str
    title: str
    body: str
    tool_name: str
    arguments: dict[str, Any]
    risk_level: RiskLevel = RiskLevel.MEDIUM
    reversible: bool = True
    request_id: str | None = None
    conversation_id: str | None = None
    reason: str | None = None


class ConfirmationService:
    def __init__(self, session: AsyncSession, *, ttl_seconds: int = 900) -> None:
        self.session = session
        self.ttl_seconds = ttl_seconds

    # ── creation ─────────────────────────────────────────────────────────────

    async def request(self, req: ConfirmationRequest) -> Confirmation:
        fingerprint = action_fingerprint(req.tool_name, req.arguments)

        # Reuse an outstanding request for the identical action rather than
        # stacking duplicates when a model retries the same call.
        existing = await self._find_pending(req.user_id, fingerprint)
        if existing is not None:
            return existing

        confirmation = Confirmation(
            user_id=req.user_id,
            request_id=req.request_id,
            conversation_id=req.conversation_id,
            title=req.title,
            body=req.body,
            action={
                "tool": req.tool_name,
                "arguments": req.arguments,
                "fingerprint": fingerprint,
                "reason": req.reason,
            },
            risk_level=req.risk_level,
            reversible=req.reversible,
            status=ConfirmationStatus.PENDING,
            expires_at=utcnow() + timedelta(seconds=self.ttl_seconds),
        )
        self.session.add(confirmation)
        await self.session.flush()
        log.info(
            "confirmation_requested",
            confirmation_id=confirmation.id,
            tool=req.tool_name,
            risk=req.risk_level.value,
            reversible=req.reversible,
        )
        return confirmation

    # ── lookup ───────────────────────────────────────────────────────────────

    async def get(self, confirmation_id: str) -> Confirmation:
        confirmation = await self.session.get(Confirmation, confirmation_id)
        if confirmation is None:
            raise NotFoundError(f"Confirmation {confirmation_id} not found")
        return self._expire_if_due(confirmation)

    async def list_pending(self, user_id: str) -> list[Confirmation]:
        stmt = (
            select(Confirmation)
            .where(
                Confirmation.user_id == user_id,
                Confirmation.status == ConfirmationStatus.PENDING,
            )
            .order_by(Confirmation.created_at.desc())
        )
        rows = (await self.session.execute(stmt)).scalars().all()
        return [c for c in (self._expire_if_due(r) for r in rows)
                if c.status is ConfirmationStatus.PENDING]

    async def _find_pending(
        self, user_id: str, fingerprint: str
    ) -> Confirmation | None:
        for candidate in await self.list_pending(user_id):
            if candidate.action.get("fingerprint") == fingerprint:
                return candidate
        return None

    async def find_approval(
        self, user_id: str, tool_name: str, arguments: dict[str, Any]
    ) -> Confirmation | None:
        """An unconsumed approval matching exactly this action, if one exists."""
        fingerprint = action_fingerprint(tool_name, arguments)
        stmt = (
            select(Confirmation)
            .where(
                Confirmation.user_id == user_id,
                Confirmation.status == ConfirmationStatus.APPROVED,
            )
            .order_by(Confirmation.decided_at.desc())
        )
        for row in (await self.session.execute(stmt)).scalars().all():
            if row.action.get("fingerprint") != fingerprint:
                continue
            if row.action.get("consumed"):
                continue
            return row
        return None

    async def consume(self, confirmation: Confirmation) -> None:
        """Mark an approval used, so it authorises exactly one execution."""
        action = dict(confirmation.action)
        action["consumed"] = True
        action["consumed_at"] = utcnow().isoformat()
        confirmation.action = action
        await self.session.flush()

    # ── decision ─────────────────────────────────────────────────────────────

    async def decide(
        self,
        confirmation_id: str,
        *,
        approved: bool,
        decided_by: str = "user",
        note: str | None = None,
    ) -> Confirmation:
        confirmation = await self.get(confirmation_id)

        if confirmation.status is ConfirmationStatus.EXPIRED:
            raise ValidationError(
                f"Confirmation {confirmation_id} expired",
                user_message="That confirmation expired. Ask me again if you still want it.",
            )
        if confirmation.status is not ConfirmationStatus.PENDING:
            raise ValidationError(
                f"Confirmation {confirmation_id} already {confirmation.status.value}",
                user_message="That has already been decided.",
            )

        confirmation.status = (
            ConfirmationStatus.APPROVED if approved else ConfirmationStatus.DENIED
        )
        confirmation.decided_by = decided_by
        confirmation.decision_note = note
        confirmation.decided_at = utcnow()
        await self.session.flush()

        log.info(
            "confirmation_resolved",
            confirmation_id=confirmation.id,
            approved=approved,
            tool=confirmation.action.get("tool"),
        )
        return confirmation

    # ── expiry ───────────────────────────────────────────────────────────────

    def _expire_if_due(self, confirmation: Confirmation) -> Confirmation:
        if confirmation.status is not ConfirmationStatus.PENDING:
            return confirmation
        expires = confirmation.expires_at
        if expires is None:
            return confirmation
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if expires <= datetime.now(timezone.utc):
            confirmation.status = ConfirmationStatus.EXPIRED
            log.info("confirmation_expired", confirmation_id=confirmation.id)
        return confirmation

    @staticmethod
    def to_dict(confirmation: Confirmation) -> dict[str, Any]:
        return {
            "id": confirmation.id,
            "title": confirmation.title,
            "body": confirmation.body,
            "tool": confirmation.action.get("tool"),
            "arguments": confirmation.action.get("arguments", {}),
            "reason": confirmation.action.get("reason"),
            "risk_level": confirmation.risk_level.value,
            "reversible": confirmation.reversible,
            "status": confirmation.status.value,
            "created_at": confirmation.created_at.isoformat()
            if confirmation.created_at
            else None,
            "expires_at": confirmation.expires_at.isoformat()
            if confirmation.expires_at
            else None,
        }
