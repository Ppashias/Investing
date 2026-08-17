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
