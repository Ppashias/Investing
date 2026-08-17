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


# ── impact, and the channel a decision arrived through (Phase D, item 4) ─────
#
# Capability answers "which domain". Risk answers "how bad if it goes wrong".
# Neither answers the question a person actually asks when a dialog appears,
# which is "can I take this back?" — and that question is the one the voice
# rule depends on.


@pytest.mark.parametrize(
    "capability,reversible,risk,expected",
    [
        (Capability.READ, True, RiskLevel.NONE, "read"),
        (Capability.READ, True, RiskLevel.LOW, "read"),
        (Capability.WRITE, True, RiskLevel.LOW, "write"),
        (Capability.EXTERNAL_ACTION, True, RiskLevel.MEDIUM, "external"),
        (Capability.EXECUTE, True, RiskLevel.LOW, "external"),
        # Running a shell command is not reversible in any sense we can verify.
        (Capability.EXECUTE, True, RiskLevel.HIGH, "destructive"),
        (Capability.SENSITIVE_ACTION, True, RiskLevel.NONE, "destructive"),
        # Irreversible beats everything, including a read.
        (Capability.READ, False, RiskLevel.NONE, "destructive"),
        # So does CRITICAL: a tool rated critical rendered as "changes
        # something" is a dialog that misinforms.
        (Capability.WRITE, True, RiskLevel.CRITICAL, "destructive"),
    ],
)
def test_impact_is_derived_pessimistically(capability, reversible, risk, expected):
    from jarvis.permissions.impact import impact_of

    assert impact_of(capability, reversible=reversible,
                     risk_level=risk).value == expected


def test_impact_is_derived_rather_than_declared() -> None:
    """A fourth declared field is a field somebody forgets to set, and the
    failure mode of forgetting is a destructive action rendered as routine."""
    from jarvis.tools.base import Tool

    assert "impact" not in {f for f in Tool.__slots__}


async def test_a_confirmation_records_how_far_the_action_reaches(
    session, user
) -> None:
    from jarvis.confirmations.service import ConfirmationRequest, ConfirmationService

    service = ConfirmationService(session)
    confirmation = await service.request(
        ConfirmationRequest(
            user_id=user.id, title="Allow browser_click?", body="Click something.",
            tool_name="browser_click", arguments={"page_id": "pg_1"},
            risk_level=RiskLevel.MEDIUM, reversible=False, impact="destructive",
        )
    )
    await session.flush()
    assert confirmation.impact == "destructive"
    assert ConfirmationService.to_dict(confirmation)["impact"] == "destructive"


async def test_a_destructive_action_cannot_be_approved_by_voice(
    session, user
) -> None:
    """`vierisid/jarvis`'s rule, and its reasoning: a single misheard syllable
    could trigger a payment.

    Speech recognition mishears, a podcast says "yes", somebody else in the
    room answers. For something that cannot be undone, the deliberate act is
    the only authoritative path.
    """
    from jarvis.confirmations.service import ConfirmationRequest, ConfirmationService
    from jarvis.errors import ValidationError

    service = ConfirmationService(session)
    confirmation = await service.request(
        ConfirmationRequest(
            user_id=user.id, title="Allow run_command?", body="rm -rf",
            tool_name="run_command", arguments={}, reversible=False,
            impact="destructive",
        )
    )
    await session.flush()

    with pytest.raises(ValidationError) as caught:
        await service.decide(confirmation.id, approved=True, channel="voice")
    assert "cannot be approved by voice" in str(caught.value)

    # …and the deliberate path still works.
    decided = await service.decide(confirmation.id, approved=True, channel="ui")
    assert decided.status is ConfirmationStatus.APPROVED
    assert decided.resolution_channel == "ui"


async def test_a_destructive_action_can_still_be_refused_by_voice(
    session, user
) -> None:
    """Refusing to act on a mishearing would mean the cautious answer is the
    one the system ignores."""
    from jarvis.confirmations.service import ConfirmationRequest, ConfirmationService

    service = ConfirmationService(session)
    confirmation = await service.request(
        ConfirmationRequest(
            user_id=user.id, title="Allow run_command?", body="rm -rf",
            tool_name="run_command", arguments={}, reversible=False,
            impact="destructive",
        )
    )
    await session.flush()

    decided = await service.decide(confirmation.id, approved=False, channel="voice")
    assert decided.status is ConfirmationStatus.DENIED
    assert decided.resolution_channel == "voice"


async def test_a_non_destructive_action_may_be_approved_by_voice(
    session, user
) -> None:
    from jarvis.confirmations.service import ConfirmationRequest, ConfirmationService

    service = ConfirmationService(session)
    confirmation = await service.request(
        ConfirmationRequest(
            user_id=user.id, title="Allow create_task?", body="Make a task.",
            tool_name="create_task", arguments={}, impact="write",
        )
    )
    await session.flush()

    decided = await service.decide(confirmation.id, approved=True, channel="voice")
    assert decided.status is ConfirmationStatus.APPROVED
    assert decided.resolution_channel == "voice"


def test_the_executor_appends_the_impact_sentence() -> None:
    """Appended centrally, not written into each tool's template.

    A tool that forgot the sentence would render a destructive action as a
    routine one, and the classification comes from fields the tool already
    declares — so there is nothing for it to forget.
    """
    import inspect

    from jarvis.tools.base import Tool, ToolResult
    from jarvis.tools.executor import ToolExecutor

    assert "_with_impact" in inspect.getsource(ToolExecutor._authorise)

    async def _noop(*, ctx):  # pragma: no cover - never invoked
        return ToolResult.ok("")

    body = ToolExecutor._with_impact(
        Tool("t", "d", {}, _noop, reversible=False), "Do the thing."
    )
    assert "cannot be undone" in body


# ── context rules: when a grant applies at all (Phase D, item 5) ─────────────
#
# `vierisid/jarvis` models these as a separate "context rules" list evaluated
# alongside its authority levels. Ours live on the grant, because a separate
# list is a second policy surface and the whole argument of this engine is that
# there is one place to look up what a user is allowed to do. A grant that does
# not apply right now is simply not a candidate, so most-specific-wins and
# DENY-breaks-ties keep working untouched.


def _at(hour: int, minute: int = 0):
    """Freeze the engine's idea of local wall-clock time."""
    from datetime import datetime as _dt
    from unittest.mock import patch

    import jarvis.permissions.engine as engine

    class _Frozen(_dt):
        @classmethod
        def now(cls, tz=None):
            if tz is not None:
                return _dt.now(tz)
            return _dt(2026, 8, 17, hour, minute)

    return patch.object(engine, "datetime", _Frozen)


async def _ask(session, user, capability=Capability.EXTERNAL_ACTION,
               tool="browser_open"):
    return await PermissionEngine(session).evaluate(
        PermissionRequest(
            user_id=user.id, capability=capability, resource=f"tool:{tool}",
            tool_name=tool,
        )
    )


async def test_a_grant_outside_its_window_does_not_apply(session, user) -> None:
    """"Let JARVIS browse during working hours" — and only then."""
    session.add(
        PermissionGrant(
            user_id=user.id, capability=Capability.EXTERNAL_ACTION,
            resource_scope="tool:browser_open", mode=PermissionMode.ALLOW,
            conditions={"active_between": {"from": "09:00", "to": "18:00"}},
            note="working hours only",
        )
    )
    await session.flush()

    with _at(11):
        assert (await _ask(session, user)).mode is PermissionMode.ALLOW
    with _at(20):
        # Falls through to the default, which asks. Fails safe rather than open.
        assert (await _ask(session, user)).mode is PermissionMode.ASK


async def test_a_window_can_wrap_past_midnight(session, user) -> None:
    """22:00 → 06:00 is two intervals, not none.

    The wrapping case is the one people actually want — "nothing external
    overnight" — so getting it wrong would make the feature useless for its
    main use.
    """
    session.add(
        PermissionGrant(
            user_id=user.id, capability=Capability.EXTERNAL_ACTION,
            resource_scope="*", mode=PermissionMode.DENY,
            conditions={"active_between": {"from": "22:00", "to": "06:00"}},
            note="no external actions overnight",
        )
    )
    await session.flush()

    for hour in (23, 2, 5):
        with _at(hour):
            assert (await _ask(session, user)).mode is PermissionMode.DENY, hour
    for hour in (7, 12, 21):
        with _at(hour):
            assert (await _ask(session, user)).mode is not PermissionMode.DENY, hour


async def test_the_window_boundary_belongs_to_one_side_only(session, user) -> None:
    """Inclusive of ``from``, exclusive of ``to``, so two adjacent windows do
    not both claim the boundary minute."""
    session.add(
        PermissionGrant(
            user_id=user.id, capability=Capability.EXTERNAL_ACTION,
            resource_scope="*", mode=PermissionMode.ALLOW,
            conditions={"active_between": {"from": "09:00", "to": "18:00"}},
        )
    )
    await session.flush()

    with _at(9, 0):
        assert (await _ask(session, user)).mode is PermissionMode.ALLOW
    with _at(18, 0):
        assert (await _ask(session, user)).mode is PermissionMode.ASK


async def test_a_malformed_window_does_not_grant_anything(session, user) -> None:
    """A typo must not silently grant permission around the clock."""
    session.add(
        PermissionGrant(
            user_id=user.id, capability=Capability.EXTERNAL_ACTION,
            resource_scope="*", mode=PermissionMode.ALLOW,
            conditions={"active_between": {"from": "9am", "to": "6pm"}},
        )
    )
    await session.flush()

    with _at(11):
        assert (await _ask(session, user)).mode is PermissionMode.ASK


async def test_a_grant_can_be_restricted_to_named_tools(session, user) -> None:
    session.add(
        PermissionGrant(
            user_id=user.id, capability=Capability.EXTERNAL_ACTION,
            resource_scope="*", mode=PermissionMode.ALLOW,
            conditions={"tools": ["browser_open"]},
        )
    )
    await session.flush()

    assert (await _ask(session, user, tool="browser_open")).mode is PermissionMode.ALLOW
    assert (await _ask(session, user, tool="browser_click")).mode is PermissionMode.ASK


async def test_a_rule_can_apply_only_to_a_poisoned_turn(session, user) -> None:
    """"Nothing that touched a web page may do this", said once.

    Expressible as a single DENY rather than per tool, which is the difference
    between a policy someone maintains and one they give up on.
    """
    # Scoped to the same specificity as the seeded ALLOW for this tool, so
    # this also shows DENY-breaks-ties still working once the condition holds.
    session.add(
        PermissionGrant(
            user_id=user.id, capability=Capability.WRITE,
            resource_scope="tool:create_task", mode=PermissionMode.DENY,
            conditions={"when_tainted": True},
        )
    )
    await session.flush()

    engine = PermissionEngine(session)
    clean = await engine.evaluate(
        PermissionRequest(user_id=user.id, capability=Capability.WRITE,
                          resource="tool:create_task", tool_name="create_task")
    )
    assert clean.mode is not PermissionMode.DENY

    poisoned = await engine.evaluate(
        PermissionRequest(user_id=user.id, capability=Capability.WRITE,
                          resource="tool:create_task", tool_name="create_task",
                          tainted=True)
    )
    assert poisoned.mode is PermissionMode.DENY


async def test_conditions_do_not_disturb_specificity_or_deny_ties(
    session, user
) -> None:
    """The existing rules keep working, because a non-applying grant is simply
    not a candidate rather than a special case inside the sort."""
    session.add_all([
        PermissionGrant(
            user_id=user.id, capability=Capability.EXTERNAL_ACTION,
            resource_scope="*", mode=PermissionMode.ALLOW,
        ),
        PermissionGrant(
            user_id=user.id, capability=Capability.EXTERNAL_ACTION,
            resource_scope="tool:browser_open", mode=PermissionMode.DENY,
            conditions={"active_between": {"from": "22:00", "to": "06:00"}},
        ),
    ])
    await session.flush()

    with _at(23):
        # The specific, currently-applying DENY wins.
        assert (await _ask(session, user)).mode is PermissionMode.DENY
    with _at(12):
        # Outside its window it is not a candidate, so the broad ALLOW stands.
        assert (await _ask(session, user)).mode is PermissionMode.ALLOW


async def test_an_unknown_condition_key_is_ignored(session, user) -> None:
    """Operator data outliving a rename must not silently revoke permissions.

    The asymmetry is deliberate and worth knowing: ignoring an unknown
    *restriction* widens rather than narrows, so anything added to this
    vocabulary has to be additive, never a rename.
    """
    session.add(
        PermissionGrant(
            user_id=user.id, capability=Capability.EXTERNAL_ACTION,
            resource_scope="*", mode=PermissionMode.ALLOW,
            conditions={"only_on_tuesdays": True},
        )
    )
    await session.flush()

    with _at(11):
        assert (await _ask(session, user)).mode is PermissionMode.ALLOW
