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
also single-use, and they go stale: an approval that was never consumed stops
authorising anything once :attr:`ConfirmationService.approval_ttl_seconds` has
passed since the decision. Without that, an approval the user gave and then
abandoned — the turn errored, the tab closed — would still silently authorise
the identical action weeks later, which is exactly the kind of latent grant
prompt injection looks for.
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
    #: The real arguments. The fingerprint is computed over these, so they must
    #: be exactly what the caller will present again on the retry.
    arguments: dict[str, Any]
    #: What gets *stored* in the record, when that must differ. Only tools
    #: declaring ``redact_arguments`` set this — a value the user is typing into
    #: a web page has no business surviving in the database after the approval
    #: is spent. The fingerprint is unaffected, so matching still works on the
    #: real arguments; this is the display copy alone.
    stored_arguments: dict[str, Any] | None = None
    risk_level: RiskLevel = RiskLevel.MEDIUM
    reversible: bool = True
    #: How far the action reaches. Derived by the executor from the tool rather
    #: than declared here, so nothing has to remember to set it.
    impact: str = "write"
    request_id: str | None = None
    conversation_id: str | None = None
    reason: str | None = None


class ConfirmationService:
    def __init__(
        self,
        session: AsyncSession,
        *,
        ttl_seconds: int = 900,
        approval_ttl_seconds: int | None = None,
    ) -> None:
        self.session = session
        #: How long a *pending* request stays answerable.
        self.ttl_seconds = ttl_seconds
        #: How long an *approved but unconsumed* decision keeps authorising its
        #: action. Defaults to the same window as the request itself.
        self.approval_ttl_seconds = (
            ttl_seconds if approval_ttl_seconds is None else approval_ttl_seconds
        )

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
                # Never ``req.arguments`` directly: see ``stored_arguments``.
                # The fingerprint above is the real one either way.
                "arguments": (
                    req.arguments
                    if req.stored_arguments is None
                    else req.stored_arguments
                ),
                "fingerprint": fingerprint,
                "reason": req.reason,
            },
            risk_level=req.risk_level,
            impact=req.impact,
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
        """A fresh, unconsumed approval matching exactly this action.

        Three conditions, all necessary: the fingerprint matches (so an
        approval cannot be transplanted onto a different action), it has not
        already been used (single-use), and it is not stale (an approval the
        user walked away from does not accumulate into a standing grant).
        """
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
            if self._approval_is_stale(row):
                row.status = ConfirmationStatus.EXPIRED
                log.info("approval_expired_unused", confirmation_id=row.id)
                continue
            return row
        return None

    def _approval_is_stale(self, confirmation: Confirmation) -> bool:
        decided = confirmation.decided_at
        if decided is None:  # approved without a timestamp — treat as unusable
            return True
        if decided.tzinfo is None:
            decided = decided.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - decided).total_seconds()
        return age > self.approval_ttl_seconds

    async def consume(self, confirmation: Confirmation) -> None:
        """Mark an approval used, so it authorises exactly one execution."""
        action = dict(confirmation.action)
        action["consumed"] = True
        action["consumed_at"] = utcnow().isoformat()
        confirmation.action = action
        await self.session.flush()

    # ── decision ─────────────────────────────────────────────────────────────

    #: Channels a destructive action may never be approved through.
    #:
    #: Straight from `vierisid/jarvis`, and its reasoning is the right one: a
    #: single misheard syllable could trigger a payment. Speech recognition
    #: mishears, a podcast in the background says "yes", somebody else in the
    #: room answers. For a destructive action the deliberate act — a click, an
    #: explicit API call — is the only authoritative path.
    #:
    #: Denials are exempt. Refusing to act on a mishearing would mean the
    #: cautious answer is the one the system ignores.
    UNSAFE_FOR_DESTRUCTIVE = frozenset({"voice"})

    async def decide(
        self,
        confirmation_id: str,
        *,
        approved: bool,
        decided_by: str = "user",
        note: str | None = None,
        channel: str = "ui",
    ) -> Confirmation:
        confirmation = await self.get(confirmation_id)

        if (
            approved
            and channel in self.UNSAFE_FOR_DESTRUCTIVE
            and (confirmation.impact or "") == "destructive"
        ):
            raise ValidationError(
                f"Confirmation {confirmation_id} is destructive and cannot be "
                f"approved by {channel}",
                user_message=(
                    "That one cannot be approved by voice — it cannot be "
                    "undone. Approve it in the Command Center instead."
                ),
            )

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
        confirmation.resolution_channel = channel
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
            # The axis a person actually reasons about: can I take this back?
            # Surfaced so the UI can render destructive differently rather than
            # leaving the user to infer it from risk_level plus reversible.
            "impact": confirmation.impact,
            "resolution_channel": confirmation.resolution_channel,
            "status": confirmation.status.value,
            "created_at": confirmation.created_at.isoformat()
            if confirmation.created_at
            else None,
            "expires_at": confirmation.expires_at.isoformat()
            if confirmation.expires_at
            else None,
        }
