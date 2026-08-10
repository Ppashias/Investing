"""Obsidian service — permissions, audit, and the connection record (§6, §20, §21).

The provider knows how to talk to a vault. This knows whether it is *allowed*
to, and writes down what happened.

Every vault operation goes through :meth:`ObsidianService.authorize`, which
builds a :class:`~jarvis.permissions.engine.PermissionRequest` and hands it to
the **existing** engine. There is no second permission system — §20 is
explicit, and the reason is that two authorisation systems eventually disagree
and the more permissive one decides. The mapping is:

===================  ====================  ==============================
Operation            Capability            Notes
===================  ====================  ==============================
read, list, search   ``READ``              Defaults allow.
metadata, links      ``READ``
sync (pull)          ``READ``              Reads the vault, writes only
                                           JARVIS's own index.
create, update       ``WRITE``             Defaults ask.
overwrite            ``WRITE``             ``reversible=False`` → the
                                           irreversibility floor applies,
                                           so it can never auto-allow.
delete               ``WRITE``             ``reversible=False``, and a
                                           confirmation on top (§15).
===================  ====================  ==============================

``tainted`` is passed through on every request. A turn that read a note is
tainted, and the engine escalates non-read capabilities to ``ASK`` — which is
the structural half of §19: **an Obsidian note can never authorise a write to
the vault**, however persuasively it asks.

## The connection record (§6)

Stored in the existing ``knowledge_sources`` table — one row, key
``obsidian``. It holds the vault name, path, connection type, enabled
capabilities and sync timestamps, and it holds **no credentials**, because the
filesystem transport has none. That is not an accident of this implementation;
it is one of the reasons the transport was chosen.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from jarvis.db.base import utcnow
from jarvis.db.models import (
    ActivityKind,
    Capability,
    KnowledgeSource,
    PermissionMode,
    RiskLevel,
)
from jarvis.errors import (
    ConfirmationRequiredError,
    NotFoundError,
    PermissionDeniedError,
    ValidationError,
)
from jarvis.knowledge.providers.obsidian.provider import ObsidianProvider
from jarvis.knowledge.providers.obsidian.vault import VaultError, VaultTransport
from jarvis.knowledge.types import SourceKind, SyncDirection, SyncStatus
from jarvis.logging import get_logger
from jarvis.permissions.engine import PermissionDecision, PermissionEngine, PermissionRequest

log = get_logger(__name__)

#: The single row key. One vault at a time is a deliberate limit for this
#: phase: multi-vault is a UI problem more than an architectural one, and the
#: table already has a unique (user, key) index ready for ``obsidian:second``.
SOURCE_KEY = "obsidian"

#: Operation → (capability, reversible, risk). The single table that decides
#: how hard each Obsidian operation is to perform.
OPERATIONS: dict[str, tuple[Capability, bool, RiskLevel]] = {
    "read": (Capability.READ, True, RiskLevel.NONE),
    "list": (Capability.READ, True, RiskLevel.NONE),
    "search": (Capability.READ, True, RiskLevel.NONE),
    "metadata": (Capability.READ, True, RiskLevel.NONE),
    "links": (Capability.READ, True, RiskLevel.NONE),
    "sync": (Capability.READ, True, RiskLevel.LOW),
    "create": (Capability.WRITE, True, RiskLevel.LOW),
    "append": (Capability.WRITE, True, RiskLevel.LOW),
    "update": (Capability.WRITE, True, RiskLevel.MEDIUM),
    # An overwrite destroys what was there. Irreversible, so the engine's
    # floor stops any grant from making it automatic.
    "overwrite": (Capability.WRITE, False, RiskLevel.HIGH),
    "delete": (Capability.WRITE, False, RiskLevel.HIGH),
    "move": (Capability.WRITE, False, RiskLevel.MEDIUM),
}

#: Operations that always meet a human, whatever the grants say (§14, §15).
ALWAYS_CONFIRM = frozenset({"delete", "overwrite"})


@dataclass(slots=True)
class ObsidianConfig:
    """Non-secret connection configuration (§6)."""

    vault_name: str = ""
    vault_path: str = ""
    connection_type: str = "filesystem"
    enabled: bool = False
    allow_writes: bool = False
    allow_deletes: bool = False
    sync_direction: SyncDirection = SyncDirection.PULL
    last_connected_at: datetime | None = None
    last_synced_at: datetime | None = None
    sync_status: SyncStatus = SyncStatus.NEVER_SYNCED
    last_error: str | None = None
    document_count: int = 0

    def to_dict(self, *, mask_path: bool = True) -> dict[str, Any]:
        return {
            "vault_name": self.vault_name,
            # §7 says "masked or appropriately displayed". A vault path is not
            # a credential, but it is a personal filesystem path, so the UI
            # gets the tail and the API can ask for the whole thing.
            "vault_path": _mask(self.vault_path) if mask_path else self.vault_path,
            "connection_type": self.connection_type,
            "enabled": self.enabled,
            "allow_writes": self.allow_writes,
            "allow_deletes": self.allow_deletes,
            "sync_direction": self.sync_direction.value,
            "sync_status": self.sync_status.value,
            "last_connected_at": (
                self.last_connected_at.isoformat() if self.last_connected_at else None
            ),
            "last_synced_at": (
                self.last_synced_at.isoformat() if self.last_synced_at else None
            ),
            "last_error": self.last_error,
            "indexed_notes": self.document_count,
        }


class ObsidianService:
    """Connection lifecycle, permission checks and audit for one user."""

    def __init__(
        self,
        session: AsyncSession,
        user_id: str,
        *,
        activity: Any = None,
        confirmations: Any = None,
    ) -> None:
        self.session = session
        self.user_id = user_id
        self.activity = activity
        self.confirmations = confirmations

    # ── configuration (§6) ───────────────────────────────────────────────────

    async def row(self) -> KnowledgeSource | None:
        return (
            await self.session.execute(
                select(KnowledgeSource).where(
                    KnowledgeSource.user_id == self.user_id,
                    KnowledgeSource.key == SOURCE_KEY,
                )
            )
        ).scalars().first()

    async def config(self) -> ObsidianConfig:
        row = await self.row()
        if row is None:
            return ObsidianConfig()
        raw = row.config or {}
        return ObsidianConfig(
            vault_name=row.name,
            vault_path=str(raw.get("vault_path") or ""),
            connection_type=str(raw.get("connection_type") or "filesystem"),
            enabled=bool(row.enabled),
            allow_writes=bool(raw.get("allow_writes")),
            allow_deletes=bool(raw.get("allow_deletes")),
            sync_direction=row.sync_direction,
            last_connected_at=_parse(raw.get("last_connected_at")),
            last_synced_at=row.last_synced_at,
            sync_status=row.sync_status,
            last_error=row.last_error,
            document_count=row.document_count,
        )

    async def connect(
        self,
        vault_path: str,
        *,
        vault_name: str | None = None,
        allow_writes: bool = False,
        allow_deletes: bool = False,
    ) -> dict[str, Any]:
        """Point JARVIS at a vault and verify it is really there.

        The verification is not optional and its result is not assumed: the
        vault is walked, the note count comes back from the walk, and a failure
        is recorded as a failure. §5's complaint about the previous attempt was
        that a provider existed with nothing behind it — a connect that cannot
        prove it reached a vault must not report CONNECTED.
        """
        transport = VaultTransport(vault_path, name=vault_name)
        try:
            info = transport.check()
        except VaultError as exc:
            await self._audit(
                "connect", status="ERROR", detail={"error": exc.user_message},
                summary=f"Obsidian connection failed: {exc.user_message}",
            )
            await self._store_error(vault_path, vault_name, exc.user_message)
            raise

        row = await self.row()
        if row is None:
            row = KnowledgeSource(
                user_id=self.user_id, key=SOURCE_KEY, kind=SourceKind.OBSIDIAN,
                name=info.name,
            )
            self.session.add(row)

        row.name = info.name
        row.enabled = True
        row.kind = SourceKind.OBSIDIAN
        row.sync_direction = SyncDirection.PULL
        row.sync_status = SyncStatus.PENDING
        row.last_error = None
        row.config = {
            "vault_path": str(transport.root),
            "connection_type": transport.kind,
            "allow_writes": bool(allow_writes and transport.writable),
            "allow_deletes": bool(allow_deletes and transport.writable),
            "has_obsidian_config": info.has_obsidian_config,
            "last_connected_at": utcnow().isoformat(),
        }
        await self.session.flush()

        await self._audit(
            "connect", status="CONNECTED",
            summary=f"Connected to Obsidian vault '{info.name}' ({info.note_count} notes)",
            detail={
                "vault": info.name,
                "notes": info.note_count,
                "folders": info.folder_count,
                "writable": transport.writable,
                "has_obsidian_config": info.has_obsidian_config,
            },
        )
        log.info("obsidian_connected", vault=info.name, notes=info.note_count,
                 writable=transport.writable)

        return {
            "connected": True,
            "vault": info.name,
            "notes": info.note_count,
            "folders": info.folder_count,
            "writable": transport.writable,
            "has_obsidian_config": info.has_obsidian_config,
        }

    async def disconnect(self, *, forget_index: bool = False) -> dict[str, Any]:
        """Stop using the vault. The vault itself is never touched.

        ``forget_index`` drops the documents JARVIS ingested from it. Off by
        default: disconnecting a source and destroying everything learned from
        it are different intentions, and conflating them makes "disconnect"
        frightening.
        """
        row = await self.row()
        if row is None:
            return {"connected": False, "detail": "No vault was configured."}

        removed = 0
        if forget_index:
            from jarvis.db.models import Document
            from jarvis.knowledge.ingestion.pipeline import IngestionPipeline

            pipeline = IngestionPipeline(self.session)
            documents = (
                await self.session.execute(
                    select(Document).where(
                        Document.user_id == self.user_id,
                        Document.source_kind == SourceKind.OBSIDIAN,
                    )
                )
            ).scalars().all()
            for document in documents:
                await pipeline.delete_document(document)
                removed += 1

        row.enabled = False
        row.sync_status = SyncStatus.DISABLED
        row.document_count = 0 if forget_index else row.document_count
        await self.session.flush()

        await self._audit(
            "disconnect", status="DISCONNECTED",
            summary=f"Disconnected from Obsidian vault '{row.name}'",
            detail={"forgot_index": forget_index, "documents_removed": removed},
        )
        return {"connected": False, "documents_removed": removed}

    async def set_permissions(
        self, *, allow_writes: bool | None = None, allow_deletes: bool | None = None
    ) -> ObsidianConfig:
        """Change what JARVIS may do to an already-connected vault.

        Separate from :meth:`connect` because the connection and the
        permissions are different decisions with different lifetimes — the
        vault stays the same while "may JARVIS write to it" changes.
        """
        row = await self.row()
        if row is None:
            return await self.config()

        config = dict(row.config or {})
        if allow_writes is not None:
            config["allow_writes"] = bool(allow_writes)
        if allow_deletes is not None:
            config["allow_deletes"] = bool(allow_deletes)
        row.config = config
        await self.session.flush()

        log.info(
            "obsidian_permissions_updated",
            vault=row.name,
            allow_writes=config.get("allow_writes"),
            allow_deletes=config.get("allow_deletes"),
        )
        return await self.config()

    async def provider(self) -> ObsidianProvider | None:
        """The live provider, or ``None`` when no vault is configured.

        Returning ``None`` rather than raising is what makes §22 work: every
        caller treats an absent vault as "this source has nothing to offer"
        and carries on with memory and previously indexed knowledge.
        """
        config = await self.config()
        if not config.enabled or not config.vault_path:
            return None
        row = await self.row()
        return ObsidianProvider(
            VaultTransport(config.vault_path, name=config.vault_name),
            vault_id=row.id if row else None,
            allow_writes=config.allow_writes,
            last_synced_at=config.last_synced_at,
            document_count=config.document_count,
        )

    async def require_provider(self) -> ObsidianProvider:
        provider = await self.provider()
        if provider is None:
            raise NotFoundError(
                "No Obsidian vault is connected",
                user_message=(
                    "No Obsidian vault is connected. Connect one in the "
                    "Obsidian panel first."
                ),
            )
        return provider

    async def test(self) -> dict[str, Any]:
        """Re-verify the stored configuration against the real vault."""
        config = await self.config()
        if not config.vault_path:
            return {"connected": False, "detail": "No vault is configured."}

        transport = VaultTransport(config.vault_path, name=config.vault_name)
        row = await self.row()
        try:
            info = transport.check()
        except VaultError as exc:
            if row is not None:
                row.sync_status = SyncStatus.ERROR
                row.last_error = exc.user_message
                await self.session.flush()
            await self._audit("test", status="ERROR",
                              summary=f"Obsidian test failed: {exc.user_message}",
                              detail={"error": exc.user_message})
            return {"connected": False, "detail": exc.user_message}

        if row is not None:
            row.last_error = None
            await self.session.flush()
        await self._audit("test", status="OK",
                          summary=f"Obsidian vault '{info.name}' reachable "
                                  f"({info.note_count} notes)",
                          detail={"notes": info.note_count})
        return {
            "connected": True,
            "vault": info.name,
            "notes": info.note_count,
            "folders": info.folder_count,
            "writable": transport.writable,
        }

    async def mark_synced(self, result: Any) -> None:
        row = await self.row()
        if row is None:
            return
        row.last_synced_at = utcnow()
        row.sync_status = (
            SyncStatus.CONFLICT if getattr(result, "conflicts", None)
            else SyncStatus.SYNCED
        )
        from jarvis.db.models import Document

        row.document_count = int(
            (
                await self.session.execute(
                    select(Document.id).where(
                        Document.user_id == self.user_id,
                        Document.source_kind == SourceKind.OBSIDIAN,
                    )
                )
            ).scalars().all().__len__()
        )
        await self.session.flush()

    # ── permissions (§20) ────────────────────────────────────────────────────

    async def authorize(
        self,
        operation: str,
        *,
        target: str = "",
        tainted: bool = False,
        actor: str = "user",
    ) -> PermissionDecision:
        """The single authorisation point for vault operations."""
        if operation not in OPERATIONS:
            raise ValidationError(f"Unknown Obsidian operation {operation!r}")

        capability, reversible, risk = OPERATIONS[operation]
        config = await self.config()

        # Operator switches are checked before the engine, because a scope the
        # user turned off is not a question about permission — it is a feature
        # they disabled, and the answer should say so.
        if capability is Capability.WRITE and not config.allow_writes:
            return PermissionDecision(
                mode=PermissionMode.DENY,
                reason="Writing to the vault is switched off for this source.",
                capability=capability,
                resource=_resource(operation, target),
                applied_rules=["obsidian_writes_disabled"],
            )
        if operation == "delete" and not config.allow_deletes:
            return PermissionDecision(
                mode=PermissionMode.DENY,
                reason="Deleting notes is switched off for this source.",
                capability=capability,
                resource=_resource(operation, target),
                applied_rules=["obsidian_deletes_disabled"],
            )

        decision = await PermissionEngine(self.session).evaluate(
            PermissionRequest(
                user_id=self.user_id,
                capability=capability,
                resource=_resource(operation, target),
                risk_level=risk,
                reversible=reversible,
                tainted=tainted,
            )
        )

        # §15: some operations meet a human whatever the grants say. The engine
        # can be configured permissively by a broad grant; this cannot.
        if operation in ALWAYS_CONFIRM and decision.mode is PermissionMode.ALLOW:
            decision.mode = PermissionMode.ASK
            decision.reason = f"{operation} always requires confirmation."
            decision.applied_rules.append("obsidian_always_confirm")

        return decision

    async def guard(
        self,
        operation: str,
        *,
        target: str = "",
        tainted: bool = False,
        actor: str = "user",
        confirmation_body: str | None = None,
        arguments: dict[str, Any] | None = None,
    ) -> None:
        """Authorise or stop. Raises on DENY and on ASK.

        The ``ASK`` path raises :class:`ConfirmationRequiredError` with a
        persisted confirmation, reusing Phase 1's fingerprint-bound, single-use
        approvals — so approving one deletion cannot authorise a different one.
        """
        decision = await self.authorize(
            operation, target=target, tainted=tainted, actor=actor
        )

        if decision.mode is PermissionMode.DENY:
            await self._audit(
                operation, status="DENIED", target=target,
                summary=f"Obsidian {operation} denied: {decision.reason}",
                detail={"reason": decision.reason, "rules": decision.applied_rules},
            )
            # 403, not 422: the request was well-formed and the answer is no.
            raise PermissionDeniedError(
                f"Obsidian {operation} denied: {decision.reason}",
                capability=decision.capability.value,
                tool=f"obsidian:{operation}",
                reason=decision.reason,
                user_message=decision.reason,
            )

        if decision.mode is PermissionMode.ASK:
            if self.confirmations is None:
                await self._audit(
                    operation, status="DENIED", target=target,
                    summary=f"Obsidian {operation} needs approval and none can "
                            "be requested here",
                )
                raise PermissionDeniedError(
                    f"Obsidian {operation} requires confirmation",
                    tool=f"obsidian:{operation}",
                    user_message=(
                        f"{operation.capitalize()} needs your approval, and this "
                        "path cannot ask for it."
                    ),
                )

            from jarvis.confirmations.service import ConfirmationRequest

            fingerprint = {"operation": operation, "target": target, **(arguments or {})}
            existing = await self.confirmations.find_approval(
                self.user_id, f"obsidian:{operation}", fingerprint
            )
            if existing is not None:
                await self.confirmations.consume(existing)
                return

            capability, reversible, risk = OPERATIONS[operation]
            confirmation = await self.confirmations.request(
                ConfirmationRequest(
                    user_id=self.user_id,
                    title=f"Allow JARVIS to {operation} in your Obsidian vault?",
                    body=confirmation_body
                    or f"{operation} — {target or 'the vault'}",
                    tool_name=f"obsidian:{operation}",
                    arguments=fingerprint,
                    risk_level=risk,
                    reversible=reversible,
                    reason=decision.reason,
                )
            )
            await self._audit(
                operation, status="AWAITING_CONFIRMATION", target=target,
                summary=f"Obsidian {operation} awaiting your approval",
                detail={"confirmation_id": confirmation.id},
            )
            raise ConfirmationRequiredError(
                f"Confirmation required for obsidian:{operation}",
                confirmation_id=confirmation.id,
                user_message=(
                    f"I need your approval to {operation} "
                    f"{target or 'in your vault'}."
                ),
            )

    # ── audit (§21) ──────────────────────────────────────────────────────────

    async def _audit(
        self,
        operation: str,
        *,
        summary: str,
        status: str,
        target: str = "",
        detail: dict[str, Any] | None = None,
        actor: str = "user",
        duration_ms: float | None = None,
    ) -> None:
        """Write one row to the append-only activity log.

        Phase 3's ``computer_audit`` table is not reused: it is shaped around
        screen actions — risk, verification, before/after screenshots — and a
        note read has none of those. The activity log is the system's general
        append-only record, it already backs "what did JARVIS do?", and it is
        what the UI streams. Same audit system, right table.
        """
        if self.activity is None:
            return
        await self.activity.record(
            ActivityKind.OBSIDIAN_ACTION,
            summary=summary,
            actor=actor,
            status=status,
            tool_name=f"obsidian:{operation}",
            duration_ms=duration_ms,
            detail={"operation": operation, "target": target, **(detail or {})},
        )

    async def audit(self, operation: str, **kwargs: Any) -> None:
        """Public wrapper — callers record their own outcomes."""
        await self._audit(operation, **kwargs)

    async def _store_error(
        self, vault_path: str, vault_name: str | None, message: str
    ) -> None:
        row = await self.row()
        if row is None:
            row = KnowledgeSource(
                user_id=self.user_id, key=SOURCE_KEY, kind=SourceKind.OBSIDIAN,
                name=vault_name or Path(vault_path).name or "Obsidian",
                enabled=False,
            )
            self.session.add(row)
        row.sync_status = SyncStatus.ERROR
        row.last_error = message
        row.config = {
            **(row.config or {}),
            "vault_path": vault_path,
            "connection_type": "filesystem",
        }
        await self.session.flush()


def _resource(operation: str, target: str) -> str:
    """Resource scope for the permission engine.

    Shaped like the rest of the system's scopes (``tool:*``, ``computer:*``) so
    a grant can be written at any level: ``knowledge:obsidian:*`` for the whole
    vault, ``knowledge:obsidian:delete`` for one operation. Most-specific-wins
    is the engine's existing rule and needs nothing new here.
    """
    scope = f"knowledge:obsidian:{operation}"
    return f"{scope}:{target}" if target else scope


def _mask(path: str) -> str:
    """Show enough of a path to recognise the vault, not the whole tree."""
    if not path:
        return ""
    parts = Path(path).parts
    if len(parts) <= 2:
        return path
    return f"…/{'/'.join(parts[-2:])}"


def _parse(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None
