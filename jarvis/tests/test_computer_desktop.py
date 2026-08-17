"""Display-dependent tests: observation, input, the executor, and the audit.

These need a real X server. Where one is not available they **skip** rather
than being deleted or mocked — a mocked click proves the mock works, and the
whole point of Phase 3 is that the input path genuinely reaches an application.

A virtual display counts. Xvfb is a real X server, XTEST injects at the server,
and an application launched into it responds exactly as it would on a physical
desktop. What a virtual display cannot prove is that the *user's own* desktop
behaves the same, which is stated in the capability report rather than assumed
away here.
"""

from __future__ import annotations

import asyncio
import inspect
import shutil
from pathlib import Path

import pytest

from jarvis.computer.capabilities import detect, start_virtual_display
from jarvis.computer.control import EmergencyStop
from jarvis.computer.executor import ActionExecutor, ExecutionContext
from jarvis.computer.filesystem import FilesystemGuard, FilesystemPolicy
from jarvis.computer.observation import (
    ObservationOptions,
    ObservationProcessor,
    ScreenshotStore,
    changed_region,
    signature_delta,
    tile_signature,
)
from jarvis.computer.policy import ComputerPolicy, ComputerPolicyEngine
from jarvis.computer.terminal import TerminalExecutor
from jarvis.computer.types import (
    ActionKind,
    ActionOutcome,
    ComputerAction,
    ComputerMode,
    ComputerScope,
    VerificationOutcome,
)
from jarvis.confirmations.service import ConfirmationService
from jarvis.db.models import Capability, ComputerAudit, PermissionGrant, PermissionMode
from jarvis.errors import ConfirmationRequiredError

pytestmark = pytest.mark.skipif(
    not shutil.which("Xvfb"), reason="no X server available"
)

ALL_SCOPES = {
    ComputerScope.SCREEN, ComputerScope.WINDOW, ComputerScope.MOUSE,
    ComputerScope.KEYBOARD, ComputerScope.FILESYSTEM, ComputerScope.TERMINAL,
    ComputerScope.CLIPBOARD, ComputerScope.APPLICATION,
}


@pytest.fixture(scope="module")
def display():
    """One Xvfb for the module — starting a server per test is slow and adds
    nothing."""
    started = start_virtual_display(width=800, height=600, number=91)
    if not started:
        pytest.skip("could not start Xvfb")
    name, process = started
    yield name
    process.terminate()
    try:
        process.wait(timeout=5)
    except Exception:
        process.kill()


@pytest.fixture
def backend(display: str):
    from jarvis.computer.backends.x11 import X11Backend

    instance = X11Backend(display)
    yield instance
    instance.close()


@pytest.fixture
def processor(backend):
    return ObservationProcessor(backend, ScreenshotStore(ttl_seconds=60))


# ── observation ──────────────────────────────────────────────────────────────


def test_screen_size(backend) -> None:
    assert backend.screen_size() == (800, 600)


def test_capture_returns_a_real_png(backend) -> None:
    png = backend.capture()
    assert png.startswith(b"\x89PNG")
    assert len(png) > 100


def test_structured_observation_needs_no_image(backend) -> None:
    state = backend.observe()
    assert state.width == 800 and state.height == 600
    assert state.cursor is not None
    assert "800x600" in state.summarise()


def test_cursor_moves(backend) -> None:
    backend.move_mouse(123, 234)
    assert backend.cursor_position() == (123, 234)


def test_cursor_is_clamped_to_the_screen(backend) -> None:
    """Off-screen coordinates would otherwise be silently clamped by the
    server, making the audit log disagree with where the pointer went."""
    backend.move_mouse(99999, 99999)
    x, y = backend.cursor_position()
    assert x < 800 and y < 600


def test_unchanged_frames_are_detected(processor) -> None:
    processor.observe()
    second = processor.observe()
    assert second.state.unchanged is True
    assert second.image_base64 is None


def test_forced_image_survives_an_unchanged_frame(processor) -> None:
    processor.observe()
    forced = processor.observe(ObservationOptions(force_image=True))
    assert forced.image_base64 is not None


def test_downscaling_bounds_the_image(processor) -> None:
    observation = processor.observe(ObservationOptions(max_edge=200, force_image=True))
    assert max(observation.image_width, observation.image_height) <= 200
    assert observation.scale < 1.0
    assert any("scaled" in note for note in observation.notes)


def test_tile_signature_detects_a_local_change(backend) -> None:
    """The bug that motivated tiling: a global hash misses this entirely."""
    first = tile_signature(backend.capture())
    # Draw something small by moving a window-sized region: with no window
    # manager the simplest reliable change is the pointer, so compare against
    # a synthetic edit of the signature instead.
    mutated = list(first)
    mutated[len(mutated) // 2] = (mutated[len(mutated) // 2] + 120) % 256
    assert signature_delta(first, mutated) >= 100
    region = changed_region(first, mutated, 800, 600)
    assert region is not None


def test_screenshot_store_expires(backend) -> None:
    store = ScreenshotStore(ttl_seconds=0, max_items=5)
    item = store.put(backend.capture(), width=800, height=600)
    assert store.get(item.id) is None, "an expired screenshot must not be served"


def test_screenshot_store_is_bounded(backend) -> None:
    store = ScreenshotStore(ttl_seconds=60, max_items=3)
    png = backend.capture()
    for _ in range(6):
        store.put(png, width=800, height=600)
    assert len(store.list()) == 3


def test_screenshots_are_not_written_to_disk_by_default(backend, tmp_path) -> None:
    """§6: retention is opt-in."""
    store = ScreenshotStore(ttl_seconds=60)
    item = store.put(backend.capture(), width=800, height=600)
    with pytest.raises(ValueError):
        store.persist(item.id)
    assert not list(tmp_path.glob("*.png"))


# ── input ────────────────────────────────────────────────────────────────────


def test_typing_does_not_raise(backend) -> None:
    backend.type_text("hello world 123")


def test_unmappable_characters_do_not_corrupt_the_layout(backend) -> None:
    """A borrowed keycode must always be returned."""
    before = backend._display.get_keyboard_mapping(8, 240)
    backend.type_text("héllo ✓ ünïcode")
    after = backend._display.get_keyboard_mapping(8, 240)
    assert before == after


def test_unknown_key_is_a_clear_error(backend) -> None:
    from jarvis.computer.backends.base import BackendError

    with pytest.raises(BackendError):
        backend.press_key("not-a-real-key")


def test_clipboard_round_trip(backend) -> None:
    backend.write_clipboard("jarvis clipboard value")
    assert backend.read_clipboard() == "jarvis clipboard value"


def test_clipboard_replacement(backend) -> None:
    backend.write_clipboard("first")
    backend.write_clipboard("second")
    assert backend.read_clipboard() == "second"


# ── executor, end to end ─────────────────────────────────────────────────────


@pytest.fixture
async def executor(session, user, backend, processor, tmp_path):
    from jarvis.permissions.engine import seed_default_grants

    await seed_default_grants(session, user.id)
    for capability in (Capability.EXECUTE, Capability.WRITE, Capability.READ):
        session.add(
            PermissionGrant(
                user_id=user.id, capability=capability,
                resource_scope="computer:*", mode=PermissionMode.ALLOW,
            )
        )
    await session.flush()

    root = tmp_path / "work"
    root.mkdir()
    (root / "a.txt").write_text("hello", encoding="utf-8")

    return ActionExecutor(
        backend=backend,
        observation=processor,
        policy_engine=ComputerPolicyEngine(
            session,
            capabilities=detect(probe_display=backend._display_name),
            policy=ComputerPolicy(
                mode=ComputerMode.AUTONOMOUS,
                enabled_scopes=frozenset(ALL_SCOPES),
                auto_scopes=frozenset(ALL_SCOPES),
            ),
        ),
        emergency_stop=EmergencyStop(),
        filesystem=FilesystemGuard(
            FilesystemPolicy(
                allowed_paths=[root], can_read=True, can_write=True, can_delete=True
            )
        ),
        terminal=TerminalExecutor(working_directory=root, allowed_roots=[root]),
        confirmations=ConfirmationService(session),
    )


async def test_executor_runs_an_action_and_audits_it(executor, session, user) -> None:
    from sqlalchemy import select

    result = await executor.execute(
        ComputerAction(kind=ActionKind.GET_CURSOR, reason="check"),
        ExecutionContext(user_id=user.id, session=session),
    )
    assert result.ok

    rows = (await session.execute(select(ComputerAudit))).scalars().all()
    assert len(rows) == 1
    assert rows[0].kind == "get_cursor" and rows[0].outcome == "SUCCEEDED"


async def test_denied_actions_are_audited_too(executor, session, user) -> None:
    from sqlalchemy import select

    result = await executor.execute(
        ComputerAction(
            kind=ActionKind.EXECUTE_COMMAND, params={"command": "rm -rf /"},
            reason="bad",
        ),
        ExecutionContext(user_id=user.id, session=session),
    )
    assert result.outcome is ActionOutcome.DENIED

    row = (await session.execute(select(ComputerAudit))).scalars().first()
    assert row.outcome == "DENIED" and row.risk == "PROHIBITED"


async def test_typed_text_never_reaches_the_audit(executor, session, user) -> None:
    from sqlalchemy import select

    secret = "correct horse battery staple"
    await executor.execute(
        ComputerAction(
            kind=ActionKind.TYPE_TEXT, params={"text": secret}, reason="type"
        ),
        ExecutionContext(user_id=user.id, session=session),
    )
    row = (await session.execute(select(ComputerAudit))).scalars().first()
    assert secret not in str(row.params)
    assert secret not in (row.summary or "")
    assert "characters" in str(row.params)


async def test_emergency_stop_aborts_before_execution(executor, session, user) -> None:
    executor.emergency_stop.engage(reason="test")
    result = await executor.execute(
        ComputerAction(kind=ActionKind.MOVE_MOUSE, params={"x": 5, "y": 5},
                       reason="move"),
        ExecutionContext(user_id=user.id, session=session),
    )
    assert result.outcome is ActionOutcome.ABORTED


async def test_stop_beats_a_prior_approval(executor, session, user) -> None:
    """An approval obtained a minute ago must not run if the stop went on in
    between — which is why the check is last, not first."""
    action = ComputerAction(
        kind=ActionKind.DELETE_PATH, params={"path": "/nonexistent"}, reason="rm"
    )
    with pytest.raises(ConfirmationRequiredError) as caught:
        await executor.execute(
            action, ExecutionContext(user_id=user.id, session=session)
        )

    confirmations = ConfirmationService(session)
    await confirmations.decide(caught.value.confirmation_id, approved=True)
    executor.emergency_stop.engage(reason="changed my mind")

    result = await executor.execute(
        action, ExecutionContext(user_id=user.id, session=session)
    )
    assert result.outcome is ActionOutcome.ABORTED


async def test_high_risk_action_requires_confirmation(executor, session, user, tmp_path) -> None:
    with pytest.raises(ConfirmationRequiredError):
        await executor.execute(
            ComputerAction(
                kind=ActionKind.DELETE_PATH,
                params={"path": str(tmp_path / "work" / "a.txt")},
                reason="delete",
            ),
            ExecutionContext(user_id=user.id, session=session),
        )
    assert (tmp_path / "work" / "a.txt").exists(), "the file must survive"


async def test_approval_lets_the_action_through(executor, session, user, tmp_path) -> None:
    target = tmp_path / "work" / "a.txt"
    action = ComputerAction(
        kind=ActionKind.DELETE_PATH, params={"path": str(target)}, reason="delete"
    )
    with pytest.raises(ConfirmationRequiredError) as caught:
        await executor.execute(
            action, ExecutionContext(user_id=user.id, session=session)
        )

    await ConfirmationService(session).decide(
        caught.value.confirmation_id, approved=True
    )
    result = await executor.execute(
        action, ExecutionContext(user_id=user.id, session=session)
    )
    assert result.ok and not target.exists()


async def test_approval_is_single_use(executor, session, user, tmp_path) -> None:
    """A second identical action must ask again."""
    target = tmp_path / "work" / "a.txt"
    action = ComputerAction(
        kind=ActionKind.DELETE_PATH, params={"path": str(target)}, reason="delete"
    )
    with pytest.raises(ConfirmationRequiredError) as caught:
        await executor.execute(
            action, ExecutionContext(user_id=user.id, session=session)
        )
    await ConfirmationService(session).decide(
        caught.value.confirmation_id, approved=True
    )
    await executor.execute(action, ExecutionContext(user_id=user.id, session=session))

    target.write_text("recreated", encoding="utf-8")
    with pytest.raises(ConfirmationRequiredError):
        await executor.execute(
            action, ExecutionContext(user_id=user.id, session=session)
        )


async def test_verification_reports_no_change(executor, session, user) -> None:
    """Clicking empty space on a blank display changes nothing, and the
    executor must say so rather than reporting success."""
    context = ExecutionContext(user_id=user.id, session=session)
    click = ComputerAction(
        kind=ActionKind.CLICK, params={"x": 400, "y": 300}, reason="click nothing"
    )
    await executor.execute(click, context)
    result = await executor.execute(click, context)

    assert result.ok
    assert result.verification in {
        VerificationOutcome.CONTRADICTED, VerificationOutcome.INCONCLUSIVE
    }


async def _approve_and_run(executor, session, user, action):
    """Run an action that needs confirmation, approving it first.

    Used where the property under test lives in the *handler* rather than the
    policy — the confirmation is correct behaviour and would otherwise mask
    the check being asserted.
    """
    context = ExecutionContext(user_id=user.id, session=session)
    try:
        return await executor.execute(action, context)
    except ConfirmationRequiredError as caught:
        await ConfirmationService(session).decide(
            caught.confirmation_id, approved=True
        )
        return await executor.execute(action, context)


async def test_unapproved_application_is_refused(executor, session, user) -> None:
    """A path is not an application name. Accepting one would make
    open_application an arbitrary-execution primitive."""
    result = await _approve_and_run(
        executor, session, user,
        ComputerAction(
            kind=ActionKind.OPEN_APPLICATION,
            params={"application": "/bin/sh"}, reason="sneak",
        ),
    )
    assert not result.ok
    assert "approved application" in result.detail


async def test_launch_arguments_are_constrained(executor, tmp_path) -> None:
    """An allow-listed executable plus an arbitrary argv is still arbitrary
    behaviour — `chromium --load-extension=…` is not the program the user
    approved. Flags are refused, and a file:// URL clears the same filesystem
    boundary a read would."""
    from jarvis.computer.backends.base import BackendError
    from jarvis.computer.filesystem import PathNotAllowed

    for bad in ("--load-extension=/tmp/x", "-incognito"):
        with pytest.raises(BackendError, match="flag"):
            executor._check_launch_argument(bad)

    with pytest.raises(BackendError, match="scheme"):
        executor._check_launch_argument("javascript:alert(1)")

    with pytest.raises(PathNotAllowed):
        executor._check_launch_argument("file:///etc/hostname")

    inside = tmp_path / "work" / "a.txt"
    assert executor._check_launch_argument(f"file://{inside}").endswith("a.txt")
    assert executor._check_launch_argument("https://example.com") == (
        "https://example.com"
    )


def test_launched_applications_do_not_inherit_the_daemon_environment(
    monkeypatch,
) -> None:
    """§19. The terminal builds a scrubbed environment; the launcher used to
    pass os.environ straight through, which handed every API key in the daemon
    to any application it started."""
    from jarvis.computer.terminal import build_environment

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret")
    monkeypatch.setenv("JARVIS_API_TOKEN", "tok-secret")

    source = inspect.getsource(ActionExecutor._open_application)
    assert "dict(os.environ)" not in source
    assert "build_environment" in source
    assert "ANTHROPIC_API_KEY" not in build_environment()


async def test_cannot_close_an_application_jarvis_did_not_start(
    executor, session, user
) -> None:
    """The user's own editor with unsaved work is exactly what an agent must
    not be able to terminate."""
    result = await _approve_and_run(
        executor, session, user,
        ComputerAction(
            kind=ActionKind.CLOSE_APPLICATION,
            params={"application": "chromium"}, reason="close",
        ),
    )
    assert not result.ok
    assert "did not start" in result.detail
