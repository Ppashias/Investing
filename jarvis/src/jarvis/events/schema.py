"""The console's event vocabulary (Phase D, front-end integration).

The command centre is a *view*. It renders what the backend says happened, and
every action it offers is a request the backend authorises exactly as it would
authorise the same action taken autonomously. This module is the contract
between those two halves: one closed set of event names, one envelope shape,
and a derivation from the activity records the system already writes.

## Why this is derived rather than emitted alongside

Every event below comes from an ``ActivityLog`` row that was already being
written. Nothing here is a second recording path.

That matters more than it looks. A parallel "UI events" channel would be a
second place the truth lives, and the two would diverge — quietly, and in the
direction that makes the console *look* healthier than the system is, because
the missing publish is always on the error path nobody exercised. Deriving from
audit rows means the console cannot show an action that was not audited, and
cannot miss one that was.

## Why server-sent events rather than a WebSocket

Asked for a WebSocket; keeping the existing SSE-over-fetch transport, and the
reason is authentication rather than preference.

**A browser cannot set headers on a WebSocket handshake.** The three ways to
authenticate one are: put the token in the query string, which sends it to
server logs, browser history and ``Referer`` — the precise finding the Phase 0
audit raised against the old dashboard; put it in a cookie, which means
adopting cookies and their CSRF surface for a system that currently has
neither; or open the socket unauthenticated and authenticate in the first
frame, which leaves a connected pre-auth socket and a state machine to get
wrong.

The existing stream is ``fetch`` + ``ReadableStream`` with the token in an
``Authorization`` header, hand-parsing SSE frames. That is already an
authenticated event stream, it is already one-way — which is all a *view*
needs — and the brief's own instruction not to weaken authentication for the
front end's convenience decides it. Actions travel over the authenticated REST
API, where they get the same treatment as everything else.
"""

from __future__ import annotations

from typing import Any

from jarvis.db.models import ActivityKind

#: The closed set. A console tab keys off these names, so an event that is not
#: here cannot be rendered — which is deliberate: a UI that guesses at unknown
#: event types is a UI that renders attacker-chosen strings.
AGENT_STARTED = "agent.started"
AGENT_UPDATED = "agent.updated"
AGENT_COMPLETED = "agent.completed"
AGENT_FAILED = "agent.failed"
AGENT_CANCELLED = "agent.cancelled"

TOOL_CALLED = "tool.called"
TOOL_COMPLETED = "tool.completed"
TOOL_DENIED = "tool.denied"

APPROVAL_REQUIRED = "approval.required"
APPROVAL_GRANTED = "approval.granted"
APPROVAL_REJECTED = "approval.rejected"

TASK_STARTED = "task.started"
TASK_PROGRESS = "task.progress"
TASK_PAUSED = "task.paused"
TASK_RESUMED = "task.resumed"
TASK_COMPLETED = "task.completed"
TASK_FAILED = "task.failed"

COMPUTER_ACTION_STARTED = "computer.action_started"
COMPUTER_ACTION_COMPLETED = "computer.action_completed"
COMPUTER_SCREENSHOT_UPDATED = "computer.screenshot_updated"

BROWSER_NAVIGATION = "browser.navigation"
BROWSER_ACTION = "browser.action"

MEMORY_PROPOSED = "memory.proposed"
MEMORY_APPROVED = "memory.approved"
MEMORY_REJECTED = "memory.rejected"

SECURITY_ALERT = "security.alert"
EMERGENCY_STOP = "emergency_stop"
SYSTEM_STATUS = "system.status"

#: Everything the console may be told about, for the test that pins the set.
EVENT_NAMES = frozenset({
    AGENT_STARTED, AGENT_UPDATED, AGENT_COMPLETED, AGENT_FAILED,
    AGENT_CANCELLED, TOOL_CALLED, TOOL_COMPLETED, TOOL_DENIED,
    APPROVAL_REQUIRED, APPROVAL_GRANTED, APPROVAL_REJECTED,
    TASK_STARTED, TASK_PROGRESS, TASK_PAUSED, TASK_RESUMED, TASK_COMPLETED,
    TASK_FAILED, COMPUTER_ACTION_STARTED, COMPUTER_ACTION_COMPLETED,
    COMPUTER_SCREENSHOT_UPDATED, BROWSER_NAVIGATION, BROWSER_ACTION,
    MEMORY_PROPOSED, MEMORY_APPROVED, MEMORY_REJECTED, SECURITY_ALERT,
    EMERGENCY_STOP, SYSTEM_STATUS,
})

#: Events the console should surface loudly rather than list. Each one means a
#: human is needed or something was refused — the two things a scrolling feed
#: is worst at conveying.
LOUD = frozenset({
    APPROVAL_REQUIRED, TOOL_DENIED, SECURITY_ALERT, EMERGENCY_STOP,
    AGENT_FAILED, TASK_FAILED,
})

#: Fields that may cross to the console, per event. Everything else on the
#: activity row is dropped.
#:
#: An allowlist rather than a denylist, because the detail dict is open-ended:
#: a tool that starts recording something new would otherwise begin streaming
#: it to every connected tab, and nobody would notice until it was a password.
_SAFE_DETAIL = frozenset({
    "agent_id", "role", "parent_id", "depth", "capabilities", "tools",
    "job_id", "title", "state", "progress", "step", "steps", "max_steps",
    "operation", "origin", "url", "page_id", "element_id", "element",
    "verdict", "reason", "decision", "rules", "confirmed", "tainted",
    "impact", "channel", "confirmation_id", "risk_level", "reversible",
    "status", "kind", "count", "chars", "task_id", "elapsed_seconds",
})


def _kind_of(kind: Any) -> str:
    return kind.value if isinstance(kind, ActivityKind) else str(kind or "")


def classify(event: Any) -> str | None:
    """Which console event, if any, an activity record represents.

    ``None`` means "nothing the console has a view for" — a large share of the
    stream, and correctly so. Returning a catch-all name instead would fill
    every panel with rows it cannot render.
    """
    kind = _kind_of(getattr(event, "kind", ""))
    status = (getattr(event, "status", "") or "").upper()
    tool = getattr(event, "tool_name", "") or ""
    detail = getattr(event, "detail", None) or {}

    if kind == "PERMISSION_DECISION":
        return TOOL_DENIED if status == "DENY" else None

    if kind == "CONFIRMATION_REQUESTED":
        return APPROVAL_REQUIRED
    if kind == "CONFIRMATION_RESOLVED":
        return APPROVAL_GRANTED if detail.get("approved") else APPROVAL_REJECTED

    if kind == "TOOL_CALL":
        if tool == "spawn_agent":
            return AGENT_STARTED
        if status in {"FAILED", "ERROR", "TIMED_OUT"}:
            return TOOL_COMPLETED
        return TOOL_CALLED if status in {"", "RUNNING"} else TOOL_COMPLETED

    if kind == "TASK_UPDATED":
        state = (detail.get("state") or status or "").upper()
        return {
            "RUNNING": TASK_STARTED,
            "PAUSED": TASK_PAUSED,
            "AWAITING_CONFIRMATION": APPROVAL_REQUIRED,
            "COMPLETED": TASK_COMPLETED,
            "CANCELLED": TASK_FAILED,
            "FAILED": TASK_FAILED,
        }.get(state, TASK_PROGRESS)
    if kind == "TASK_CREATED":
        return TASK_STARTED

    if kind == "BROWSER_ACTION":
        operation = detail.get("operation") or ""
        return BROWSER_NAVIGATION if operation == "navigate" else BROWSER_ACTION

    if kind == "COMPUTER_ACTION":
        return (
            COMPUTER_ACTION_COMPLETED
            if status in {"OK", "FAILED", "REFUSED", "DENIED"}
            else COMPUTER_ACTION_STARTED
        )

    if kind == "MEMORY_CAPTURED":
        # One kind, three outcomes. The status distinguishes them, because a
        # proposal and a stored fact are the same event to the recorder and
        # very different things to a human deciding whether to trust JARVIS.
        if status in {"PROPOSED", "AWAITING_CONFIRMATION"}:
            return MEMORY_PROPOSED
        if status in {"REFUSED", "REJECTED", "FORGOTTEN"}:
            return MEMORY_REJECTED
        return MEMORY_APPROVED

    if kind == "EMERGENCY_STOP":
        return EMERGENCY_STOP

    # Anything that was refused is a security event regardless of subsystem.
    # Collected here rather than per-kind above, so a new subsystem's refusals
    # reach the security panel without anybody remembering to add them.
    if status in {"DENIED", "REFUSED", "BLOCKED"}:
        return SECURITY_ALERT

    return None


def envelope(event: Any) -> dict[str, Any] | None:
    """One activity record as a console event, or ``None`` to drop it.

    The shape is fixed so the client never has to branch on which producer sent
    something: ``{event, at, actor, summary, status, tool, request_id, detail}``.

    ``summary`` is prose written by JARVIS, and ``detail`` values can include
    page-authored text — an element name, a page title. Neither is escaped
    here, because escaping at the producer is how you end up with
    double-escaped text in one consumer and raw text in another. The client
    inserts everything with ``textContent``; that is the boundary, and
    ``test_the_console_never_uses_innerhtml`` is what keeps it one.
    """
    name = classify(event)
    if name is None:
        return None

    detail = getattr(event, "detail", None) or {}
    return {
        "event": name,
        "loud": name in LOUD,
        "at": getattr(event, "created_at", None),
        "actor": getattr(event, "actor", None),
        "summary": getattr(event, "summary", "") or "",
        "status": getattr(event, "status", None),
        "tool": getattr(event, "tool_name", None),
        "request_id": getattr(event, "request_id", None),
        "detail": {k: v for k, v in detail.items() if k in _SAFE_DETAIL},
    }
