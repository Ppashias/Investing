"""Browser integration at the orchestrator's edges (Phase 4, Step 8).

Step 6 proved the model can reach the browser tools and that the control plane
holds for one tool per assistant turn. This file covers the surfaces that
sequence of tests never touched:

* several browser tool calls in a **single** assistant response, which is a
  different code path (``ExecuteStage._run_tools`` loops) and the one where
  intra-batch taint accumulation lives;
* a batch interrupted mid-way by a confirmation;
* the audit trail as seen through the **HTTP API** rather than through the
  service that wrote it;
* the iteration bound, with browser tools doing the looping.

It also contains one investigation rather than an assertion of correctness —
see ``test_a_later_turn_does_not_inherit_the_previous_turns_taint``. That test
documents behaviour that is arguably wrong. It is deliberately not fixed here.
"""

from __future__ import annotations

import threading
from dataclasses import replace
from http.server import ThreadingHTTPServer
from typing import Any

import pytest
from sqlalchemy import select

from jarvis.core import JarvisCore
from jarvis.db.models import (
    ActivityKind,
    ActivityLog,
    Confirmation,
    ConfirmationStatus,
    Conversation,
    ToolExecution,
)
from jarvis.providers.base import CompletionResult, ToolUseBlock, Usage

from .conftest import text_result, tool_result
from .test_browser_runtime import resolve_chromium
from .test_browser_tools import _Handler

# ── harness ──────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def batch_site() -> str:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
async def agent(core, batch_site: str):
    settings, reason = await resolve_chromium()
    if settings is None:
        pytest.skip(f"No usable Chromium on this machine: {reason}")
    core.browser.settings = replace(settings, max_pages=4, allow_localhost=True)
    try:
        yield core
    finally:
        await core.browser.shutdown()


def tool_batch(*calls: tuple[str, dict[str, Any], str]) -> CompletionResult:
    """One assistant response asking for several tools at once.

    ``conftest.tool_result`` emits a single ``ToolUseBlock``; a model is
    entitled to emit several, and ``ExecuteStage._run_tools`` loops over them.
    That loop is where taint accumulates *within* a turn, so it needs a way to
    be driven.
    """
    return CompletionResult(
        content=[ToolUseBlock(id=cid, name=name, input=args)
                 for name, args, cid in calls],
        stop_reason="tool_use",
        model="stub-model",
        provider="stub",
        usage=Usage(input_tokens=20, output_tokens=15, cost_micros=50),
        latency_ms=1.0,
    )


async def turn(core, message: str, *responses, conversation=None):
    core.providers.get("stub").responses = list(responses)
    async with core.database.session_factory() as session:
        user = await JarvisCore.ensure_default_user(session)
        await session.commit()
        convo = None
        if conversation is not None:
            convo = await session.get(Conversation, conversation)
        return await core.orchestrator.handle(
            session=session, user=user, message=message, conversation=convo
        )


def spy_on_taint(seen: list[tuple[str, bool]]):
    """Record ``(tool, ctx.tainted)`` for every call the loop makes."""
    from jarvis.tools import executor as ex

    real = ex.ToolExecutor.execute_safe

    async def spy(self, call, ctx):
        seen.append((call.name, ctx.tainted))
        return await real(self, call, ctx)

    ex.ToolExecutor.execute_safe = spy
    return real


def restore_taint_spy(real) -> None:
    from jarvis.tools import executor as ex

    ex.ToolExecutor.execute_safe = real


async def approve_pending(core) -> str:
    from jarvis.confirmations.service import ConfirmationService

    async with core.database.session_factory() as session:
        rows = (
            await session.execute(select(Confirmation).order_by(Confirmation.created_at))
        ).scalars().all()
        pending = [r for r in rows if r.status is ConfirmationStatus.PENDING]
        assert pending, "expected a pending confirmation"
        await ConfirmationService(session).decide(pending[-1].id, approved=True)
        await session.commit()
        return pending[-1].id


async def browser_rows(core):
    async with core.database.session_factory() as session:
        rows = (
            await session.execute(
                select(ActivityLog)
                .where(ActivityLog.kind == ActivityKind.BROWSER_ACTION)
                .order_by(ActivityLog.created_at)
            )
        ).scalars().all()
        return [(r.status, (r.detail or {}).get("operation"), r.detail or {})
                for r in rows]


def page_of(core) -> str:
    handles = core.browser.pages()
    assert handles, "expected an open page"
    return handles[-1].page_id


# ── 8A — cross-turn taint: an investigation, not a claim ─────────────────────


async def test_a_later_turn_does_not_inherit_the_previous_turns_taint(
    agent, batch_site
) -> None:
    """REAL BROWSER. **Documents a limitation. Does not assert it is correct.**

    Turn one reads a page whose text is a prompt-injection payload, so turn one
    is tainted and the engine escalates accordingly. Turn two continues the
    same conversation — the payload is still in the transcript the model is
    reasoning from, because turns with tool calls replay losslessly by design —
    and turn two starts **clean**.

    The cause is architectural rather than a bug in the browser code:
    ``ContextBundle.tainted`` is set in exactly two places, memory retrieval and
    knowledge retrieval. Conversation history is not a taint source, and
    ``PipelineContext.tool_taint`` is per-request by construction.

    Scope of the exposure, stated precisely rather than dramatically:
    ``browser_click`` and ``browser_fill`` are unaffected — they declare
    ``requires_confirmation``, so they ask on every turn whatever taint says.
    What is *not* escalated on turn two is every other non-read capability,
    which on this build means Obsidian writes and task creation.

    The same hole exists for a poisoned Obsidian note read in a previous turn,
    so a browser-specific patch would be the wrong shape as well as out of
    scope. Recorded here for the Step 11 adversarial audit, per the Step 8
    instruction not to change global taint semantics.
    """
    seen: list[tuple[str, bool]] = []
    real = spy_on_taint(seen)
    try:
        first = await turn(
            agent, "read that notice",
            tool_result("browser_open", {"url": batch_site + "/injection"},
                        call_id="a"),
            lambda _r: tool_result("browser_extract", {"page_id": page_of(agent)},
                                   call_id="b"),
            text_result("The page says something odd."),
        )
        assert first.status == "completed", first.text

        # Turn one really was tainted, or the rest proves nothing.
        assert ("browser_extract", False) in seen
        conversation_id = first.conversation_id

        second = await turn(
            agent, "now make a note of that",
            tool_result("create_task", {"title": "Something the page suggested"},
                        call_id="c"),
            text_result("Done."),
            conversation=conversation_id,
        )
        assert second.status in ("completed", "needs_confirmation")
    finally:
        restore_taint_spy(real)

    # The same conversation, so the payload is in the model's context.
    assert second.conversation_id == conversation_id

    taint_at_create = [t for name, t in seen if name == "create_task"]
    assert taint_at_create, "the second turn did not reach create_task"

    # THE FINDING. If this ever starts failing, the architecture changed and
    # the limitation recorded in the Step 8 documentation is stale.
    assert taint_at_create[0] is False, (
        "cross-turn taint now propagates — update docs/jarvis/08 and the Step 11 "
        "audit note, which both record that it does not"
    )


async def test_within_one_turn_the_taint_does_carry(agent, batch_site) -> None:
    """The control for the test above.

    Without this, "turn two is clean" could mean taint is broken everywhere
    rather than specifically not crossing a turn boundary.
    """
    seen: list[tuple[str, bool]] = []
    real = spy_on_taint(seen)
    try:
        await turn(
            agent, "read it then note it",
            tool_result("browser_open", {"url": batch_site + "/injection"},
                        call_id="a"),
            lambda _r: tool_result("browser_extract", {"page_id": page_of(agent)},
                                   call_id="b"),
            tool_result("create_task", {"title": "In the same turn"}, call_id="c"),
            text_result("done"),
        )
    finally:
        restore_taint_spy(real)

    by_name = dict(seen)
    assert by_name["browser_extract"] is False
    assert by_name["create_task"] is True, (
        "within a single turn the extract must taint what follows it"
    )


# ── 8B — several browser tools in one assistant response ─────────────────────


async def test_two_browser_tools_in_one_response_both_run(
    agent, batch_site
) -> None:
    """REAL BROWSER. A batch is a different path from two sequential turns."""
    opened = await turn(
        agent, "open it",
        tool_result("browser_open", {"url": batch_site + "/"}, call_id="a"),
        text_result("open"),
    )
    assert opened.status == "completed"
    page_id = page_of(agent)

    response = await turn(
        agent, "look at it two ways",
        tool_batch(
            ("browser_extract", {"page_id": page_id}, "b1"),
            ("browser_inspect", {"page_id": page_id}, "b2"),
        ),
        text_result("Read and inspected."),
    )

    assert response.status == "completed", response.text
    # This turn's calls only — the open belongs to the turn before it. Both
    # blocks of the one assistant response ran, in order.
    called = [c["tool"] for c in response.tool_calls]
    assert called == ["browser_extract", "browser_inspect"]
    assert not [c for c in response.tool_calls if c["is_error"]]

    async with agent.database.session_factory() as session:
        rows = (await session.execute(select(ToolExecution))).scalars().all()
    assert sorted(r.tool_name for r in rows) == [
        "browser_extract", "browser_inspect", "browser_open"
    ], "every tool in the batch must have its own execution row"


async def test_the_second_tool_in_a_batch_sees_the_first_ones_taint(
    agent, batch_site
) -> None:
    """REAL BROWSER. The rule the loop's comment asserts, finally proven.

    ``_run_tools`` recomputes taint on every iteration precisely so that a
    model asking for "read this page, then act on it" in one response does not
    get the action evaluated against a turn that was clean when it began.
    """
    await turn(agent, "open it",
               tool_result("browser_open", {"url": batch_site + "/injection"},
                           call_id="a"),
               text_result("open"))
    page_id = page_of(agent)

    seen: list[tuple[str, bool]] = []
    real = spy_on_taint(seen)
    try:
        await turn(
            agent, "read it and note it, together",
            tool_batch(
                ("browser_extract", {"page_id": page_id}, "b1"),
                ("create_task", {"title": "Suggested by the page"}, "b2"),
            ),
            text_result("done"),
        )
    finally:
        restore_taint_spy(real)

    batch = [(n, t) for n, t in seen if n in ("browser_extract", "create_task")]
    assert batch[0] == ("browser_extract", False), "taint must start off"
    assert batch[1] == ("create_task", True), (
        "the second tool in the batch inherited a clean context"
    )


async def test_taint_stays_on_for_the_rest_of_a_batch(agent, batch_site) -> None:
    """Monotonic *within* the batch, not merely "the previous tool was dirty".

    ``browser_status`` is JARVIS's own bookkeeping and returns clean. If the
    accumulator were an assignment rather than a latch, the tool after it would
    go back to being evaluated as trustworthy.
    """
    await turn(agent, "open it",
               tool_result("browser_open", {"url": batch_site + "/injection"},
                           call_id="a"),
               text_result("open"))
    page_id = page_of(agent)

    seen: list[tuple[str, bool]] = []
    real = spy_on_taint(seen)
    try:
        await turn(
            agent, "read, check, then note",
            tool_batch(
                ("browser_extract", {"page_id": page_id}, "c1"),
                ("browser_status", {}, "c2"),
                ("create_task", {"title": "After a clean tool"}, "c3"),
            ),
            text_result("done"),
        )
    finally:
        restore_taint_spy(real)

    by_name = dict(seen)
    assert by_name["browser_status"] is True
    assert by_name["create_task"] is True, (
        "a clean tool in the middle of a batch washed the turn clean"
    )


# ── 8B — a batch interrupted by a confirmation ───────────────────────────────


async def test_a_middle_tool_suspending_stops_the_batch_and_keeps_what_ran(
    agent, batch_site
) -> None:
    """REAL BROWSER. Three tools, the middle one needs approval.

    Four things have to be true at once, and each is a way this could go wrong:
    the first tool's real effect survives, its audit row survives, the
    suspension is persisted so the user can answer it, and the third tool does
    **not** run — a batch that carried on past an unanswered question would
    make the question decorative.
    """
    await turn(agent, "open the form",
               tool_result("browser_open", {"url": batch_site + "/form"},
                           call_id="a"),
               text_result("open"))
    page_id = page_of(agent)

    inspected = await turn(agent, "inspect",
                           tool_result("browser_inspect", {"page_id": page_id},
                                       call_id="b"),
                           text_result("ok"))
    button = next(e for e in inspected.tool_calls[0]["data"]["elements"]
                  if e["role"] == "button")

    before = len(await browser_rows(agent))

    seen: list[tuple[str, bool]] = []
    real = spy_on_taint(seen)
    try:
        response = await turn(
            agent, "read it, click it, then read it again",
            tool_batch(
                ("browser_extract", {"page_id": page_id}, "d1"),
                ("browser_click",
                 {"page_id": page_id, "element_id": button["element_id"]}, "d2"),
                ("browser_pages", {}, "d3"),
            ),
            text_result("unreachable"),
        )
    finally:
        restore_taint_spy(real)

    # The turn stopped to ask.
    assert response.status == "needs_confirmation"
    assert response.pending_confirmation is not None

    attempted = [n for n, _ in seen]
    assert "browser_extract" in attempted, "the first tool ran"
    assert "browser_click" in attempted, "the second tool is what suspended"
    assert "browser_pages" not in attempted, (
        "the third tool ran past an unanswered confirmation"
    )

    # The first tool's audit row survived the suspension commit.
    after = await browser_rows(agent)
    assert len(after) > before
    assert ("OK", "extract") in [(s, op) for s, op, _ in after]

    # And the confirmation is really persisted, not just raised.
    async with agent.database.session_factory() as session:
        pending = [
            c for c in (await session.execute(select(Confirmation))).scalars().all()
            if c.status is ConfirmationStatus.PENDING
        ]
    assert len(pending) == 1
    assert pending[0].action["tool"] == "browser_click"


async def test_approving_the_suspended_tool_lets_it_proceed(
    agent, batch_site
) -> None:
    """Resumption after a mid-batch suspension, by re-request.

    The approval is bound to the click's exact arguments, so the model asking
    again with the same element is the thing it authorises — and nothing else.
    """
    await turn(agent, "open the form",
               tool_result("browser_open", {"url": batch_site + "/form"},
                           call_id="a"),
               text_result("open"))
    page_id = page_of(agent)
    inspected = await turn(agent, "inspect",
                           tool_result("browser_inspect", {"page_id": page_id},
                                       call_id="b"),
                           text_result("ok"))
    button = next(e for e in inspected.tool_calls[0]["data"]["elements"]
                  if e["role"] == "button")
    click_args = {"page_id": page_id, "element_id": button["element_id"]}

    suspended = await turn(
        agent, "read then click",
        tool_batch(
            ("browser_extract", {"page_id": page_id}, "e1"),
            ("browser_click", click_args, "e2"),
        ),
        text_result("unreachable"),
    )
    assert suspended.status == "needs_confirmation"

    await approve_pending(agent)

    resumed = await turn(
        agent, "yes, go ahead",
        tool_result("browser_click", click_args, call_id="e3"),
        text_result("Clicked it."),
    )
    assert resumed.status == "completed", resumed.text
    assert resumed.tool_calls[0]["is_error"] is False
    assert ("OK", "click") in [(s, op) for s, op, _ in await browser_rows(agent)]


# ── 8D — the iteration bound ─────────────────────────────────────────────────


async def test_browser_tools_cannot_loop_the_orchestrator_forever(
    agent, batch_site
) -> None:
    """REAL BROWSER. A model that just keeps browsing still has to stop.

    ``browser_status`` is the right tool for this: it needs no page, never
    fails, and never asks — so nothing except the iteration bound itself can
    end the loop.
    """
    stub = agent.providers.get("stub")
    responses = [tool_result("browser_status", {}, call_id=f"i{i}")
                 for i in range(40)]

    response = await turn(agent, "keep checking the browser", *responses)

    assert "max_iterations_reached" in response.warnings
    assert stub.call_count <= agent.settings.max_agent_iterations
    assert response.status == "completed", (
        "hitting the bound is reported, not raised as a failure"
    )


async def test_a_bounded_run_leaves_no_pages_behind(agent, batch_site) -> None:
    """Pages opened during a run that hit the bound are still JARVIS's to close.

    The cap is what makes this matter: a loop that opened a page per iteration
    would hit ``max_pages`` and start failing, so the two bounds have to agree.
    """
    stub = agent.providers.get("stub")
    responses = [tool_result("browser_open", {"url": batch_site + "/"},
                             call_id=f"o{i}") for i in range(40)]

    response = await turn(agent, "keep opening pages", *responses)
    assert "max_iterations_reached" in response.warnings

    # Whatever it managed to open is within the cap and accounted for.
    assert agent.browser.page_count <= agent.browser.settings.max_pages

    report = await agent.browser.shutdown()
    assert report.clean is True, report.describe()
    assert agent.browser.page_count == 0
    assert agent.browser.running is False


# ── 8C — the audit trail as the API serves it ────────────────────────────────


@pytest.fixture
async def api(core, client, batch_site: str):
    """A real HTTP client over the same core the browser tests drive.

    Both, deliberately. The audit is written by the service and read by the
    API, and a value that is redacted on the way in but reconstructed on the
    way out would be invisible to any test that only looked at one end.
    """
    settings, reason = await resolve_chromium()
    if settings is None:
        pytest.skip(f"No usable Chromium on this machine: {reason}")
    core.browser.settings = replace(settings, max_pages=4, allow_localhost=True)
    try:
        yield client
    finally:
        await core.browser.shutdown()


async def test_browser_actions_are_visible_through_the_activity_api(
    core, api, batch_site
) -> None:
    """REAL BROWSER, real HTTP. The trail has to be reachable to be a trail.

    Written by ``ActivityService`` and read back over the wire, because an
    audit nobody can retrieve is a log file with extra steps.
    """
    await turn(core, "open and read it",
               tool_result("browser_open", {"url": batch_site + "/"}, call_id="a"),
               lambda _r: tool_result("browser_extract", {"page_id": page_of(core)},
                                      call_id="b"),
               text_result("done"))

    response = api.get("/api/activity", params={"limit": 200})
    assert response.status_code == 200
    rows = [r for r in response.json()["activity"]
            if r.get("kind") == "BROWSER_ACTION"]
    assert rows, "no browser activity reached the API"

    operations = {(r.get("detail") or {}).get("operation") for r in rows}
    assert {"navigate", "extract"} <= operations

    extract = next(r for r in rows
                   if (r.get("detail") or {}).get("operation") == "extract")
    detail = extract["detail"]
    # Everything Step 7 promised is reconstructable from the API alone.
    assert detail["origin"].startswith("http://127.0.0.1:")
    assert detail["tainted"] is False
    assert detail["decision"] == "ALLOW"
    assert "rules" in detail
    assert extract["status"] == "OK"


async def test_a_filled_value_never_crosses_the_api_boundary(
    core, api, batch_site
) -> None:
    """REAL BROWSER, real form, real HTTP.

    Redaction happens at the executor. This checks the other end: that nothing
    downstream — activity detail, execution arguments, confirmation payloads —
    puts the value back on the wire.
    """
    typed = "api-boundary-secret-55213"

    await turn(core, "open the form",
               tool_result("browser_open", {"url": batch_site + "/form"},
                           call_id="a"),
               text_result("open"))
    page_id = page_of(core)
    inspected = await turn(core, "inspect",
                           tool_result("browser_inspect", {"page_id": page_id},
                                       call_id="b"),
                           text_result("ok"))
    box = next(e for e in inspected.tool_calls[0]["data"]["elements"]
               if e["role"] in ("textbox", "searchbox"))
    args = {"page_id": page_id, "element_id": box["element_id"], "text": typed}

    asked = await turn(core, "type it",
                       tool_result("browser_fill", args, call_id="c"),
                       text_result("unreachable"))
    assert asked.status == "needs_confirmation"
    await approve_pending(core)
    done = await turn(core, "yes",
                      tool_result("browser_fill", args, call_id="d"),
                      text_result("typed"))
    assert done.tool_calls[0]["is_error"] is False

    # Every endpoint that could carry it.
    surfaces = [
        api.get("/api/activity", params={"limit": 500}),
        api.get("/api/confirmations"),
        api.get("/api/system/status"),
    ]
    for response in surfaces:
        assert response.status_code == 200, response.text
        assert typed not in response.text, (
            f"the typed value came back from {response.request.url}"
        )


async def test_a_refused_credential_value_never_crosses_the_api_boundary(
    core, api, batch_site
) -> None:
    """The refusal path, which is the one that matters most.

    A password the model tried to type is the single worst thing that could
    survive into an HTTP response, and the fill is refused *after* the
    confirmation exists — so the record is created before anything knows the
    field was forbidden.
    """
    secret = "refused-credential-77431"

    await turn(core, "open login",
               tool_result("browser_open", {"url": batch_site + "/login"},
                           call_id="a"),
               text_result("open"))
    page_id = page_of(core)
    inspected = await turn(core, "inspect",
                           tool_result("browser_inspect", {"page_id": page_id},
                                       call_id="b"),
                           text_result("ok"))
    password = next(e for e in inspected.tool_calls[0]["data"]["elements"]
                    if e.get("name") == "password")
    args = {"page_id": page_id, "element_id": password["element_id"],
            "text": secret}

    await turn(core, "sign in", tool_result("browser_fill", args, call_id="c"),
               text_result("x"))
    await approve_pending(core)
    refused = await turn(core, "yes",
                         tool_result("browser_fill", args, call_id="d"),
                         text_result("refused"))
    assert refused.tool_calls[0]["is_error"] is True
    assert refused.tool_calls[0]["data"]["credential_field"] is True

    for path in ("/api/activity", "/api/confirmations"):
        response = api.get(path, params={"limit": 500})
        assert response.status_code == 200
        assert secret not in response.text, f"{path} returned the credential"

    # The refusal itself is still visible — redaction must not cost the record.
    rows = api.get("/api/activity", params={"limit": 500}).json()["activity"]
    fills = [r for r in rows
             if (r.get("detail") or {}).get("operation") == "fill"]
    assert any(r["status"] == "REFUSED" for r in fills)
