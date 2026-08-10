"""Permission engine and confirmation flow.

These are the security-critical tests. Each of the engine's fail-safe rules
gets a test that would fail if the rule were removed.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from jarvis.confirmations.service import (
    ConfirmationRequest,
    ConfirmationService,
    action_fingerprint,
)
from jarvis.db.base import utcnow
from jarvis.db.models import (
    Capability,
    ConfirmationStatus,
    PermissionGrant,
    PermissionMode,
    RiskLevel,
    ToolDefinition,
)
from jarvis.errors import ValidationError
from jarvis.permissions.engine import PermissionEngine, PermissionRequest


# ── defaults ─────────────────────────────────────────────────────────────────


async def test_read_is_allowed_by_default(session, user) -> None:
    decision = await PermissionEngine(session).evaluate(
        PermissionRequest(user_id=user.id, capability=Capability.READ,
                          resource="tool:anything")
    )
    assert decision.mode is PermissionMode.ALLOW


async def test_sensitive_action_is_denied_by_default(session, user) -> None:
    decision = await PermissionEngine(session).evaluate(
        PermissionRequest(
            user_id=user.id,
            capability=Capability.SENSITIVE_ACTION,
            resource="tool:transfer_money",
        )
    )
    assert decision.mode is PermissionMode.DENY


async def test_ungranted_write_asks_rather_than_allows(session, user) -> None:
    decision = await PermissionEngine(session).evaluate(
        PermissionRequest(user_id=user.id, capability=Capability.WRITE,
                          resource="tool:not_granted")
    )
    assert decision.mode is PermissionMode.ASK


async def test_seeded_grant_allows_named_tool(session, user) -> None:
    decision = await PermissionEngine(session).evaluate(
        PermissionRequest(user_id=user.id, capability=Capability.WRITE,
                          resource="tool:create_task", risk_level=RiskLevel.LOW)
    )
    assert decision.mode is PermissionMode.ALLOW


# ── fail-safe rules ──────────────────────────────────────────────────────────


async def test_irreversible_action_is_never_auto_allowed(session, user) -> None:
    """The reversibility floor. Removing it would let a broad grant silently
    authorise something that cannot be undone."""
    session.add(
        PermissionGrant(
            user_id=user.id,
            capability=Capability.WRITE,
            resource_scope="*",
            mode=PermissionMode.ALLOW,
        )
    )
    await session.flush()

    decision = await PermissionEngine(session).evaluate(
        PermissionRequest(
            user_id=user.id,
            capability=Capability.WRITE,
            resource="tool:delete_everything",
            reversible=False,
        )
    )
    assert decision.mode is PermissionMode.ASK
    assert "irreversible_floor" in decision.applied_rules


async def test_sensitive_action_cannot_be_auto_allowed_even_when_granted(
    session, user
) -> None:
    session.add(
        PermissionGrant(
            user_id=user.id,
            capability=Capability.SENSITIVE_ACTION,
            resource_scope="*",
            mode=PermissionMode.ALLOW,
        )
    )
    await session.flush()

    decision = await PermissionEngine(session).evaluate(
        PermissionRequest(
            user_id=user.id,
            capability=Capability.SENSITIVE_ACTION,
            resource="tool:wire_transfer",
        )
    )
    assert decision.mode is PermissionMode.ASK
    assert "sensitive_floor" in decision.applied_rules


async def test_untrusted_context_escalates_writes(session, user) -> None:
    """Taint tracking — the structural defence against prompt injection."""
    decision = await PermissionEngine(session).evaluate(
        PermissionRequest(
            user_id=user.id,
            capability=Capability.WRITE,
            resource="tool:create_task",
            risk_level=RiskLevel.LOW,
            tainted=True,
        )
    )
    assert decision.mode is PermissionMode.ASK
    assert "taint_escalation" in decision.applied_rules


async def test_untrusted_context_does_not_escalate_reads(session, user) -> None:
    decision = await PermissionEngine(session).evaluate(
        PermissionRequest(user_id=user.id, capability=Capability.READ,
                          resource="tool:list_tasks", tainted=True)
    )
    assert decision.mode is PermissionMode.ALLOW


async def test_risk_ceiling_downgrades_allow_to_ask(session, user) -> None:
    decision = await PermissionEngine(session).evaluate(
        PermissionRequest(
            user_id=user.id,
            capability=Capability.WRITE,
            resource="tool:create_task",
            risk_level=RiskLevel.CRITICAL,   # grant ceiling is LOW
        )
    )
    assert decision.mode is PermissionMode.ASK


# ── scope matching ───────────────────────────────────────────────────────────


async def test_most_specific_scope_wins(session, user) -> None:
    session.add_all(
        [
            PermissionGrant(user_id=user.id, capability=Capability.EXECUTE,
                            resource_scope="*", mode=PermissionMode.ALLOW),
            PermissionGrant(user_id=user.id, capability=Capability.EXECUTE,
                            resource_scope="tool:dangerous", mode=PermissionMode.DENY),
        ]
    )
    await session.flush()

    engine = PermissionEngine(session)
    specific = await engine.evaluate(
        PermissionRequest(user_id=user.id, capability=Capability.EXECUTE,
                          resource="tool:dangerous")
    )
    other = await engine.evaluate(
        PermissionRequest(user_id=user.id, capability=Capability.EXECUTE,
                          resource="tool:harmless")
    )
    assert specific.mode is PermissionMode.DENY
    assert other.mode is PermissionMode.ALLOW


async def test_expired_grant_is_ignored(session, user) -> None:
    session.add(
        PermissionGrant(
            user_id=user.id,
            capability=Capability.EXECUTE,
            resource_scope="tool:x",
            mode=PermissionMode.ALLOW,
            expires_at=utcnow() - timedelta(hours=1),
        )
    )
    await session.flush()

    decision = await PermissionEngine(session).evaluate(
        PermissionRequest(user_id=user.id, capability=Capability.EXECUTE,
                          resource="tool:x")
    )
    assert decision.mode is PermissionMode.ASK  # falls back to default


async def test_revoked_grant_is_ignored(session, user) -> None:
    session.add(
        PermissionGrant(
            user_id=user.id,
            capability=Capability.EXECUTE,
            resource_scope="tool:y",
            mode=PermissionMode.ALLOW,
            revoked_at=utcnow(),
        )
    )
    await session.flush()

    decision = await PermissionEngine(session).evaluate(
        PermissionRequest(user_id=user.id, capability=Capability.EXECUTE,
                          resource="tool:y")
    )
    assert decision.mode is PermissionMode.ASK


async def test_disabled_tool_is_denied(session, user) -> None:
    session.add(
        ToolDefinition(name="create_task", capability=Capability.WRITE, enabled=False)
    )
    await session.flush()

    decision = await PermissionEngine(session).evaluate(
        PermissionRequest(
            user_id=user.id,
            capability=Capability.WRITE,
            resource="tool:create_task",
            tool_name="create_task",
        )
    )
    assert decision.mode is PermissionMode.DENY


async def test_tool_mode_override_beats_grant(session, user) -> None:
    session.add(
        ToolDefinition(
            name="create_task",
            capability=Capability.WRITE,
            enabled=True,
            mode_override=PermissionMode.ASK,
        )
    )
    await session.flush()

    decision = await PermissionEngine(session).evaluate(
        PermissionRequest(
            user_id=user.id,
            capability=Capability.WRITE,
            resource="tool:create_task",
            tool_name="create_task",
        )
    )
    assert decision.mode is PermissionMode.ASK


# ── confirmations ────────────────────────────────────────────────────────────


def _request(user_id: str, **overrides) -> ConfirmationRequest:
    payload = {
        "user_id": user_id,
        "title": "Allow it?",
        "body": "Something risky",
        "tool_name": "risky_tool",
        "arguments": {"path": "/tmp/x"},
        "risk_level": RiskLevel.HIGH,
        "reversible": False,
    }
    payload.update(overrides)
    return ConfirmationRequest(**payload)  # type: ignore[arg-type]


async def test_confirmation_approve_then_find(session, user) -> None:
    service = ConfirmationService(session)
    created = await service.request(_request(user.id))
    assert created.status is ConfirmationStatus.PENDING

    await service.decide(created.id, approved=True)
    found = await service.find_approval(user.id, "risky_tool", {"path": "/tmp/x"})
    assert found is not None and found.id == created.id


async def test_approval_is_bound_to_exact_arguments(session, user) -> None:
    """An approval for one action must not authorise a different one."""
    service = ConfirmationService(session)
    created = await service.request(_request(user.id))
    await service.decide(created.id, approved=True)

    assert await service.find_approval(user.id, "risky_tool", {"path": "/etc/passwd"}) is None
    assert await service.find_approval(user.id, "other_tool", {"path": "/tmp/x"}) is None


async def test_approval_is_single_use(session, user) -> None:
    service = ConfirmationService(session)
    created = await service.request(_request(user.id))
    await service.decide(created.id, approved=True)

    first = await service.find_approval(user.id, "risky_tool", {"path": "/tmp/x"})
    assert first is not None
    await service.consume(first)

    assert await service.find_approval(user.id, "risky_tool", {"path": "/tmp/x"}) is None


async def test_stale_approval_stops_authorising(session, user) -> None:
    """An approval the user never spent must not become a standing grant.

    Approve, walk away, come back much later: the identical action has to ask
    again rather than executing silently.
    """
    service = ConfirmationService(session, ttl_seconds=900, approval_ttl_seconds=0)
    created = await service.request(_request(user.id))
    await service.decide(created.id, approved=True)

    assert await service.find_approval(user.id, "risky_tool", {"path": "/tmp/x"}) is None
    assert created.status is ConfirmationStatus.EXPIRED


async def test_fresh_approval_is_still_honoured(session, user) -> None:
    """The staleness check must not break the normal approve-then-resume flow."""
    service = ConfirmationService(session, ttl_seconds=900)
    created = await service.request(_request(user.id))
    await service.decide(created.id, approved=True)

    found = await service.find_approval(user.id, "risky_tool", {"path": "/tmp/x"})
    assert found is not None and found.id == created.id


async def test_denied_confirmation_yields_no_approval(session, user) -> None:
    service = ConfirmationService(session)
    created = await service.request(_request(user.id))
    await service.decide(created.id, approved=False, note="no thanks")

    assert created.status is ConfirmationStatus.DENIED
    assert await service.find_approval(user.id, "risky_tool", {"path": "/tmp/x"}) is None


async def test_confirmation_cannot_be_decided_twice(session, user) -> None:
    service = ConfirmationService(session)
    created = await service.request(_request(user.id))
    await service.decide(created.id, approved=True)
    with pytest.raises(ValidationError):
        await service.decide(created.id, approved=False)


async def test_expired_confirmation_cannot_be_approved(session, user) -> None:
    service = ConfirmationService(session, ttl_seconds=-1)
    created = await service.request(_request(user.id))
    with pytest.raises(ValidationError):
        await service.decide(created.id, approved=True)


async def test_duplicate_request_reuses_pending(session, user) -> None:
    service = ConfirmationService(session)
    first = await service.request(_request(user.id))
    second = await service.request(_request(user.id))
    assert first.id == second.id


def test_fingerprint_is_key_order_independent() -> None:
    assert action_fingerprint("t", {"a": 1, "b": 2}) == action_fingerprint(
        "t", {"b": 2, "a": 1}
    )


def test_fingerprint_differs_on_value_change() -> None:
    assert action_fingerprint("t", {"a": 1}) != action_fingerprint("t", {"a": 2})
