"""The command centre: event contract, read APIs, and what the UI cannot do.

The front end is the console, not the authority. It displays state, requests
actions, receives events and requests approvals — it never decides. Most of the
tests here assert an absence, because that is what "not the authority" means in
practice: no control that spawns an agent, widens a ceiling, edits a grant, or
clears taint.
"""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from jarvis.activity.service import ActivityEvent
from jarvis.db.models import ActivityKind
from jarvis.events.schema import EVENT_NAMES, LOUD, classify, envelope


def _event(kind, *, status="", detail=None, summary="s", tool=None):
    return ActivityEvent(
        kind=kind.value if isinstance(kind, ActivityKind) else kind,
        actor="a", summary=summary, status=status, tool_name=tool,
        detail=detail or {},
    )


# ── the event vocabulary is closed ───────────────────────────────────────────


def test_the_console_vocabulary_is_exactly_what_was_specified() -> None:
    """Named individually rather than counted. A count passes when one event
    is swapped for another, and the point is *which* names a panel may key
    off — a UI that renders unknown event types renders attacker-chosen
    strings."""
    assert EVENT_NAMES == {
        "agent.started", "agent.updated", "agent.completed", "agent.failed",
        "agent.cancelled", "tool.called", "tool.completed", "tool.denied",
        "approval.required", "approval.granted", "approval.rejected",
        "task.started", "task.progress", "task.paused", "task.resumed",
        "task.completed", "task.failed", "computer.action_started",
        "computer.action_completed", "computer.screenshot_updated",
        "browser.navigation", "browser.action", "memory.proposed",
        "memory.approved", "memory.rejected", "security.alert",
        "emergency_stop", "system.status",
    }


@pytest.mark.parametrize(
    "kind,status,detail,expected",
    [
        (ActivityKind.CONFIRMATION_REQUESTED, "", {}, "approval.required"),
        (ActivityKind.CONFIRMATION_RESOLVED, "", {"approved": True},
         "approval.granted"),
        (ActivityKind.CONFIRMATION_RESOLVED, "", {"approved": False},
         "approval.rejected"),
        (ActivityKind.PERMISSION_DECISION, "DENY", {}, "tool.denied"),
        (ActivityKind.PERMISSION_DECISION, "ALLOW", {}, None),
        (ActivityKind.BROWSER_ACTION, "OK", {"operation": "navigate"},
         "browser.navigation"),
        (ActivityKind.MEMORY_CAPTURED, "PROPOSED", {}, "memory.proposed"),
        (ActivityKind.EMERGENCY_STOP, "", {}, "emergency_stop"),
        # Nothing the console has a view for. Dropping is correct: a catch-all
        # name would fill every panel with rows it cannot render.
        (ActivityKind.MODEL_CALL, "OK", {}, None),
        (ActivityKind.STAGE_STARTED, "", {}, None),
    ],
)
def test_activity_records_classify_into_console_events(
    kind, status, detail, expected
) -> None:
    assert classify(_event(kind, status=status, detail=detail)) == expected


def test_any_refusal_reaches_the_security_panel(subtests=None) -> None:
    """Collected by status rather than per subsystem, so a new subsystem's
    refusals arrive without anybody remembering to add them."""
    for kind in (ActivityKind.OBSIDIAN_ACTION, ActivityKind.COMPUTER_TASK,
                 ActivityKind.KNOWLEDGE_INGESTED):
        assert classify(_event(kind, status="REFUSED")) == "security.alert", kind


def test_events_needing_a_human_are_marked_loud() -> None:
    """A scrolling feed is worst at exactly the things that most need
    noticing."""
    assert "approval.required" in LOUD
    assert "tool.denied" in LOUD
    assert "emergency_stop" in LOUD
    assert "tool.called" not in LOUD


# ── the envelope drops what it was not asked to carry ────────────────────────


def test_the_envelope_allowlists_detail_fields() -> None:
    """An allowlist rather than a denylist.

    The detail dict is open-ended, so a tool that starts recording something
    new would otherwise begin streaming it to every connected tab — and nobody
    would notice until it was a password.
    """
    payload = envelope(_event(
        ActivityKind.BROWSER_ACTION, status="OK",
        detail={"operation": "navigate", "url": "http://x", "secret": "hunter2",
                "cookie": "abc"},
    ))
    assert payload["detail"] == {"operation": "navigate", "url": "http://x"}
    assert "hunter2" not in str(payload)


def test_the_envelope_shape_is_fixed() -> None:
    """So the client never branches on which producer sent something."""
    payload = envelope(_event(ActivityKind.CONFIRMATION_REQUESTED))
    assert set(payload) == {
        "event", "loud", "at", "actor", "summary", "status", "tool",
        "request_id", "detail",
    }


# ── the stream ───────────────────────────────────────────────────────────────


def test_the_console_stream_is_authenticated(client: TestClient) -> None:
    """Same AuthDep as everything else. A console stream that forgot it would
    narrate this machine's activity to anything that could reach the port."""
    import inspect

    from jarvis.api.routes import stream_console

    assert "_" in inspect.signature(stream_console).parameters


def test_the_console_stream_carries_the_token_in_a_header(
    client: TestClient
) -> None:
    """Not in the query string, which is the whole reason this is SSE-over-fetch
    rather than a WebSocket.

    A browser cannot set headers on a WebSocket handshake, so authenticating one
    means the token in the URL — reaching server logs, browser history and
    Referer, which is the exact finding the Phase 0 audit raised against the old
    dashboard.
    """
    app_js = client.get("/assets/app.js").text
    assert 'headers.Authorization = "Bearer "' in app_js
    assert "?token=" not in app_js
    assert "new WebSocket" not in app_js


# ── the console is not the authority ─────────────────────────────────────────


def _console_code(client: TestClient) -> str:
    source = client.get("/assets/console.js").text
    code = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    return re.sub(r"^\s*//.*$", "", code, flags=re.M)


def test_the_console_cannot_spawn_an_agent(client: TestClient) -> None:
    """Spawning hands authority to a second actor, so it goes through
    spawn_agent — a tool, and therefore ToolExecutor, the permission engine and
    the confirmation flow. A console button that spawned one directly would be
    the bypass this whole architecture exists to prevent."""
    code = _console_code(client)
    assert "spawn" not in code
    # …and there is no endpoint for it either.
    assert client.post("/api/agents").status_code in (404, 405)


def test_the_console_cannot_widen_a_ceiling_or_edit_a_grant(
    client: TestClient
) -> None:
    """Their absence is the design, not a missing feature."""
    code = _console_code(client)
    for forbidden in ("capabilities =", "ceiling", "grant(", "/permissions",
                      "PATCH", "tainted = false"):
        assert forbidden not in code, f"console.js contains {forbidden}"


def test_the_console_never_uses_innerhtml(client: TestClient) -> None:
    """Event summaries are JARVIS's prose and detail values can carry
    page-authored text. textContent is the boundary, and this is what keeps it
    one."""
    for asset in ("console.js", "app.js", "hud.js", "voice.js"):
        code = _console_code_for(client, asset)
        assert "innerHTML" not in code, f"{asset} uses innerHTML"
        assert "outerHTML" not in code, f"{asset} uses outerHTML"
        assert "insertAdjacentHTML" not in code, f"{asset} inserts HTML"


def _console_code_for(client: TestClient, asset: str) -> str:
    source = client.get("/assets/" + asset).text
    code = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    return re.sub(r"^\s*//.*$", "", code, flags=re.M)


# ── the read APIs ────────────────────────────────────────────────────────────


def test_agents_endpoint_is_read_only(client: TestClient) -> None:
    body = client.get("/api/agents").json()
    assert "jobs" in body and "root" in body
    # The root agent has no ceiling: for the user's own loop the grants are the
    # whole story.
    assert body["root"]["capabilities"] is None


def test_security_endpoint_reports_grants_without_offering_to_change_them(
    client: TestClient
) -> None:
    body = client.get("/api/security").json()
    for key in ("emergency_stop", "browser", "computer", "grants",
                "denied_last_24h", "running_jobs"):
        assert key in body, key
    # There is no POST/PATCH on this router at all.
    assert client.post("/api/security").status_code in (404, 405)


def test_job_control_rejects_an_unknown_action(client: TestClient) -> None:
    """Pause, resume and cancel withdraw authority. Nothing else is offered,
    and `start` is deliberately absent — starting work is a tool call."""
    assert client.post("/api/agents/jobs/job_x/start").status_code == 400
    assert client.post("/api/agents/jobs/job_x/destroy").status_code == 400


@pytest.fixture
def locked_client(core, monkeypatch):
    """Auth switched on, no token supplied. Mirrors the fixture in
    test_api_and_security rather than importing it, because a fixture shared
    across files by import is a fixture that silently changes when the other
    file's needs change."""
    from jarvis.api.app import create_app
    from jarvis.config import Settings, reset_config_caches

    monkeypatch.setenv("JARVIS_API_TOKEN", "test-token-abcdefghijklmnop")
    reset_config_caches()
    settings = Settings(environment="test",
                        database_url="sqlite+aiosqlite:///:memory:",
                        require_auth=True, log_level="CRITICAL")
    core.settings = settings
    with TestClient(create_app(settings, core=core)) as c:
        yield c


def test_every_console_endpoint_requires_a_token(locked_client) -> None:
    """The new surface must not be the one that forgot.

    Enumerated rather than spot-checked: each of these narrates or controls
    something, and one unauthenticated route is the whole boundary.
    """
    for path in ("/api/agents", "/api/security", "/api/activity/console"):
        assert locked_client.get(path).status_code == 401, path
    assert locked_client.post(
        "/api/agents/jobs/job_x/cancel"
    ).status_code == 401


def test_an_invalid_token_is_refused_like_a_missing_one(locked_client) -> None:
    """Distinguishing them would tell an attacker when they had found a real
    token prefix."""
    for path in ("/api/agents", "/api/security"):
        response = locked_client.get(
            path, headers={"Authorization": "Bearer not-the-token"}
        )
        assert response.status_code == 401, path


def test_the_right_token_reaches_the_console_apis(locked_client) -> None:
    headers = {"Authorization": "Bearer test-token-abcdefghijklmnop"}
    assert locked_client.get("/api/agents", headers=headers).status_code == 200
    assert locked_client.get("/api/security", headers=headers).status_code == 200


# ── computer and browser panels ──────────────────────────────────────────────


def test_no_element_id_appears_twice(client: TestClient) -> None:
    """A duplicate id makes getElementById return the first match, so one of
    the two controls silently stops working.

    Caught for real: the console's Observe button collided with the existing
    Computer tab's, which would have left one of them dead with no error
    anywhere.
    """
    import collections
    import re

    ids = re.findall(r'id="([^"]+)"', client.get("/").text)
    dupes = [i for i, n in collections.Counter(ids).items() if n > 1]
    assert dupes == [], f"duplicate element ids: {dupes}"


def test_desktop_observation_goes_through_the_policy_engine(
    client: TestClient
) -> None:
    """The console calls /computer/observe, which runs the action through
    ComputerService.execute_action like any other.

    So a machine with no display, or a user without the SCREEN scope, gets a
    refusal rather than a blank panel — and the console never touches a
    backend directly.
    """
    code = _console_code(client)
    assert "/computer/observe" in code
    # Reading `data.computer.backend` off a status payload is fine; what must
    # not appear is anything that *drives* one, or the raw action endpoint
    # that would let the console compose a click of its own.
    for forbidden in ("X11Backend", "WindowsBackend", "xtest", "pyautogui",
                      "/computer/action", "execute_action"):
        assert forbidden not in code, f"console.js reaches {forbidden}"


def test_the_console_does_not_stream_the_screen(client: TestClient) -> None:
    """Observation is pull-based, because a live screenshot feed is a
    recording, and a recording of somebody's desktop is the thing this system
    most has to not become."""
    code = _console_code(client)
    assert "setInterval" not in code
    assert "addEventListener(\"click\", observe)" in code


def test_the_browser_panel_offers_no_take_control(client: TestClient) -> None:
    """Handing a human the keyboard on a page JARVIS opened means their next
    click happens in a context the policy engine authorised for JARVIS, and
    the backend cannot tell the two apart afterwards. Closing the page is the
    honest version, and it is a tool call."""
    code = _console_code(client)
    for forbidden in ("take_control", "takeControl", "browser_click",
                      "browser_navigate"):
        assert forbidden not in code, f"console.js contains {forbidden}"


def test_the_status_browser_block_carries_no_page_titles(client: TestClient) -> None:
    """Ids and addresses only.

    A title is page-authored, and the rest of that block is configuration.
    Mixing untrusted text into it would make one dict two trust levels, which
    is how a consumer forgets which half needs escaping.
    """
    body = client.get("/api/security").json()
    for page in body["browser"].get("pages", []):
        assert set(page) == {"page_id", "url"}


# ── the status reactor ───────────────────────────────────────────────────────


def test_the_reactor_carries_state_rather_than_spinning(client: TestClient) -> None:
    """The largest borrowing from the films, and it earns the space only if it
    means something.

    A decorative ring at that size would be a lie the size of the panel — the
    most prominent thing on screen saying nothing. Each ring is a reading, and
    the console publishes them.
    """
    reactor = _console_code_for(client, "reactor.js")
    for reading in ("approvals", "jobs", "denials", "mode"):
        assert reading in reactor, f"the reactor ignores {reading}"

    console = _console_code(client)
    assert 'CustomEvent("jarvis:reading"' in console


def test_every_ring_has_its_number_printed_beside_it(client: TestClient) -> None:
    """Colour is reinforcement, never the only signal — the rule the rest of
    this interface follows. Someone who cannot separate gold from cyan still
    has to be able to read "3 awaiting you"."""
    page = client.get("/").text
    for element in ("readApprovals", "readJobs", "readDenials"):
        assert 'id="%s"' % element in page


def test_the_reactor_cannot_reach_the_api(client: TestClient) -> None:
    """It draws; the console computes. Kept apart so a rendering bug cannot
    take the panel's data with it."""
    code = _console_code_for(client, "reactor.js")
    for forbidden in ("fetch(", "jarvisApi", "localStorage", "token"):
        assert forbidden not in code, f"reactor.js contains {forbidden}"


def test_the_reactor_stops_for_reduced_motion(client: TestClient) -> None:
    code = _console_code_for(client, "reactor.js")
    assert "prefers-reduced-motion" in code
    # …and draws a still frame rather than nothing: the readings must survive
    # the animation being switched off.
    assert "draw(0)" in code


# ── memory trust ─────────────────────────────────────────────────────────────


def test_the_console_cannot_turn_tainted_memory_into_trusted(
    client: TestClient
) -> None:
    """Three layers, and this asserts all of them.

    The panel offers no such control; MemoryService.update() drops a falsy
    `tainted` and logs it; and the API's update schema has no such field at
    all, so the request could not even be expressed. Taint is a fact about
    where a claim came from, and approving the claim does not change that.
    """
    # The panel reads taint and renders it; what it must never do is send it.
    code = _console_code(client)
    assert "memory.tainted" in code, "the panel should read taint"

    # It sends no request bodies at all for memory — only POSTs to /confirm and
    # /archive, which name the action rather than carrying fields. There is
    # therefore nothing to smuggle a taint change into, which is a stronger
    # statement than "it does not currently set that field".
    assert "body:" not in code, "the console builds a request body"
    assert re.search(r'method:\s*"PATCH"', code) is None

    # The request could not be expressed even if the panel tried.
    from jarvis.api.memory_routes import UpdateMemoryRequest

    assert "tainted" not in UpdateMemoryRequest.model_fields
    # …nor at creation: a memory the user typed is trusted by definition, and
    # a `source` field on the create request would let a caller *claim* WEB
    # provenance for something they wrote.
    from jarvis.api.memory_routes import CreateMemoryRequest

    assert "source" not in CreateMemoryRequest.model_fields


async def test_a_patch_attempting_to_clear_taint_leaves_it_set(
    client: TestClient, session, user
) -> None:
    """Driven through the real endpoint rather than asserted from the schema
    alone, because "the field is absent" and "the memory is still tainted"
    are different claims and only the second matters.

    The memory is made tainted through the service, since the API deliberately
    offers no way to create one — a caller who could claim WEB provenance for
    text they wrote would be forging the very thing taint records.
    """
    from jarvis.db.models import MemorySource
    from jarvis.memory.service import MemoryDraft, MemoryService

    outcome = await MemoryService(session).create(
        user.id,
        MemoryDraft(content="Something a web page said",
                    subject="provenance", source=MemorySource.WEB),
        actor="test",
    )
    await session.commit()
    memory_id = outcome.memory.id
    assert outcome.memory.tainted is True

    # Unknown fields are dropped or refused; either way the taint survives.
    client.patch("/api/memories/" + memory_id, json={"tainted": False})
    client.patch("/api/memories/" + memory_id,
                 json={"content": "Something ordinary", "tainted": False})

    fetched = client.get("/api/memories/" + memory_id)
    if fetched.status_code == 200:
        assert fetched.json()["tainted"] is True


def test_the_three_trust_states_are_distinguished(client: TestClient) -> None:
    """PROPOSED, TAINTED, TRUSTED. The panel's whole job."""
    code = _console_code(client)
    for state in ('"proposed"', '"tainted"', '"trusted"'):
        assert state in code, state

    css = client.get("/assets/app.css").text
    for rule in (".trust-proposed", ".trust-tainted", ".trust-trusted"):
        assert rule in css, rule


def test_trust_is_carried_by_position_as_well_as_colour(
    client: TestClient
) -> None:
    """A scanning eye reads position before it reads a word, and colour is
    never the only signal here. Each row carries a left edge *and* a printed
    tag."""
    code = _console_code(client)
    assert '"trust-tag"' in code
    css = client.get("/assets/app.css").text
    assert "border-left-color:var(--red)" in css


# ── approvals already waiting when the console connects ──────────────────────


def test_pending_approvals_are_loaded_on_connect(client: TestClient) -> None:
    """The stream carries what happens *next*.

    A console that only ever learned about approvals from `approval.required`
    showed "Nothing is waiting on you." to anyone who opened the page after
    JARVIS had asked, or who simply reloaded it — which is the one situation
    where a person is most likely to be looking for what they owe an answer
    to. Found by running the thing and reading the panel, not by a test, so
    this is the test.
    """
    code = _console_code(client)
    assert "refreshApprovals" in code

    # It must be wired to the connect handler, not merely defined. Pinned by
    # reading the handler body, because a defined-but-never-called function is
    # exactly the shape this bug had.
    handler = re.search(
        r'addEventListener\("jarvis:authenticated"[^{]*\{(.*?)\}\s*\)',
        code, flags=re.S,
    )
    assert handler is not None, "the authenticated handler moved"
    assert "refreshApprovals" in handler.group(1)

    # And it reads the same endpoint the Confirmations view does, so the two
    # cannot disagree about what is outstanding.
    assert '"/confirmations"' in code


async def test_the_console_reads_fields_the_confirmation_payload_actually_has(
    client: TestClient, core
) -> None:
    """The panel is only as good as the shape it assumes.

    Seeding from `/confirmations` means the console now reads `id`, `title`,
    `tool`, `impact` and `reason` off that payload. If any of them were
    renamed, the panel would still render — with blank rows — and nothing else
    in the suite would notice. Driven through the real endpoint rather than
    asserted against `to_dict`, since the endpoint is what the browser sees.
    """
    from jarvis.confirmations.service import ConfirmationRequest, ConfirmationService
    from jarvis.core import JarvisCore
    from jarvis.db.models import RiskLevel

    # The core's database, not the standalone `session` fixture: those are two
    # different in-memory SQLites, and a confirmation written to the other one
    # would leave this test asserting against an empty list forever.
    async with core.database.session_factory() as session:
        # …and the subject the API resolves, for the same reason: a
        # confirmation raised for somebody else would not appear either.
        owner = await JarvisCore.ensure_default_user(session)
        await ConfirmationService(session).request(
            ConfirmationRequest(
                user_id=owner.id,
                title="Delete the draft",
                body="This cannot be undone.",
                tool_name="browser_click",
                arguments={"page_id": "pg_1", "selector": "#delete"},
                risk_level=RiskLevel.HIGH,
                reversible=False,
                impact="destructive",
                reason="the action cannot be taken back",
            )
        )
        await session.commit()

    rows = client.get("/api/confirmations").json()["confirmations"]
    assert len(rows) == 1
    row = rows[0]
    for field in ("id", "title", "tool", "impact", "reason"):
        assert field in row, f"the console reads {field} and it is absent"
    assert row["impact"] == "destructive"
    assert row["title"] == "Delete the draft"


def test_seeding_approvals_does_not_carry_the_pending_arguments(
    client: TestClient
) -> None:
    """`/confirmations` returns `body` and `arguments`; the panel takes
    neither.

    A standing panel is not the dialog. The arguments *are* the thing being
    approved, and a value the user typed into a page — which is exactly what
    `redact_arguments` exists to keep out of the database — has no business
    sitting on screen until somebody gets round to deciding. The decision
    surface shows them; the waiting list shows what is waiting.
    """
    code = _console_code(client)
    seeder = re.search(r"async function refreshApprovals\(\)(.*?)\n  \}",
                       code, flags=re.S)
    assert seeder is not None, "refreshApprovals moved"
    for forbidden in ("row.arguments", "row.body", ".arguments", ".body"):
        assert forbidden not in seeder.group(1), forbidden


# ── the console does not claim features are missing that exist ───────────────


def test_the_console_holds_no_hardcoded_list_of_what_exists(
    client: TestClient
) -> None:
    """The defect this replaced, pinned so it cannot come back.

    app.js carried a hand-written "NOT IMPLEMENTED" list naming memory, file
    access, computer control, browser control and agents — months after each
    of them shipped. A UI telling somebody a feature is missing when they have
    it is worse than saying nothing: they stop looking for it.

    The lesson is not "correct the list". A second, hand-written statement of
    what exists will always drift from the thing it describes, so the fix is
    that the console has no opinion of its own and renders what the server
    computes.
    """
    code = _console_code_for(client, "app.js")

    assert "NOT IMPLEMENTED" not in code
    for stale in ("Phase 2)", "Phase 3)", "Phase 4)", "Phase 5)", "Phase 6)"):
        assert stale not in code, f"a hardcoded roadmap entry survived: {stale}"
    # …and it reads the derived block instead.
    assert "status.subsystems" in code


def test_the_empty_conversation_does_not_disclaim_working_features(
    client: TestClient
) -> None:
    """The same claim in the other place it was made — the empty chat state,
    which is the first sentence a new user reads."""
    page = client.get("/").text

    assert "not built yet" not in page
    assert "Phase 1:" not in page


async def test_every_subsystem_reports_available_or_says_why(client) -> None:
    """An entry marked unavailable with no reason is a dead end, and one marked
    available that is not is the overclaim this whole layer exists to avoid."""
    subsystems = client.get("/api/system/status").json()["subsystems"]

    assert subsystems, "nothing reported at all"
    for entry in subsystems:
        assert set(entry) == {"name", "state", "detail"}
        assert entry["state"] in {"ready", "unavailable", "unknown"}
        if entry["state"] != "ready":
            assert entry["detail"], f"{entry['name']} is {entry['state']} with no reason"


async def test_a_built_but_unavailable_subsystem_is_not_called_unbuilt(
    client, core
) -> None:
    """The distinction the old list could not express.

    Browser control is built; it is unavailable without Playwright's Chromium.
    "Unavailable — run playwright install chromium" is useful. "Not
    implemented (Phase 5)" is false, and sends the reader somewhere that does
    not exist.
    """
    entry = next(
        s for s in core.subsystems() if s["name"] == "Browser control"
    )

    if entry["state"] == "ready":
        pytest.skip("a browser is available here, so there is no reason to check")
    detail = entry["detail"]
    assert "not implemented" not in detail.lower()
    assert "Phase" not in detail


async def test_memory_is_available_even_when_search_is_only_lexical(core) -> None:
    """The caveat is about the *quality* of recall, not whether it works.

    Reporting memory as unavailable because embeddings are absent would be the
    same overclaim in the opposite direction — and would hide a subsystem the
    user can use today.
    """
    entry = next(s for s in core.subsystems() if s["name"] == "Memory")

    assert entry["state"] == "ready"
    if not core.embeddings.info.semantic:
        assert "lexical" in entry["detail"]


async def test_an_unprobed_browser_is_reported_as_unknown_not_unavailable(
    core,
) -> None:
    """The third state, and why it exists.

    Probing starts a Playwright driver process, so it stays lazy — paying for
    that on every start would be a real cost for a capability most turns never
    use, and there is a test asserting startup launches nothing. Given that, a
    panel reading "unavailable" for something nobody has looked at would be the
    same overclaim as the stale list, pointed the other way.

    I got this wrong first: I made startup probe eagerly so the panel would
    read cleanly, which broke the no-launch guarantee. The state is the fix,
    not the probe.
    """
    from jarvis.browser.capabilities import BrowserAvailability

    if core.browser.capabilities.state is not BrowserAvailability.UNPROBED:
        pytest.skip("something in this fixture already probed")

    entry = next(s for s in core.subsystems() if s["name"] == "Browser control")
    assert entry["state"] == "unknown"
    assert "Not checked yet" in entry["detail"]
