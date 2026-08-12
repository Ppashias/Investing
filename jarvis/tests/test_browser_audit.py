"""Browser audit integrity and operational lifecycle (Phase 4, Step 7).

Steps 5 and 6 proved the browser tools behave and that the model reaches them.
This file asks a narrower question: **if something went wrong, could you tell?**

The Step 7 reconnaissance found three gaps, and most of what follows exists to
keep them closed:

* failure paths returned errors without writing any audit row, so a stale
  element reference or a page stranded on a refused destination left no trace
  at all — the audit showed a gap where an attempt had been;
* no row recorded whether the turn was tainted, so "was JARVIS acting on a
  poisoned page when it did this?" could not be answered from the log;
* a plain ALLOW recorded no origin decision, so a successful action did not say
  on whose authority it happened. (The executor writes a `PERMISSION_DECISION`
  row, but that one is about `tool:browser_extract` — a different resource from
  `browser:http://example.com`.)

Service-level lifecycle — crashes, partial teardown, orphan processes — is
covered by ``test_browser_lifecycle.py`` from Step 3 and is not repeated. What
is tested here is the lifecycle as the *tools* see it, which is the part a
stale reference would travel through.
"""

from __future__ import annotations

import threading
from dataclasses import replace
from http.server import ThreadingHTTPServer

import pytest
from sqlalchemy import select

from jarvis.core import JarvisCore
from jarvis.db.models import (
    ActivityKind,
    ActivityLog,
    Capability,
    Confirmation,
    PermissionGrant,
    PermissionMode,
    ToolExecution,
)
from jarvis.errors import ConfirmationRequiredError, PermissionDeniedError
from jarvis.tools.base import ToolContext
from jarvis.tools.executor import ToolCall

from .test_browser_runtime import resolve_chromium
from .test_browser_tools import _Handler

# ── harness ──────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def audit_site() -> str:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
async def browsing(core, audit_site: str):
    settings, reason = await resolve_chromium()
    if settings is None:
        pytest.skip(f"No usable Chromium on this machine: {reason}")
    core.browser.settings = replace(settings, max_pages=4, allow_localhost=True)
    try:
        yield core
    finally:
        await core.browser.shutdown()


def _ctx(core, session, user, *, tainted: bool = False) -> ToolContext:
    return ToolContext(
        user_id=user.id,
        session=session,
        request_id="req_audit",
        tainted=tainted,
        extras={
            "embeddings": core.embeddings,
            "project_id": None,
            "computer": core.computer,
            "browser": core.browser,
            "activity": core.orchestrator._activity(session),
        },
    )


async def run_tool(core, name: str, arguments: dict, *, tainted: bool = False):
    async with core.database.session_factory() as session:
        user = await JarvisCore.ensure_default_user(session)
        await session.commit()
        executor = core.orchestrator._make_executor(session)
        try:
            outcome = await executor.execute(
                ToolCall(id="tu_1", name=name, arguments=arguments),
                _ctx(core, session, user, tainted=tainted),
            )
            await session.commit()
            return outcome
        except (ConfirmationRequiredError, PermissionDeniedError):
            await session.commit()
            raise


async def rows(core) -> list[ActivityLog]:
    async with core.database.session_factory() as session:
        return list(
            (
                await session.execute(
                    select(ActivityLog)
                    .where(ActivityLog.kind == ActivityKind.BROWSER_ACTION)
                    .order_by(ActivityLog.created_at)
                )
            ).scalars().all()
        )


async def audit(core) -> list[tuple[str, str, dict]]:
    return [(r.status, (r.detail or {}).get("operation"), r.detail or {})
            for r in await rows(core)]


async def open_page(core, url: str) -> str:
    outcome = await run_tool(core, "browser_open", {"url": url})
    assert outcome.result.is_error is False, outcome.result.content
    return outcome.result.data["page_id"]


async def inspect_ids(core, page_id: str) -> list[dict]:
    found = await run_tool(core, "browser_inspect", {"page_id": page_id})
    assert found.result.is_error is False, found.result.content
    return found.result.data["elements"]


async def confirm_last(core) -> None:
    from jarvis.confirmations.service import ConfirmationService

    async with core.database.session_factory() as session:
        row = (
            await session.execute(select(Confirmation).order_by(Confirmation.created_at))
        ).scalars().all()[-1]
        await ConfirmationService(session).decide(row.id, approved=True)
        await session.commit()


# ── audit omission: every failure leaves a trace ─────────────────────────────


@pytest.mark.parametrize(
    "tool,arguments,operation",
    [
        ("browser_navigate", {"page_id": "pg_nope", "url": "http://x.test/"},
         "navigate"),
        ("browser_inspect", {"page_id": "pg_nope"}, "inspect"),
        ("browser_extract", {"page_id": "pg_nope"}, "extract"),
        ("browser_click", {"page_id": "pg_nope", "element_id": "el_nope"}, "click"),
    ],
)
async def test_an_attempt_on_a_page_that_does_not_exist_is_recorded(
    browsing, tool, arguments, operation
) -> None:
    """A refused attempt is evidence. Silence is the absence of it.

    Before Step 7 these paths returned an error and wrote nothing, so an
    investigator reading the log saw no attempt rather than a refused one — the
    two look identical from the outside and mean very different things.
    """
    if tool == "browser_click":
        with pytest.raises(ConfirmationRequiredError):
            await run_tool(browsing, tool, arguments)
        await confirm_last(browsing)

    outcome = await run_tool(browsing, tool, arguments)
    assert outcome.result.is_error is True

    recorded = await audit(browsing)
    assert recorded, f"{tool} wrote no audit row at all"
    statuses = [(s, op) for s, op, _ in recorded]
    assert ("REFUSED", operation) in statuses, statuses


async def test_a_stale_element_reference_is_audited_for_click_and_fill(
    browsing, audit_site
) -> None:
    """REAL BROWSER. The reference dies with the navigation; the attempt does not.

    A model reusing an id from a page that has moved on is the single most
    likely wrong browser call, and the one an operator most wants to see.
    """
    page_id = await open_page(browsing, audit_site + "/form")
    elements = await inspect_ids(browsing, page_id)
    stale = elements[0]["element_id"]

    # Navigating bumps the generation, killing every reference on the page.
    moved = await run_tool(browsing, "browser_navigate",
                           {"page_id": page_id, "url": audit_site + "/"})
    assert moved.result.is_error is False

    for tool, extra in (("browser_click", {}), ("browser_fill", {"text": "x"})):
        args = {"page_id": page_id, "element_id": stale, **extra}
        with pytest.raises(ConfirmationRequiredError):
            await run_tool(browsing, tool, args)
        await confirm_last(browsing)
        outcome = await run_tool(browsing, tool, args)
        assert outcome.result.is_error is True, tool

    recorded = [(s, op) for s, op, _ in await audit(browsing)]
    assert ("REFUSED", "click") in recorded
    assert ("REFUSED", "fill") in recorded


async def test_a_page_stranded_on_a_refused_destination_audits_every_refusal(
    browsing, audit_site
) -> None:
    """REAL BROWSER, real redirect into the cloud metadata endpoint.

    The page is inert afterwards — Step 5 proved that. Step 7 adds that each
    tool's refusal is *visible*, because a page JARVIS cannot touch is exactly
    the state someone will later ask questions about.
    """
    page_id = await open_page(browsing, audit_site + "/")
    refused = await run_tool(
        browsing, "browser_navigate",
        {"page_id": page_id, "url": audit_site + "/redirect-to-metadata"},
    )
    assert refused.result.data["verdict"] == "REDIRECT_VIOLATION"

    for tool in ("browser_inspect", "browser_extract"):
        outcome = await run_tool(browsing, tool, {"page_id": page_id})
        assert outcome.result.is_error is True, tool

    recorded = [(s, op) for s, op, _ in await audit(browsing)]
    assert ("REFUSED", "inspect") in recorded
    assert ("REFUSED", "extract") in recorded


async def test_closing_a_page_that_is_not_open_is_recorded_as_a_noop(
    browsing,
) -> None:
    """Not an error — the caller got what they wanted — but still an attempt."""
    outcome = await run_tool(browsing, "browser_close_page",
                             {"page_id": "pg_never_existed"})
    assert outcome.result.is_error is False
    assert outcome.result.data["closed"] is False

    recorded = [(s, op) for s, op, _ in await audit(browsing)]
    assert ("NOOP", "close_page") in recorded


# ── audit status corruption: a failure must never read as a success ──────────


async def test_a_refused_navigation_is_never_recorded_as_ok(
    browsing, audit_site
) -> None:
    """The status must match what happened.

    A row that says OK for an action that did not happen is worse than no row:
    no row is a gap someone notices, and a wrong row is one they do not.
    """
    await run_tool(browsing, "browser_open", {"url": "file:///etc/passwd"})
    await run_tool(browsing, "browser_open", {"url": "http://169.254.169.254/"})

    for status, operation, _ in await audit(browsing):
        assert status != "OK", "a refused open must not be recorded as OK"
        assert operation == "navigate"

    assert browsing.browser.page_count == 0


async def test_a_successful_action_is_recorded_as_ok(browsing, audit_site) -> None:
    """The converse, so the test above cannot pass by everything being REFUSED."""
    await open_page(browsing, audit_site + "/")
    assert ("OK", "navigate") in [(s, op) for s, op, _ in await audit(browsing)]


# ── taint: the audit distinguishes tainted execution from clean ──────────────


async def test_every_browser_row_records_whether_the_turn_was_tainted(
    browsing, audit_site
) -> None:
    """Stamped centrally, so no call site can produce a row that reads clean."""
    page_id = await open_page(browsing, audit_site + "/")
    await run_tool(browsing, "browser_extract", {"page_id": page_id})
    await run_tool(browsing, "browser_inspect", {"page_id": page_id})
    await run_tool(browsing, "browser_close_page", {"page_id": page_id})

    recorded = await audit(browsing)
    assert recorded
    for status, operation, detail in recorded:
        assert "tainted" in detail, f"{operation}/{status} lost the taint flag"
        assert detail["tainted"] is False


async def test_a_tainted_action_is_distinguishable_in_the_log(
    browsing, audit_site
) -> None:
    """The question an incident review asks first.

    Two identical extracts, one on a tainted turn and one not. If the audit
    could not tell them apart, "was JARVIS acting on something a web page told
    it to do?" would have no answer in the record.
    """
    page_id = await open_page(browsing, audit_site + "/")

    await run_tool(browsing, "browser_extract", {"page_id": page_id})
    await run_tool(browsing, "browser_extract", {"page_id": page_id},
                   tainted=True)

    extracts = [d for _, op, d in await audit(browsing) if op == "extract"]
    assert [d["tainted"] for d in extracts] == [False, True]


async def test_a_denial_records_the_taint_it_was_decided_under(
    browsing, audit_site
) -> None:
    """Taint changes the decision, so the row must say it was present.

    Otherwise a DENY that only happened because the turn was poisoned reads
    identically to one from a standing operator rule.
    """
    from jarvis.browser.urls import UrlPolicy

    origin = UrlPolicy(allow_localhost=True).check(audit_site + "/").origin
    async with browsing.database.session_factory() as session:
        user = await JarvisCore.ensure_default_user(session)
        session.add(
            PermissionGrant(
                user_id=user.id, capability=Capability.READ,
                resource_scope=f"browser:{origin}", mode=PermissionMode.DENY,
            )
        )
        await session.commit()

    with pytest.raises(PermissionDeniedError):
        await run_tool(browsing, "browser_open", {"url": audit_site + "/"},
                       tainted=True)

    denials = [d for s, _, d in await audit(browsing) if s == "DENIED"]
    assert denials, "the denial was not audited"
    assert denials[-1]["tainted"] is True


# ── permission decision on the permitted path ────────────────────────────────


async def test_a_permitted_action_records_the_authority_it_acted_under(
    browsing, audit_site
) -> None:
    """A plain ALLOW used to leave the origin decision nowhere.

    The executor's PERMISSION_DECISION row is about ``tool:browser_extract``.
    That is a different resource from ``browser:http://127.0.0.1:PORT``, and
    only the latter answers "was this site permitted?".
    """
    page_id = await open_page(browsing, audit_site + "/")
    await run_tool(browsing, "browser_extract", {"page_id": page_id})

    extract = [d for s, op, d in await audit(browsing)
               if op == "extract" and s == "OK"][-1]
    assert extract["decision"] == "ALLOW"
    assert "rules" in extract
    assert extract["confirmed"] is False
    assert extract["origin"].startswith("http://127.0.0.1:")


async def test_an_approved_interaction_records_that_it_was_confirmed(
    browsing, audit_site
) -> None:
    """Confirmation state belongs on the action's own row, not only on the ask."""
    page_id = await open_page(browsing, audit_site + "/form")
    button = next(e for e in await inspect_ids(browsing, page_id)
                  if e["role"] == "button")
    args = {"page_id": page_id, "element_id": button["element_id"]}

    with pytest.raises(ConfirmationRequiredError):
        await run_tool(browsing, "browser_click", args)
    await confirm_last(browsing)
    outcome = await run_tool(browsing, "browser_click", args)
    assert outcome.result.is_error is False

    click = [d for s, op, d in await audit(browsing)
             if op == "click" and s == "OK"][-1]
    assert click["confirmed"] is True
    assert click["decision"] == "ASK", (
        "an interaction is irreversible, so the engine's floor makes it ASK"
    )
    assert click["element_id"] == button["element_id"]
    assert click["page_id"] == page_id


# ── lifecycle as the tools see it ────────────────────────────────────────────


async def test_no_reference_survives_a_browser_restart(
    browsing, audit_site
) -> None:
    """REAL BROWSER, shut down and started again.

    Service-level teardown is Step 3's territory. What this adds is the path a
    stale id would actually travel: the model still has the old page_id and
    element_id in its transcript, and reuse must fail by lookup rather than
    land on whatever now occupies that slot.
    """
    page_id = await open_page(browsing, audit_site + "/form")
    element_id = (await inspect_ids(browsing, page_id))[0]["element_id"]

    await browsing.browser.shutdown()
    assert browsing.browser.page_count == 0

    # The browser starts again on demand, with a fresh context.
    reopened = await open_page(browsing, audit_site + "/form")
    assert reopened != page_id, "ids are random, so reuse cannot be accidental"

    dead = await run_tool(browsing, "browser_extract", {"page_id": page_id})
    assert dead.result.is_error is True

    args = {"page_id": reopened, "element_id": element_id}
    with pytest.raises(ConfirmationRequiredError):
        await run_tool(browsing, "browser_click", args)
    await confirm_last(browsing)
    stale = await run_tool(browsing, "browser_click", args)
    assert stale.result.is_error is True, (
        "an element id from before the restart must not resolve against the "
        "new page"
    )

    recorded = [(s, op) for s, op, _ in await audit(browsing)]
    assert ("REFUSED", "extract") in recorded
    assert ("REFUSED", "click") in recorded


async def test_a_full_tool_cycle_leaves_no_chromium_behind(
    browsing, audit_site
) -> None:
    """Open, act, close, shut down — and nothing is still running.

    Asserted against the service's own view rather than by scanning the process
    table, which would be a platform-specific assumption.
    """
    page_id = await open_page(browsing, audit_site + "/")
    await run_tool(browsing, "browser_extract", {"page_id": page_id})
    await run_tool(browsing, "browser_close_page", {"page_id": page_id})

    report = await browsing.browser.shutdown()
    assert report.clean is True, report.describe()
    assert browsing.browser.running is False
    assert browsing.browser.page_count == 0

    # Idempotent: the fixture will shut down again on the way out.
    again = await browsing.browser.shutdown()
    assert again.clean is True


# ── credential values, on every path ─────────────────────────────────────────


async def test_no_refusal_path_persists_the_value_that_was_going_to_be_typed(
    browsing, audit_site
) -> None:
    """Three ways a fill can fail, and none of them may keep the text.

    The stale-reference path is the new one: Step 7 gave it an audit row, and a
    row is exactly where a value would have started leaking.
    """
    secret = "value-that-must-not-persist-8812"
    page_id = await open_page(browsing, audit_site + "/login")
    elements = await inspect_ids(browsing, page_id)
    password = next(e for e in elements if e.get("name") == "password")

    attempts = [
        # Credential refusal, against the live DOM.
        {"page_id": page_id, "element_id": password["element_id"], "text": secret},
        # A reference that does not exist.
        {"page_id": page_id, "element_id": "el_invented", "text": secret},
        # A page that does not exist.
        {"page_id": "pg_invented", "element_id": "el_invented", "text": secret},
    ]
    for args in attempts:
        with pytest.raises(ConfirmationRequiredError):
            await run_tool(browsing, "browser_fill", args)
        await confirm_last(browsing)
        outcome = await run_tool(browsing, "browser_fill", args)
        assert outcome.result.is_error is True, args

    async with browsing.database.session_factory() as session:
        logs = (await session.execute(select(ActivityLog))).scalars().all()
        execs = (await session.execute(select(ToolExecution))).scalars().all()
        confirmations = (await session.execute(select(Confirmation))).scalars().all()

    persisted = str([(r.summary, r.detail) for r in logs])
    persisted += str([(e.arguments, e.result) for e in execs])
    persisted += str([c.action for c in confirmations])
    assert secret not in persisted, "a refused fill kept the value somewhere"

    # The confirmation *body* is the deliberate exception: the user has to see
    # what they are approving. Asserted rather than left implicit, so that the
    # exception stays a decision instead of becoming an oversight.
    assert any(secret in c.body for c in confirmations)
