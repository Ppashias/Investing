"""The browser's outward-facing surfaces (Phase 4, Step 9).

Step 8 checked what `/api/activity` returns *after* the fact. Two exposure
surfaces were left unverified, and the Step 9 coverage review found both:

* **The live stream.** `/api/activity/stream` broadcasts every activity event
  to any connected client the moment it happens. A value redacted in the
  database but present on the wire would be worse than a database leak, not
  better — it reaches a browser tab immediately and leaves no row to audit.
  Nothing tested it, for browser events or for anything else.

* **The absence of a direct browser endpoint.** Browser actions reach the
  browser only through `ToolExecutor`, and today that is true because no HTTP
  route touches `BrowserService`. Nothing pinned it. "True by inspection" is
  how a `POST /api/browser/navigate` gets added next year with the best of
  intentions and no permission check.

Both are structural claims the codebase already makes elsewhere — "no
credential store", "no Playwright in the tools" — and both are now enforced
rather than assumed.
"""

from __future__ import annotations

import json
import threading
from dataclasses import replace
from http.server import ThreadingHTTPServer

import pytest

from jarvis.core import JarvisCore
from jarvis.db.models import ActivityKind

from .conftest import text_result, tool_result
from .test_browser_runtime import resolve_chromium
from .test_browser_tools import _Handler

# ── harness ──────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def exposure_site() -> str:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
async def browsing(core, exposure_site: str):
    settings, reason = await resolve_chromium()
    if settings is None:
        pytest.skip(f"No usable Chromium on this machine: {reason}")
    core.browser.settings = replace(settings, max_pages=4, allow_localhost=True)
    try:
        yield core
    finally:
        await core.browser.shutdown()


async def turn(core, message: str, *responses):
    core.providers.get("stub").responses = list(responses)
    async with core.database.session_factory() as session:
        user = await JarvisCore.ensure_default_user(session)
        await session.commit()
        return await core.orchestrator.handle(
            session=session, user=user, message=message
        )


def page_of(core) -> str:
    handles = core.browser.pages()
    assert handles, "expected an open page"
    return handles[-1].page_id


# ── the live stream ──────────────────────────────────────────────────────────


async def test_browser_actions_are_broadcast_on_the_activity_bus(
    browsing, exposure_site
) -> None:
    """REAL BROWSER. The bus is what the SSE endpoint serialises.

    Subscribing directly rather than over HTTP keeps this test about the
    *content* of the broadcast; the endpoint's framing is checked separately
    below. Both halves matter, and testing them together would leave it unclear
    which one failed.
    """
    queue = await browsing.activity_bus.subscribe()
    try:
        await turn(
            browsing, "open and read it",
            tool_result("browser_open", {"url": exposure_site + "/"}, call_id="a"),
            lambda _r: tool_result("browser_extract", {"page_id": page_of(browsing)},
                                   call_id="b"),
            text_result("read it"),
        )

        events = []
        while not queue.empty():
            events.append(queue.get_nowait())
    finally:
        await browsing.activity_bus.unsubscribe(queue)

    browser_events = [e for e in events if e.kind == ActivityKind.BROWSER_ACTION.value]
    assert browser_events, "no browser action reached the live stream"

    operations = {(e.detail or {}).get("operation") for e in browser_events}
    assert {"navigate", "extract"} <= operations

    extract = next(e for e in browser_events
                   if (e.detail or {}).get("operation") == "extract")
    assert extract.status == "OK"
    assert extract.detail["origin"].startswith("http://127.0.0.1:")
    assert extract.detail["tainted"] is False
    assert extract.detail["decision"] == "ALLOW"


async def test_a_filled_value_is_not_broadcast_to_live_subscribers(
    browsing, exposure_site
) -> None:
    """REAL BROWSER, real form. The leak that would be worse than a database leak.

    A row in the database can be found and removed. A value pushed to every
    connected client the instant it is typed cannot be. Redaction happens in the
    executor, before ``ActivityService.record`` runs, so the bus receives the
    same redacted detail — but "should" and "does" are different claims and only
    one of them is testable.
    """
    typed = "broadcast-secret-40917"

    await turn(browsing, "open the form",
               tool_result("browser_open", {"url": exposure_site + "/form"},
                           call_id="a"),
               text_result("open"))
    page_id = page_of(browsing)
    found = await turn(browsing, "inspect",
                       tool_result("browser_inspect", {"page_id": page_id},
                                   call_id="b"),
                       text_result("ok"))
    box = next(e for e in found.tool_calls[0]["data"]["elements"]
               if e["role"] in ("textbox", "searchbox"))
    args = {"page_id": page_id, "element_id": box["element_id"], "text": typed}

    queue = await browsing.activity_bus.subscribe()
    try:
        asked = await turn(browsing, "type it",
                           tool_result("browser_fill", args, call_id="c"),
                           text_result("unreachable"))
        assert asked.status == "needs_confirmation"

        from jarvis.confirmations.service import ConfirmationService
        from jarvis.db.models import Confirmation, ConfirmationStatus
        from sqlalchemy import select

        async with browsing.database.session_factory() as session:
            rows = (await session.execute(select(Confirmation))).scalars().all()
            pending = [r for r in rows if r.status is ConfirmationStatus.PENDING]
            await ConfirmationService(session).decide(pending[-1].id, approved=True)
            await session.commit()

        done = await turn(browsing, "yes",
                          tool_result("browser_fill", args, call_id="d"),
                          text_result("typed"))
        assert done.tool_calls[0]["is_error"] is False

        broadcast = []
        while not queue.empty():
            broadcast.append(queue.get_nowait())
    finally:
        await browsing.activity_bus.unsubscribe(queue)

    assert broadcast, "nothing was broadcast at all"
    wire = json.dumps([e.to_dict() for e in broadcast])
    assert typed not in wire, "the typed value went out on the live stream"

    # The event itself still happened — redaction must not cost the record.
    fills = [e for e in broadcast if (e.detail or {}).get("operation") == "fill"]
    assert any(e.status == "OK" for e in fills)


async def test_a_refused_credential_is_not_broadcast_either(
    browsing, exposure_site
) -> None:
    """REAL BROWSER. The refusal path, on the live stream.

    A password the model attempted is the worst thing that could be pushed to a
    connected client, and the confirmation exists before the DOM check refuses
    the fill — so the broadcast happens while nothing yet knows the field was
    forbidden.
    """
    secret = "broadcast-credential-11923"

    await turn(browsing, "open login",
               tool_result("browser_open", {"url": exposure_site + "/login"},
                           call_id="a"),
               text_result("open"))
    page_id = page_of(browsing)
    found = await turn(browsing, "inspect",
                       tool_result("browser_inspect", {"page_id": page_id},
                                   call_id="b"),
                       text_result("ok"))
    password = next(e for e in found.tool_calls[0]["data"]["elements"]
                    if e.get("name") == "password")
    args = {"page_id": page_id, "element_id": password["element_id"], "text": secret}

    queue = await browsing.activity_bus.subscribe()
    try:
        await turn(browsing, "sign in",
                   tool_result("browser_fill", args, call_id="c"),
                   text_result("x"))

        from jarvis.confirmations.service import ConfirmationService
        from jarvis.db.models import Confirmation, ConfirmationStatus
        from sqlalchemy import select

        async with browsing.database.session_factory() as session:
            rows = (await session.execute(select(Confirmation))).scalars().all()
            pending = [r for r in rows if r.status is ConfirmationStatus.PENDING]
            await ConfirmationService(session).decide(pending[-1].id, approved=True)
            await session.commit()

        refused = await turn(browsing, "yes",
                             tool_result("browser_fill", args, call_id="d"),
                             text_result("refused"))
        assert refused.tool_calls[0]["data"]["credential_field"] is True

        broadcast = []
        while not queue.empty():
            broadcast.append(queue.get_nowait())
    finally:
        await browsing.activity_bus.unsubscribe(queue)

    wire = json.dumps([e.to_dict() for e in broadcast])
    assert secret not in wire, "the credential went out on the live stream"

    fills = [e for e in broadcast if (e.detail or {}).get("operation") == "fill"]
    assert any(e.status == "REFUSED" for e in fills), (
        "the refusal must still be visible to a live watcher"
    )


async def test_the_sse_endpoint_frames_activity_events(core) -> None:
    """The endpoint's own contract: a published event becomes an SSE frame.

    Driven in-process against the real route function rather than over a
    socket. That is a deliberate limitation, not a shortcut, and it is worth
    stating why: ``TestClient`` runs the app in a portal thread with its own
    event loop, and the stream's ``asyncio.Queue`` belongs to that loop.
    Publishing from the test thread never wakes the waiting coroutine, so an
    HTTP-level version of this test hangs rather than fails — a worse outcome
    than not having it.

    What runs here is the endpoint's actual generator, framing an actual
    published event. What is *not* covered is the socket between them: headers,
    chunked transfer, and client disconnect. Recorded as such rather than
    claimed as end-to-end.
    """
    import asyncio

    from jarvis.activity.service import ActivityEvent
    from jarvis.api.routes import stream_activity

    response = await stream_activity(core, None)
    assert response.media_type == "text/event-stream"
    frames = response.body_iterator

    first = await asyncio.wait_for(frames.__anext__(), timeout=5)
    assert "event: ready" in _text(first)

    core.activity_bus.publish(
        ActivityEvent(
            kind=ActivityKind.BROWSER_ACTION.value,
            actor="agent",
            summary="Opened http://example.test/",
            detail={"operation": "navigate", "origin": "http://example.test:80",
                    "tainted": False, "decision": "ALLOW"},
            status="OK",
            tool_name="browser:navigate",
        )
    )

    frame = _text(await asyncio.wait_for(frames.__anext__(), timeout=5))
    await frames.aclose()

    assert frame.startswith("event: activity")
    payload = json.loads(frame.split("data: ", 1)[1])
    assert payload["kind"] == "BROWSER_ACTION"
    assert payload["status"] == "OK"
    assert payload["detail"]["operation"] == "navigate"
    assert payload["detail"]["tainted"] is False
    assert payload["tool_name"] == "browser:navigate"


def _text(chunk) -> str:
    return chunk.decode() if isinstance(chunk, (bytes, bytearray)) else chunk


# ── the absence of a direct browser endpoint ─────────────────────────────────


def test_no_http_route_reaches_the_browser_directly(client) -> None:
    """Browser actions go through ``ToolExecutor`` or they do not happen.

    Today that holds because no route touches ``BrowserService`` — but "true by
    inspection" is how ``POST /api/browser/navigate`` gets added next year with
    the best of intentions and no permission check. Pinned against the real
    route table rather than a source grep, so a route added anywhere fails here.

    ``browser_status`` and the rest stay reachable the way every other tool is:
    the model asks, the executor decides.
    """
    paths = {getattr(route, "path", "") for route in client.app.routes}
    offending = [p for p in paths if "browser" in p.lower()]
    assert not offending, f"HTTP routes reach the browser directly: {offending}"


def test_the_api_module_does_not_import_the_browser_service() -> None:
    """The other half: no route *body* can reach it either.

    A route named innocuously could still hold a ``BrowserService``. The API
    layer knows about the core, and the core owns the browser for its lifecycle
    — what must not appear is the API reaching past that to drive it.
    """
    import inspect

    from jarvis.api import routes

    source = inspect.getsource(routes)
    for forbidden in ("BrowserService", "new_page(", "browser.page(",
                      "core.browser", "operations.navigate", "goto("):
        assert forbidden not in source, f"api/routes.py reaches the browser: {forbidden}"


async def _drain_sse(frames, *, limit: int = 200) -> list[str]:
    """Everything the generator has ready, without waiting for a heartbeat."""
    import asyncio

    out: list[str] = []
    for _ in range(limit):
        try:
            chunk = await asyncio.wait_for(frames.__anext__(), timeout=0.25)
        except (asyncio.TimeoutError, StopAsyncIteration):
            break
        out.append(_text(chunk))
    return out


async def test_a_real_browser_fill_reaches_sse_without_its_value(
    browsing, exposure_site
) -> None:
    """REAL BROWSER through the REAL SSE generator. The end-to-end version.

    The other SSE test frames a synthetic event; this one drives an actual fill
    on an actual form and reads what a connected client would have received.
    Both halves of the requirement are checked against the same frames: that a
    browser action *arrives*, and that the value typed into the form does not.

    The generator is consumed in the test's own event loop, which is what makes
    this possible at all — see the note on ``TestClient`` in the sibling test.
    """
    import asyncio

    from jarvis.api.routes import stream_activity
    from jarvis.confirmations.service import ConfirmationService
    from jarvis.db.models import Confirmation, ConfirmationStatus
    from sqlalchemy import select

    typed = "sse-end-to-end-secret-63104"

    await turn(browsing, "open the form",
               tool_result("browser_open", {"url": exposure_site + "/form"},
                           call_id="a"),
               text_result("open"))
    page_id = page_of(browsing)
    found = await turn(browsing, "inspect",
                       tool_result("browser_inspect", {"page_id": page_id},
                                   call_id="b"),
                       text_result("ok"))
    box = next(e for e in found.tool_calls[0]["data"]["elements"]
               if e["role"] in ("textbox", "searchbox"))
    args = {"page_id": page_id, "element_id": box["element_id"], "text": typed}

    response = await stream_activity(browsing, None)
    frames = response.body_iterator
    try:
        ready = _text(await asyncio.wait_for(frames.__anext__(), timeout=5))
        assert "event: ready" in ready

        asked = await turn(browsing, "type it",
                           tool_result("browser_fill", args, call_id="c"),
                           text_result("unreachable"))
        assert asked.status == "needs_confirmation"

        async with browsing.database.session_factory() as session:
            rows = (await session.execute(select(Confirmation))).scalars().all()
            pending = [r for r in rows if r.status is ConfirmationStatus.PENDING]
            await ConfirmationService(session).decide(pending[-1].id, approved=True)
            await session.commit()

        done = await turn(browsing, "yes",
                          tool_result("browser_fill", args, call_id="d"),
                          text_result("typed"))
        assert done.tool_calls[0]["is_error"] is False

        received = await _drain_sse(frames)
    finally:
        await frames.aclose()

    wire = "".join(received)

    # A browser action really did reach the consumer.
    payloads = [json.loads(f.split("data: ", 1)[1])
                for f in received if f.startswith("event: activity")]
    browser = [p for p in payloads if p["kind"] == "BROWSER_ACTION"]
    assert browser, "no browser action reached the SSE consumer"
    assert any((p.get("detail") or {}).get("operation") == "fill" for p in browser)

    # And the value did not.
    assert typed not in wire, "the typed value was sent to SSE consumers"
