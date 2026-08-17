"""The console stream, driven by real turns rather than constructed events.

Every other console test asserts a schema or an endpoint in isolation: given
this ``ActivityEvent``, does ``classify`` name it correctly; given this URL,
is it authenticated. Both are worth having and neither answers the question a
user actually has, which is whether the thing that just happened shows up on
the screen.

So these tests take the long way round. A turn goes through the real
orchestrator, the real permission engine, the real tool executor and the real
persistence layer — only the model call is a stub — and what is asserted is
what a subscriber to the activity bus would have received. That is the same
object ``/activity/console`` reshapes and writes to the socket, so the only
untested link left is the socket itself.

That last link is deliberate, and was tried before it was ruled out: opening
the stream through TestClient and reading one frame hangs, because the
response never ends and the test client has no way to know a frame is all the
test wanted. What that would have exercised is Starlette's streaming and this
process's event loop rather than anything JARVIS does, so the final test here
drives the endpoint's generator directly instead — which covers the ready
frame, the subscription and the headers, and stops exactly where the socket
begins.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from jarvis.core import JarvisCore
from jarvis.db.models import Capability, RiskLevel
from jarvis.events.schema import LOUD, envelope
from jarvis.tools.base import ToolContext, ToolResult, tool
from tests.conftest import StubProvider, text_result, tool_result


async def _turn(core: JarvisCore, message: str) -> Any:
    async with core.database.session_factory() as session:
        user = await JarvisCore.ensure_default_user(session)
        await session.commit()
        return await core.orchestrator.handle(
            session=session, user=user, message=message
        )


def _drain(queue: asyncio.Queue) -> list[Any]:
    """Everything published so far, in order.

    Publication is synchronous — ``ActivityBus.publish`` is a ``put_nowait``
    into every subscriber's queue — so by the time the turn has returned,
    whatever it produced is already sitting here.
    """
    events = []
    while True:
        try:
            events.append(queue.get_nowait())
        except asyncio.QueueEmpty:
            return events


def _console(queue: asyncio.Queue) -> list[dict[str, Any]]:
    """What the endpoint would have written, minus the socket.

    Built by the same call ``stream_console`` makes, including the drop of
    anything the console has no view for — so a test cannot pass on an event
    that would never have been sent.
    """
    return [payload for payload in
            (envelope(event) for event in _drain(queue)) if payload is not None]


# ── the ordinary path ────────────────────────────────────────────────────────


async def test_a_real_turn_reaches_the_console_stream(
    core: JarvisCore, stub: StubProvider
) -> None:
    """The one that would have caught a broken wire at any point in the chain.

    Orchestrator to activity service to bus to envelope. Asserted on names
    from the closed vocabulary rather than on counts, because "some events
    arrived" is true of a stream that reports the wrong things.
    """
    queue = await core.activity_bus.subscribe()
    try:
        stub.responses = [
            tool_result("create_task", {"title": "Water the plants"}),
            text_result("Added."),
        ]
        response = await _turn(core, "Add a task to water the plants")
        assert response.status == "completed"

        names = [payload["event"] for payload in _console(queue)]
    finally:
        await core.activity_bus.unsubscribe(queue)

    assert "tool.called" in names, names
    assert "tool.completed" in names, names
    # Ordering is part of the contract: a completion drawn before its call
    # would make the feed read backwards.
    assert names.index("tool.called") < names.index("tool.completed")


async def test_a_slow_tool_is_announced_before_it_finishes(
    core: JarvisCore, stub: StubProvider
) -> None:
    """The gap the test above found, pinned so it cannot come back.

    The executor used to record TOOL_CALL only once the handler had returned,
    always with a terminal status — so `tool.called` sat in the console
    vocabulary and was never once emitted, and a tool that takes half a minute
    produced an empty feed for half a minute. That is exactly the window in
    which somebody wants to know what JARVIS is doing.

    Asserted from inside the handler rather than after the turn, because
    "both events exist at the end" would pass on the old behaviour too if the
    executor merely recorded twice.
    """
    queue = await core.activity_bus.subscribe()
    seen_while_running: list[str] = []
    ran: list[bool] = []

    @tool(
        name="take_a_while",
        description="Does something slowly",
        capability=Capability.READ,
        risk_level=RiskLevel.LOW,
        parameters={"type": "object", "properties": {}},
    )
    async def slow(*, ctx: ToolContext) -> ToolResult:
        ran.append(True)
        seen_while_running.extend(p["event"] for p in _console(queue))
        return ToolResult(ok=True, data={})

    core.tools.register(slow)
    try:
        stub.responses = [tool_result("take_a_while", {}), text_result("Done.")]
        await _turn(core, "Please run the slow tool and tell me when it is done")
    finally:
        await core.activity_bus.unsubscribe(queue)
        core.tools.unregister("take_a_while")

    assert ran, "the handler never ran"
    assert "tool.called" in seen_while_running, seen_while_running
    assert "tool.completed" not in seen_while_running, (
        "the console was told the tool finished before it had"
    )


async def test_the_stream_carries_no_content_from_the_turn(
    core: JarvisCore, stub: StubProvider
) -> None:
    """The allowlist, tested against a real turn instead of a fixture.

    ``test_the_envelope_allowlists_detail_fields`` proves the filter drops what
    it is given. This proves the turn does not hand it something new — a stage
    that started attaching the message, the arguments or the tool's output to
    an activity record would be a content leak into a panel that gets
    screenshotted, and the schema test would still pass.
    """
    queue = await core.activity_bus.subscribe()
    try:
        stub.responses = [
            tool_result("create_task", {"title": "Renew passport 12 Foo Street"}),
            text_result("Added."),
        ]
        await _turn(core, "my landline is 555 0100, add a task to renew my passport")
        payloads = json.dumps(_console(queue))
    finally:
        await core.activity_bus.unsubscribe(queue)

    for secret in ("555 0100", "12 Foo Street", "landline"):
        assert secret not in payloads, f"{secret!r} reached the console stream"


# ── the loud path ────────────────────────────────────────────────────────────


async def test_an_action_needing_approval_arrives_marked_loud(
    core: JarvisCore, stub: StubProvider
) -> None:
    """The event the whole console exists to deliver.

    A confirmation that failed to reach the stream would leave the user
    looking at a screen that says nothing is waiting on them while JARVIS
    waits for an answer — which is worse than no console, because it is an
    assurance that happens to be false.
    """

    @tool(
        name="shred_the_archive",
        description="Deletes the archive",
        capability=Capability.EXECUTE,
        risk_level=RiskLevel.HIGH,
        reversible=False,
        requires_confirmation=True,
        parameters={"type": "object",
                    "properties": {"target": {"type": "string"}},
                    "required": ["target"]},
    )
    async def shred(*, target: str, ctx: ToolContext) -> ToolResult:
        raise AssertionError("the confirmation should have stopped this")

    core.tools.register(shred)
    queue = await core.activity_bus.subscribe()
    try:
        stub.responses = [tool_result("shred_the_archive", {"target": "everything"})]
        response = await _turn(core, "Shred the archive")
        assert response.status == "needs_confirmation"

        payloads = _console(queue)
    finally:
        await core.activity_bus.unsubscribe(queue)
        core.tools.unregister("shred_the_archive")

    approvals = [p for p in payloads if p["event"] == "approval.required"]
    assert approvals, [p["event"] for p in payloads]
    # Loud is what makes the reactor and the banner react rather than the row
    # scrolling past with everything else.
    assert approvals[0]["loud"] is True
    assert "approval.required" in LOUD

    # The console needs the id to key its panel; without it a decision could
    # not be tied back to the thing being decided.
    assert approvals[0]["detail"].get("confirmation_id")
    # …and not the arguments. What is being shredded belongs in the dialog.
    assert "everything" not in json.dumps(approvals[0])


async def test_a_refusal_reaches_the_stream_as_a_security_event(
    core: JarvisCore, stub: StubProvider
) -> None:
    """A denial is not a failure, and the console draws them differently.

    Driven through the engine rather than by recording a DENY by hand: the
    question is whether a real refusal produces a real event, and a
    hand-written record answers a different one.
    """

    @tool(
        name="wire_the_money",
        description="Moves money",
        capability=Capability.SENSITIVE_ACTION,
        risk_level=RiskLevel.CRITICAL,
        reversible=False,
        parameters={"type": "object", "properties": {}},
    )
    async def wire(*, ctx: ToolContext) -> ToolResult:
        raise AssertionError("the engine should never have let this run")

    core.tools.register(wire)
    queue = await core.activity_bus.subscribe()
    try:
        stub.responses = [
            tool_result("wire_the_money", {}),
            text_result("I cannot do that."),
        ]
        await _turn(core, "Wire the money")
        payloads = _console(queue)
    finally:
        await core.activity_bus.unsubscribe(queue)
        core.tools.unregister("wire_the_money")

    names = [p["event"] for p in payloads]
    assert "tool.denied" in names or "security.alert" in names, names


# ── the transport ────────────────────────────────────────────────────────────


async def test_the_stream_opens_with_a_ready_frame(core: JarvisCore) -> None:
    """The endpoint's own generator, one frame in.

    The console keys its "connected" state on this frame, so a stream that
    subscribed but announced nothing would leave the header reading CONNECTING
    forever while events arrived behind it.
    """
    from jarvis.api.routes import stream_console

    response = await stream_console(core, None)
    assert response.media_type == "text/event-stream"
    # No buffering in front of it, or the console shows nothing until whatever
    # sits in the middle decides it has enough.
    assert response.headers.get("x-accel-buffering") == "no"

    frames = response.body_iterator
    try:
        first = await frames.__anext__()
    finally:
        await frames.aclose()
    assert first.startswith("event: ready")

    # The generator unsubscribes on the way out; a stream that leaked its queue
    # would leave the bus fanning out to a tab that closed hours ago.
    assert core.activity_bus.subscriber_count == 0
