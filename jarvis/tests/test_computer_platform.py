"""Does JARVIS tell the truth about what this machine can do? (§2, §3)

The audit's finding 5 was that computer actions are unreachable on Windows and
asked whether that is reported honestly. These tests answer it — and they are
careful about what they are worth.

## What these tests prove, and what they do not

They drive the real :func:`jarvis.computer.capabilities.detect` with
``platform.system`` reporting ``Windows``. That exercises the actual branch a
Windows machine takes and proves what JARVIS *reports* there: which flags are
set, which actions are refused, and with which words.

**They do not prove Windows runtime behaviour.** No Windows API is called, no
Windows process is started, and nothing here would catch a failure that only
appears on a real desktop. The suite runs on Linux. Anything that needs a real
Windows kernel is marked UNVERIFIED — WINDOWS RUNTIME in the hardening report,
and simulating a platform string is not the same as running on it.

The distinction matters because the failure mode being guarded against *is*
overclaiming, and a test that overclaimed its own reach would be an odd way to
guard against it.
"""

from __future__ import annotations

import platform

import pytest

from jarvis.computer.capabilities import CapabilityReport, detect
from jarvis.computer.types import ActionKind

DISPLAY_ACTIONS = [
    ActionKind.OBSERVE_SCREEN,
    ActionKind.SCREENSHOT,
    ActionKind.CLICK,
    ActionKind.DOUBLE_CLICK,
    ActionKind.RIGHT_CLICK,
    ActionKind.DRAG,
    ActionKind.SCROLL,
    ActionKind.MOVE_MOUSE,
    ActionKind.TYPE_TEXT,
    ActionKind.PRESS_KEY,
    ActionKind.HOTKEY,
    ActionKind.GET_WINDOWS,
    ActionKind.GET_ACTIVE_WINDOW,
    ActionKind.FOCUS_WINDOW,
    ActionKind.OPEN_APPLICATION,
    ActionKind.CLOSE_APPLICATION,
    ActionKind.READ_CLIPBOARD,
    ActionKind.WRITE_CLIPBOARD,
]


@pytest.fixture
def windows(monkeypatch: pytest.MonkeyPatch):
    """Detection as it runs on a Windows desktop, with no X server."""

    def _detect(**kwargs) -> CapabilityReport:
        monkeypatch.setattr(platform, "system", lambda: "Windows")
        monkeypatch.setattr(platform, "release", lambda: "11")
        monkeypatch.delenv("DISPLAY", raising=False)
        monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
        return detect(**kwargs)

    return _detect


# ── the report is honest ─────────────────────────────────────────────────────


def test_no_desktop_capability_is_claimed_on_windows(windows) -> None:
    report = windows()

    assert report.display is None
    assert report.display_kind == "none"
    assert report.has_physical_display is False
    assert report.has_pointer_input is False
    assert report.has_keyboard_input is False
    assert report.has_screenshot is False
    assert report.has_window_enumeration is False
    assert report.has_clipboard is False
    assert report.has_xtest is False


@pytest.mark.parametrize("kind", DISPLAY_ACTIONS, ids=lambda k: k.value)
def test_every_desktop_action_is_unavailable_on_windows(windows, kind) -> None:
    report = windows()
    assert report.supports(kind) is False
    assert report.reason_unavailable(kind)


def test_windows_is_not_described_as_headless(windows) -> None:
    """A machine with a monitor in front of it is not headless.

    Calling it that sends the user looking for a display problem they do not
    have, instead of understanding that the feature is not built.
    """
    notes = " ".join(windows().notes)
    assert "headless" not in notes.lower()
    assert "Windows" in notes
    assert "no Windows computer-control backend" in notes


def test_the_reason_does_not_offer_a_remedy_that_cannot_work(windows) -> None:
    """The refusal must not tell a Windows user to install Xvfb.

    Xvfb would install and start; it would also be irrelevant, because X11
    automation of an X server does not reach a single Windows application.
    Naming a fix that cannot work is a worse answer than naming none.
    """
    reason = windows().reason_unavailable(ActionKind.CLICK)
    assert "Xvfb" not in reason
    assert "headless" not in reason.lower()
    assert "missing feature, not a misconfiguration" in reason
    assert "no Windows computer-control backend" in reason


def test_an_x_server_on_windows_is_not_reported_as_the_desktop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The overclaim this guards against is a realistic setup, not a contrived one.

    VcXsrv, X410 and WSLg all set DISPLAY on Windows. Probing it would succeed
    — a genuine X server, with XTEST, a width and a height — and JARVIS would
    report a physical display and working pointer input on a machine where it
    cannot click a single Windows window. The automation would be real and
    entirely beside the point, which is the most convincing kind of false
    capability.
    """
    monkeypatch.setattr(platform, "system", lambda: "Windows")
    monkeypatch.setenv("DISPLAY", "localhost:0.0")

    report = detect()

    assert report.display is None
    assert report.has_physical_display is False
    assert report.has_pointer_input is False
    assert report.supports(ActionKind.CLICK) is False
    notes = " ".join(report.notes)
    assert "localhost:0.0" in notes
    assert "would not touch" in notes


def test_macos_gets_the_same_treatment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Not Windows-specific: any platform without a backend must say so."""
    monkeypatch.setattr(platform, "system", lambda: "Darwin")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)

    report = detect()
    assert report.supports(ActionKind.CLICK) is False
    assert "Darwin" in report.reason_unavailable(ActionKind.CLICK)


# ── what still works ─────────────────────────────────────────────────────────


def test_the_non_desktop_subsystems_are_still_available(windows) -> None:
    """The refusal must be narrow.

    Obsidian, the knowledge base, tasks and the terminal have nothing to do
    with a display. Reporting them as unavailable because there is no desktop
    backend would be the opposite error — disclaiming a working capability.
    """
    report = windows()
    assert report.has_filesystem is True
    assert report.has_terminal is True
    assert report.supports(ActionKind.EXECUTE_COMMAND) is True


# ── the report the API and the model see ─────────────────────────────────────


def test_the_serialised_report_marks_every_action_with_its_reason(windows) -> None:
    """``to_dict`` is what the UI renders and what status tools summarise.

    An action listed as available with no reason, or unavailable with no
    reason, is how a dead control reaches the screen.
    """
    payload = windows().to_dict()
    actions = payload["actions"]

    assert set(actions) == {kind.value for kind in ActionKind}
    for kind in DISPLAY_ACTIONS:
        entry = actions[kind.value]
        assert entry["available"] is False
        assert entry["reason"]

    for entry in actions.values():
        assert entry["available"] is (entry["reason"] is None)


def test_the_serialised_report_does_not_advertise_a_display(windows) -> None:
    payload = windows().to_dict()
    assert payload["display"]["value"] is None
    assert payload["display"]["physical"] is False
    assert payload["input"] == {"pointer": False, "keyboard": False, "xtest": False}
    assert payload["observation"]["screenshot"] is False


def test_no_gui_application_is_advertised_as_launchable(windows) -> None:
    """The known-application list is discovered by probing PATH for Linux
    binaries. On Windows those names are not what a GUI application is called,
    and a list of applications JARVIS cannot launch is an invitation to try."""
    report = windows()
    assert report.supports(ActionKind.OPEN_APPLICATION) is False


# ── the permission layer refuses rather than pretends ────────────────────────


async def test_an_unavailable_action_is_denied_not_asked(core, windows) -> None:
    """No confirmation makes a missing backend appear.

    Asking the user to approve a click that cannot happen would be the fake
    success this whole layer exists to prevent — they would approve it, and
    then be told it failed. DENY is the only honest answer, and the reason the
    user reads is the capability report's own words.
    """
    from jarvis.computer.policy import (
        PHASE3_FORBIDDEN_SCOPES,
        ComputerPolicy,
        ComputerPolicyEngine,
    )
    from jarvis.computer.types import ComputerAction, ComputerMode, ComputerScope
    from jarvis.core import JarvisCore
    from jarvis.db.models import PermissionMode

    report = windows()

    async with core.database.session_factory() as session:
        user = await JarvisCore.ensure_default_user(session)
        engine = ComputerPolicyEngine(
            session,
            capabilities=report,
            # Deliberately the most permissive configuration that exists:
            # every scope enabled, every scope automatic, the highest mode.
            # If anything could turn an unavailable action into an ASK or an
            # ALLOW, this is the setting that would do it.
            policy=ComputerPolicy(
                mode=ComputerMode.AUTONOMOUS,
                enabled_scopes=frozenset(ComputerScope) - PHASE3_FORBIDDEN_SCOPES,
                auto_scopes=frozenset(ComputerScope) - PHASE3_FORBIDDEN_SCOPES,
            ),
        )
        decision = await engine.evaluate(
            ComputerAction(
                kind=ActionKind.CLICK,
                params={"x": 10, "y": 10},
                reason="Click the Save button",
            ),
            user_id=user.id,
        )

    assert decision.mode is PermissionMode.DENY
    assert "capability_unavailable" in decision.applied_rules
    assert "no Windows computer-control backend" in decision.reason
    assert "Xvfb" not in decision.reason


# ── what the model is actually told ──────────────────────────────────────────


def test_desktop_tools_are_not_offered_to_the_model_on_windows(
    core, windows
) -> None:
    """The defect this closes was an approve-then-fail.

    The capability check lives in ``ComputerPolicyEngine``, inside the handler.
    The executor's permission decision runs *before* the handler and returns
    ASK for an ungranted EXECUTE — so on a machine with no display backend the
    user was asked to approve a click, approved it, and only then learned that
    clicking is impossible here. An approval collected for an action that was
    never going to happen teaches the user their approvals are ceremonial.

    PlanStage now withholds the tool instead. Nothing is weakened: the policy
    engine still denies the action for every other caller, which
    ``test_an_unavailable_action_is_denied_not_asked`` proves separately.
    """
    from jarvis.orchestrator.stages import PlanStage

    core.computer.capabilities = windows()
    plan = PlanStage(core.router, core.tools, core.computer)
    offered = {t.name for t in core.tools.enabled() if plan._runnable_here(t)}

    for withheld in ("click", "scroll", "type_text", "press_key",
                     "observe_screen", "list_windows", "open_application"):
        assert withheld not in offered

    # Withheld narrowly: filesystem and terminal actions do not need a display,
    # and status must never be withheld or "why can't you click?" has no answer.
    for kept in ("computer_status", "read_file", "list_directory", "run_command",
                 "search_obsidian", "remember", "create_task"):
        assert kept in offered


def test_every_computer_tool_is_classified(core) -> None:
    """A new computer tool must be added to the availability map.

    Without this, adding a tool and forgetting the map silently reintroduces
    the approve-then-fail path for that one tool — the quietest possible
    regression.
    """
    from jarvis.tools.builtin.computer_tools import TOOL_ACTIONS

    names = {t.name for t in core.tools.all() if t.category == "computer"}
    # computer_status is deliberately unmapped: it is the tool that explains
    # the others' absence, so it is never withheld.
    assert names - set(TOOL_ACTIONS) == {"computer_status"}


async def test_the_model_is_told_why_a_click_is_impossible(core, windows) -> None:
    """Withholding the tool is not the only guard.

    A tool call can still arrive — a replayed confirmation, a direct API
    caller, a model that names a tool it was not offered. Called anyway, the
    result must be an error carrying the platform's reason: not a success, not
    a bare "failed", and not a suggestion to install something that cannot
    help.
    """
    from jarvis.core import JarvisCore
    from jarvis.db.models import Capability, PermissionGrant, PermissionMode
    from jarvis.tools.base import ToolContext
    from jarvis.tools.executor import ToolCall

    core.computer.capabilities = windows()

    async with core.database.session_factory() as session:
        user = await JarvisCore.ensure_default_user(session)
        # An explicit ALLOW so the executor's own decision does not stop the
        # call at a confirmation. The point here is what happens *after* the
        # permission layer has said yes: the most permissive setting possible
        # must still not produce a click.
        session.add(
            PermissionGrant(
                user_id=user.id,
                capability=Capability.EXECUTE,
                resource_scope="tool:click",
                mode=PermissionMode.ALLOW,
                note="Test grant.",
            )
        )
        await session.commit()

        orchestrator = core.orchestrator
        executor = orchestrator._make_executor(session)
        outcome = await executor.execute(
            ToolCall(
                id="tu_1",
                name="click",
                arguments={"x": 100, "y": 200, "target": "the Save button"},
            ),
            ToolContext(
                user_id=user.id,
                session=session,
                request_id="req_win",
                extras={
                    "embeddings": core.embeddings,
                    "project_id": None,
                    "computer": core.computer,
                    "activity": orchestrator._activity(session),
                },
            ),
        )
        await session.commit()

    assert outcome.result.is_error is True
    assert "no Windows computer-control backend" in outcome.result.content
    assert "Xvfb" not in outcome.result.content


async def test_computer_status_does_not_claim_a_screen_on_windows(
    core, windows
) -> None:
    """The tool the prompt tells the model to consult must not overclaim.

    The capabilities block instructs the model never to say it can see the
    screen before a tool has told it so. That instruction is worthless if the
    tool says yes.
    """
    from jarvis.core import JarvisCore
    from jarvis.tools.base import ToolContext
    from jarvis.tools.executor import ToolCall

    core.computer.capabilities = windows()

    async with core.database.session_factory() as session:
        user = await JarvisCore.ensure_default_user(session)
        await session.commit()

        orchestrator = core.orchestrator
        executor = orchestrator._make_executor(session)
        outcome = await executor.execute(
            ToolCall(id="tu_1", name="computer_status", arguments={}),
            ToolContext(
                user_id=user.id,
                session=session,
                request_id="req_win",
                extras={
                    "embeddings": core.embeddings,
                    "project_id": None,
                    "computer": core.computer,
                    "activity": orchestrator._activity(session),
                },
            ),
        )
        await session.commit()

    content = outcome.result.content
    assert outcome.result.is_error is False
    assert "Windows" in content
    assert "headless" not in content.lower()


def test_the_prompt_does_not_disclaim_capabilities_that_exist() -> None:
    """The opposite failure, and the one that was actually shipping.

    The capabilities block is static text in every system prompt. It still
    said Obsidian was not connected and computer control was not built, long
    after both shipped — instructing the model to deny working features. A
    prompt that lies in the safe direction is still a prompt that lies.
    """
    from jarvis.prompts import identity

    text = identity.CAPABILITIES_IMPLEMENTED
    assert "There is no Obsidian connection" not in text
    assert "Not built yet (Phase 3 onward)" not in text
    assert "obsidian_status" in text
    assert "computer_status" in text


# ── the reorder must not have changed any outcome ────────────────────────────


@pytest.mark.parametrize(
    "kind,available",
    [(ActionKind.CLICK, False), (ActionKind.EXECUTE_COMMAND, True)],
    ids=["unavailable-action", "available-action"],
)
async def test_lockdown_still_wins_over_the_capability_check(
    core, windows, kind, available
) -> None:
    """Moving the capability check earlier must not let it outrank a stop.

    LOCKDOWN and the phase-forbidden scopes are checked before it and stay
    that way: the operator saying "nothing at all" is a stronger statement
    than the machine saying "not this". The parametrisation covers both sides
    of the reorder — an action the machine cannot do, and one it can.
    """
    from jarvis.computer.policy import ComputerPolicy, ComputerPolicyEngine
    from jarvis.computer.types import ComputerAction, ComputerMode
    from jarvis.core import JarvisCore
    from jarvis.db.models import PermissionMode

    report = windows()
    assert report.supports(kind) is available

    async with core.database.session_factory() as session:
        user = await JarvisCore.ensure_default_user(session)
        decision = await ComputerPolicyEngine(
            session,
            capabilities=report,
            policy=ComputerPolicy(mode=ComputerMode.LOCKDOWN),
        ).evaluate(
            ComputerAction(
                kind=kind,
                params={"command": "echo hello", "x": 1, "y": 1},
                reason="anything",
            ),
            user_id=user.id,
        )

    assert decision.mode is PermissionMode.DENY
    assert "LOCKDOWN" in decision.reason


def test_no_action_kind_maps_to_a_forbidden_scope() -> None:
    """Why there is no forbidden-scope ordering test above it.

    ``PHASE3_FORBIDDEN_SCOPES`` covers FINANCIAL, COMMUNICATION and
    SYSTEM_SETTINGS, and no ``ActionKind`` is mapped to any of them — the rule
    is unreachable by construction rather than by configuration, which is the
    stronger guarantee. It is kept as the check that catches the first action
    kind added into one of those scopes, and this test records that it is
    currently dead code on purpose.

    It also means the capability check's move ahead of the *scope-enabled*
    rule cannot have disturbed it: the forbidden-scope rule still runs first
    and still matches nothing.
    """
    from jarvis.computer.policy import PHASE3_FORBIDDEN_SCOPES
    from jarvis.computer.types import ACTION_SCOPE

    assert not {ACTION_SCOPE[kind] for kind in ActionKind} & PHASE3_FORBIDDEN_SCOPES


# ── the Windows backend (Phase D, item 9) ────────────────────────────────────
#
# UNVERIFIED — WINDOWS RUNTIME. None of this executes an input call; those need
# a Windows session and there isn't one. What is tested here is everything that
# does not: the platform guard, the key vocabulary, the escaping, the element
# registry's staleness rule, and the refusal messages. Those are the parts
# where a mistake is silent — a key name that becomes a key *sequence* types
# something nobody asked for and reports success.


def test_the_windows_backend_refuses_to_run_anywhere_else() -> None:
    """Constructing it off-Windows must fail loudly rather than half-work."""
    from jarvis.computer.backends.base import BackendUnavailable
    from jarvis.computer.backends.windows import WindowsBackend

    if platform.system() == "Windows":  # pragma: no cover - not this machine
        pytest.skip("this assertion is about the non-Windows case")
    with pytest.raises(BackendUnavailable) as caught:
        WindowsBackend()
    assert "only runs on Windows" in str(caught.value)


def test_a_missing_dependency_names_the_install_command() -> None:
    """"Unavailable" without a remedy is a dead end."""
    from jarvis.computer.backends.base import BackendUnavailable
    from jarvis.computer.backends.windows import _require

    with pytest.raises(BackendUnavailable) as caught:
        _require("a_module_that_does_not_exist")
    assert "pip install pywinauto" in str(caught.value)


@pytest.mark.parametrize(
    "text,expected",
    [
        ("hello", "hello"),
        ("a+b", "a{+}b"),
        ("100%", "100{%}"),
        ("^caret", "{^}caret"),
        ("{DEL}", "{{}DEL{}}"),
        ("(paren)", "{(}paren{)}"),
    ],
)
def test_typed_text_is_escaped_not_interpreted(text: str, expected: str) -> None:
    """``send_keys`` reads ``^%+{}()~`` as syntax.

    Without escaping, a password containing ``+`` presses Shift and a document
    containing ``{DEL}`` deletes something. Both would be reported as a
    successful "type".
    """
    from jarvis.computer.backends.windows import WindowsBackend

    assert WindowsBackend._escape(text) == expected


def test_an_unknown_key_is_refused_rather_than_forwarded() -> None:
    """A key name must not be able to become a key sequence.

    The vocabulary is explicit for this reason: forwarding an arbitrary string
    to ``send_keys`` would let ``press_key("^{ESC}")`` open the Start menu, and
    a model that can name a key could name a macro.
    """
    from jarvis.computer.backends.base import BackendError
    from jarvis.computer.backends.windows import _KEYS

    assert "enter" in _KEYS and _KEYS["enter"] == "{ENTER}"
    # Nothing in the table maps to a bare modifier or a multi-key sequence.
    for token in _KEYS.values():
        assert token.startswith("{") and token.endswith("}")
        assert token.count("{") == 1
    assert BackendError is not None


def test_only_known_modifiers_compose_a_hotkey() -> None:
    from jarvis.computer.backends.windows import _MODIFIERS

    assert set(_MODIFIERS) >= {"ctrl", "alt", "shift"}
    for name, token in _MODIFIERS.items():
        assert token, name


def test_element_ids_are_issued_not_guessed() -> None:
    """The browser's argument, applied to the desktop.

    A coordinate the model computed from a screenshot clicks whatever happens
    to be underneath and reports success. An id JARVIS issued resolves to a
    named control or to nothing.
    """
    from jarvis.computer.backends.base import BackendError
    from jarvis.computer.backends.windows import UiElement, WindowsBackend

    backend = WindowsBackend.__new__(WindowsBackend)
    backend._elements = {}
    backend._generation = 0
    backend._elements_window = None

    with pytest.raises(BackendError) as caught:
        backend.resolve("ui_invented")
    assert "never issued" in str(caught.value)

    element = UiElement(element_id="ui_1", name="Transfer funds",
                        control_type="Button", rect=(0, 0, 100, 40))
    backend._elements["ui_1"] = element
    assert backend.resolve("ui_1") is element
    assert element.centre == (50, 20)


def test_a_disabled_control_is_refused_rather_than_clicked() -> None:
    """Clicking a greyed-out button succeeds and does nothing, which is worse
    than failing: the model believes the action happened."""
    from jarvis.computer.backends.base import BackendError
    from jarvis.computer.backends.windows import UiElement, WindowsBackend

    backend = WindowsBackend.__new__(WindowsBackend)
    backend._elements = {
        "ui_1": UiElement(element_id="ui_1", name="Send", control_type="Button",
                          rect=(0, 0, 10, 10), enabled=False)
    }
    with pytest.raises(BackendError) as caught:
        backend.click_element("ui_1")
    assert "greyed out" in caught.value.user_message


def test_only_operable_controls_are_offered() -> None:
    """Static text is content, not something to press."""
    from jarvis.computer.backends.windows import INTERACTIVE_TYPES

    assert "Button" in INTERACTIVE_TYPES
    assert "Edit" in INTERACTIVE_TYPES
    assert "Text" not in INTERACTIVE_TYPES
    assert "Image" not in INTERACTIVE_TYPES


def test_the_service_picks_the_windows_backend_on_windows(monkeypatch) -> None:
    """Pinned by source, because the branch cannot run here.

    The display probe reports "no desktop" on Windows — there is no X server
    and never will be — so without this branch a Windows machine with a real
    desktop gets UnavailableBackend.
    """
    import inspect

    from jarvis.computer.service import ComputerService

    source = inspect.getsource(ComputerService.start)
    assert 'platform.system() == "Windows"' in source
    assert "WindowsBackend" in source
    # …and the X11 branch is now the *else*, so neither shadows the other.
    assert "elif self.capabilities.display:" in source
