"""The action executor — the only path from an action to the machine.

This is Phase 3's equivalent of :class:`~jarvis.tools.executor.ToolExecutor`,
and it exists for the same reason: §13 says no tool may bypass the permission
system, and the only way to *know* that is for there to be one function that
touches the backend. Everything above — tools, the agent loop, the API — builds
a :class:`ComputerAction` and hands it here.

Every execution runs the same seven steps, in this order:

1. **Availability.** Can this machine do it at all?
2. **Policy.** :class:`ComputerPolicyEngine` — mode, scope, risk, grants.
3. **Confirmation.** ``ASK`` suspends and persists, reusing Phase 1's
   fingerprint-bound, single-use, time-bounded confirmations.
4. **Emergency stop.** Checked *last*, immediately before the action runs, so
   an approval granted a minute ago still does not execute if the stop went on
   in between (§27).
5. **Observe before.** Captured for verification when the action is visual.
6. **Execute**, under a timeout (§28).
7. **Observe after and verify** (§9), then write the audit row.

The audit row is written on **every** path, including denied, aborted and
failed. A log that only records successes answers the wrong question.
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from jarvis.computer.backends.base import BackendError, BackendUnavailable, DesktopBackend
from jarvis.computer.control import EmergencyStop, EmergencyStopError
from jarvis.computer.filesystem import FilesystemGuard, PathNotAllowed
from jarvis.computer.observation import ObservationOptions, ObservationProcessor
from jarvis.computer.policy import ComputerPolicyEngine, PolicyDecision
from jarvis.computer.terminal import CommandRefused, TerminalExecutor
from jarvis.computer.types import (
    ActionKind,
    ActionOutcome,
    ActionResult,
    ActionRisk,
    ComputerAction,
    VerificationOutcome,
)
from jarvis.db.models import ComputerAudit, PermissionMode
from jarvis.errors import ConfirmationRequiredError
from jarvis.logging import get_logger, scrub_text, timed

log = get_logger(__name__)

#: Per-action timeouts (§28). Generous enough for a slow application, short
#: enough that a hang is noticed rather than waited out.
ACTION_TIMEOUTS: dict[ActionKind, float] = {
    ActionKind.OPEN_APPLICATION: 30.0,
    ActionKind.EXECUTE_COMMAND: 60.0,
    ActionKind.OBSERVE_SCREEN: 15.0,
    ActionKind.SCREENSHOT: 15.0,
    ActionKind.TYPE_TEXT: 30.0,
    ActionKind.DRAG: 15.0,
}
DEFAULT_ACTION_TIMEOUT = 10.0

#: A URI scheme at the start of an argument. Two characters minimum, so a
#: Windows drive letter is not mistaken for one.
_URI_SCHEME = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]+:")

#: Parameter names whose *values* never reach the audit log or a log line.
_REDACT_PARAMS = frozenset({"text", "content", "value"})

#: Actions whose effect should be visible on screen, so a before/after
#: observation is worth the ~100ms it costs.
_VISUAL_ACTIONS = frozenset(
    {
        ActionKind.CLICK, ActionKind.DOUBLE_CLICK, ActionKind.RIGHT_CLICK,
        ActionKind.DRAG, ActionKind.SCROLL, ActionKind.TYPE_TEXT,
        ActionKind.PRESS_KEY, ActionKind.HOTKEY, ActionKind.OPEN_APPLICATION,
        ActionKind.FOCUS_WINDOW, ActionKind.CLOSE_APPLICATION,
    }
)


@dataclass(slots=True)
class ExecutionContext:
    user_id: str
    session: AsyncSession
    request_id: str | None = None
    task_id: str | None = None
    sequence: int = 0
    actor: str = "agent"


class ActionExecutor:
    def __init__(
        self,
        *,
        backend: DesktopBackend,
        observation: ObservationProcessor,
        policy_engine: ComputerPolicyEngine,
        emergency_stop: EmergencyStop,
        filesystem: FilesystemGuard,
        terminal: TerminalExecutor,
        applications: dict[str, str] | None = None,
        confirmations: Any = None,
        activity: Any = None,
    ) -> None:
        self.backend = backend
        self.observation = observation
        self.policy_engine = policy_engine
        self.emergency_stop = emergency_stop
        self.filesystem = filesystem
        self.terminal = terminal
        self.applications = applications or {}
        self.confirmations = confirmations
        self.activity = activity
        #: pid -> application name, for close_application. Only processes
        #: JARVIS started: it may not close applications it did not open.
        self._launched: dict[str, Any] = {}

    async def execute(
        self, action: ComputerAction, ctx: ExecutionContext
    ) -> ActionResult:
        """The chokepoint. Returns a result; raises only for confirmation."""
        with timed() as clock:
            decision = await self.policy_engine.evaluate(action, user_id=ctx.user_id)

            if decision.denied:
                return await self._finish(
                    action, ctx, decision,
                    ActionOutcome.UNAVAILABLE
                    if decision.unavailable_reason
                    else ActionOutcome.DENIED,
                    detail=decision.reason,
                    duration_ms=clock.elapsed_ms,
                )

            if decision.needs_confirmation:
                confirmation_id = await self._request_confirmation(action, ctx, decision)
                if confirmation_id is not None:
                    await self._finish(
                        action, ctx, decision, ActionOutcome.AWAITING_CONFIRMATION,
                        detail=decision.reason, duration_ms=clock.elapsed_ms,
                        confirmation_id=confirmation_id,
                    )
                    raise ConfirmationRequiredError(
                        f"Confirmation required for {action.kind.value}",
                        confirmation_id=confirmation_id,
                        user_message=f"I need your approval: {action.describe()}",
                    )

            # Checked here, not earlier. An approval obtained a minute ago must
            # not execute if the stop went on in between (§27).
            try:
                self.emergency_stop.check()
            except EmergencyStopError as exc:
                return await self._finish(
                    action, ctx, decision, ActionOutcome.ABORTED,
                    detail=exc.user_message, duration_ms=clock.elapsed_ms,
                )

            before = self._observe_quietly() if action.kind in _VISUAL_ACTIONS else None

            try:
                data, detail = await asyncio.wait_for(
                    self._perform(action, ctx),
                    timeout=ACTION_TIMEOUTS.get(action.kind, DEFAULT_ACTION_TIMEOUT),
                )
            except asyncio.TimeoutError:
                return await self._finish(
                    action, ctx, decision, ActionOutcome.TIMED_OUT,
                    detail=f"No response within "
                           f"{ACTION_TIMEOUTS.get(action.kind, DEFAULT_ACTION_TIMEOUT)}s.",
                    duration_ms=clock.elapsed_ms,
                )
            except BackendUnavailable as exc:
                return await self._finish(
                    action, ctx, decision, ActionOutcome.UNAVAILABLE,
                    detail=exc.user_message, duration_ms=clock.elapsed_ms,
                )
            except (BackendError, PathNotAllowed, CommandRefused) as exc:
                return await self._finish(
                    action, ctx, decision, ActionOutcome.FAILED,
                    detail=exc.user_message, error=str(exc),
                    duration_ms=clock.elapsed_ms,
                )
            except Exception as exc:  # pragma: no cover - defensive
                log.exception("computer_action_crashed", kind=action.kind.value)
                return await self._finish(
                    action, ctx, decision, ActionOutcome.FAILED,
                    detail="The action failed unexpectedly.",
                    error=scrub_text(str(exc)), duration_ms=clock.elapsed_ms,
                )

            verification, verification_detail, after = self._verify(action, before)

            return await self._finish(
                action, ctx, decision, ActionOutcome.SUCCEEDED,
                detail=detail, data=data, duration_ms=clock.elapsed_ms,
                verification=verification, verification_detail=verification_detail,
                observation_before=before.state.screenshot_id if before else None,
                observation_after=after,
            )

    # ── the actual work ──────────────────────────────────────────────────────

    async def _perform(
        self, action: ComputerAction, ctx: ExecutionContext
    ) -> tuple[dict[str, Any], str]:
        params = action.params
        kind = action.kind

        # Observation
        if kind in {ActionKind.OBSERVE_SCREEN, ActionKind.SCREENSHOT}:
            observation = await asyncio.to_thread(
                self.observation.observe,
                ObservationOptions(
                    include_image=params.get("include_image", True),
                    window_id=params.get("window_id"),
                    max_edge=int(params.get("max_edge", 0)) or 1024,
                    # A screenshot is always an explicit request for pixels.
                    # An observation may also force them — the Computer
                    # panel's "Observe now" button means "show me the screen",
                    # and answering "it looks the same as last time" is not
                    # what the user pressed the button for.
                    force_image=(
                        kind is ActionKind.SCREENSHOT
                        or bool(params.get("force_image"))
                    ),
                ),
            )
            return observation.to_dict(include_image=True), observation.summarise()

        if kind is ActionKind.GET_WINDOWS:
            windows = await asyncio.to_thread(self.backend.windows)
            return (
                {"windows": [w.to_dict() for w in windows]},
                f"{len(windows)} window(s) open.",
            )

        if kind is ActionKind.GET_ACTIVE_WINDOW:
            window = await asyncio.to_thread(self.backend.active_window)
            return (
                {"window": window.to_dict() if window else None},
                f"Active window: {window.title!r}" if window else "No active window.",
            )

        if kind is ActionKind.GET_CURSOR:
            x, y = await asyncio.to_thread(self.backend.cursor_position)
            return {"x": x, "y": y}, f"Cursor at {x},{y}."

        # Mouse
        if kind is ActionKind.MOVE_MOUSE:
            x, y = int(params["x"]), int(params["y"])
            await asyncio.to_thread(self.backend.move_mouse, x, y)
            return {"x": x, "y": y}, f"Moved the pointer to {x},{y}."

        if kind in {ActionKind.CLICK, ActionKind.DOUBLE_CLICK, ActionKind.RIGHT_CLICK}:
            x, y = int(params["x"]), int(params["y"])
            button = 3 if kind is ActionKind.RIGHT_CLICK else 1
            count = 2 if kind is ActionKind.DOUBLE_CLICK else 1
            await asyncio.to_thread(
                self.backend.click, x, y, button=button, count=count
            )
            return {"x": x, "y": y}, f"{kind.value.replace('_', ' ')} at {x},{y}."

        if kind is ActionKind.DRAG:
            await asyncio.to_thread(
                self.backend.drag,
                int(params["from_x"]), int(params["from_y"]),
                int(params["to_x"]), int(params["to_y"]),
            )
            return {}, "Dragged."

        if kind is ActionKind.SCROLL:
            direction = str(params.get("direction", "down"))
            amount = int(params.get("amount", 3))
            await asyncio.to_thread(
                self.backend.scroll, direction, amount,
                x=params.get("x"), y=params.get("y"),
            )
            return {}, f"Scrolled {direction} {amount}."

        # Keyboard
        if kind is ActionKind.TYPE_TEXT:
            text = str(params["text"])
            await asyncio.to_thread(self.backend.type_text, text)
            # Length, never the text: this string reaches the model and logs.
            return {"characters": len(text)}, f"Typed {len(text)} character(s)."

        if kind is ActionKind.PRESS_KEY:
            key = str(params["key"])
            await asyncio.to_thread(self.backend.press_key, key)
            return {"key": key}, f"Pressed {key}."

        if kind is ActionKind.HOTKEY:
            keys = [str(k) for k in params["keys"]]
            await asyncio.to_thread(self.backend.hotkey, keys)
            return {"keys": keys}, f"Pressed {'+'.join(keys)}."

        # Windows and applications
        if kind is ActionKind.FOCUS_WINDOW:
            window_id = str(params["window_id"])
            await asyncio.to_thread(self.backend.focus_window, window_id)
            return {"window_id": window_id}, f"Focused {window_id}."

        if kind is ActionKind.OPEN_APPLICATION:
            return await self._open_application(str(params["application"]),
                                                params.get("arguments") or [])

        if kind is ActionKind.CLOSE_APPLICATION:
            return await self._close_application(str(params["application"]))

        # Clipboard
        if kind is ActionKind.READ_CLIPBOARD:
            text = await asyncio.to_thread(self.backend.read_clipboard)
            # §20: the clipboard commonly holds a password. The content is
            # returned because the caller asked for it and the read was
            # confirmed, but it is scrubbed of credential shapes on the way.
            cleaned = scrub_text(text)
            return (
                {"text": cleaned, "characters": len(text)},
                f"Clipboard holds {len(text)} character(s).",
            )

        if kind is ActionKind.WRITE_CLIPBOARD:
            text = str(params["text"])
            await asyncio.to_thread(self.backend.write_clipboard, text)
            return {"characters": len(text)}, f"Copied {len(text)} character(s)."

        # Filesystem
        if kind is ActionKind.READ_FILE:
            content, meta = await asyncio.to_thread(
                self.filesystem.read_text, params["path"]
            )
            return (
                {**meta, "content": content},
                f"Read {meta['bytes']} bytes from {meta['path']}.",
            )

        if kind is ActionKind.WRITE_FILE:
            meta = await asyncio.to_thread(
                self.filesystem.write_text, params["path"], str(params["content"]),
                overwrite=bool(params.get("overwrite")),
            )
            verb = "Overwrote" if meta["overwrote"] else "Created"
            return meta, f"{verb} {meta['path']} ({meta['bytes']} bytes)."

        if kind is ActionKind.LIST_DIRECTORY:
            meta = await asyncio.to_thread(
                self.filesystem.list_directory, params["path"]
            )
            return meta, f"{len(meta['entries'])} entries in {meta['path']}."

        if kind is ActionKind.CREATE_DIRECTORY:
            meta = await asyncio.to_thread(
                self.filesystem.create_directory, params["path"]
            )
            return meta, f"Directory {meta['path']} ready."

        if kind is ActionKind.DELETE_PATH:
            meta = await asyncio.to_thread(
                self.filesystem.delete, params["path"],
                recursive=bool(params.get("recursive")),
            )
            return meta, f"Deleted {meta['path']}."

        if kind is ActionKind.MOVE_PATH:
            meta = await asyncio.to_thread(
                self.filesystem.move, params["source"], params["destination"]
            )
            return meta, f"Moved to {meta['destination']}."

        # Terminal
        if kind is ActionKind.EXECUTE_COMMAND:
            result = await self.terminal.run(
                str(params["command"]),
                timeout=params.get("timeout"),
                working_directory=params.get("working_directory"),
            )
            return result.to_dict(), result.summarise()

        if kind is ActionKind.WAIT:
            seconds = max(0.0, min(float(params.get("seconds", 1)), 30.0))
            await asyncio.sleep(seconds)
            return {"seconds": seconds}, f"Waited {seconds:g}s."

        raise BackendError(f"No handler for {kind.value}")

    async def _open_application(
        self, name: str, arguments: list[str]
    ) -> tuple[dict[str, Any], str]:
        """Launch from the allow-list only (§23).

        The name is a key into the discovered-applications map, never a path.
        Accepting a path would make this an arbitrary-execution primitive with
        a friendlier name than ``execute_command``.

        Arguments are constrained for the same reason. An allow-listed
        executable plus an unconstrained argument vector is still arbitrary
        behaviour — ``chromium --user-data-dir=... --load-extension=...`` is a
        different program from ``chromium https://example.com``. Only http(s)
        URLs and paths inside the filesystem allow-list get through; flags do
        not, and the two this container needs for Chromium are added below by
        JARVIS rather than accepted from a caller.
        """
        import os
        import subprocess

        executable = self.applications.get(name)
        if not executable:
            available = ", ".join(sorted(self.applications)) or "none"
            raise BackendError(
                f"{name!r} is not an approved application. Available: {available}."
            )

        checked = [self._check_launch_argument(str(a)) for a in arguments]

        display = getattr(self.backend, "_display_name", None)
        # The scrubbed environment, not the daemon's. A launched application is
        # no more entitled to ANTHROPIC_API_KEY than a launched command is
        # (§19), and inheriting os.environ here would have quietly undone the
        # allow-list that terminal.py builds so carefully.
        from jarvis.computer.terminal import build_environment

        env = build_environment()
        for name_ in ("XAUTHORITY", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS"):
            value = os.environ.get(name_)
            if value:
                env[name_] = value
        if display:
            env["DISPLAY"] = display
        elif os.environ.get("DISPLAY"):
            env["DISPLAY"] = os.environ["DISPLAY"]

        # Chromium refuses to run as root without --no-sandbox, and this
        # container is root. Added only for the browser, only when needed.
        argv = [executable, *checked]
        if "chrom" in executable.lower():
            argv[1:1] = ["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"]

        process = subprocess.Popen(
            argv, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        self._launched[name] = process

        # Wait for a window rather than a fixed sleep: applications differ by
        # an order of magnitude in start-up time, and a fixed delay is either
        # too slow or wrong.
        appeared = False
        for _ in range(60):
            await asyncio.sleep(0.25)
            if process.poll() is not None:
                raise BackendError(
                    f"{name} exited immediately with code {process.returncode}."
                )
            try:
                if await asyncio.to_thread(self.backend.windows):
                    appeared = True
                    break
            except Exception:
                continue

        return (
            {"application": name, "pid": process.pid, "window_appeared": appeared},
            f"Opened {name} (pid {process.pid})."
            + ("" if appeared else " No window appeared yet."),
        )

    def _check_launch_argument(self, argument: str) -> str:
        """Accept a URL or an allow-listed path. Refuse everything else."""
        if argument.startswith("-"):
            raise BackendError(
                f"{argument!r} is a command-line flag. JARVIS passes only URLs "
                "and approved file paths to an application, because flags can "
                "change what the program fundamentally does."
            )
        if argument.startswith(("http://", "https://")):
            return argument
        if argument.startswith("file://"):
            # Legitimate — opening a local document in a viewer — but a
            # ``file://`` URL is a filesystem read wearing a URL, so it clears
            # the same boundary. Otherwise ``file:///etc/shadow`` would be a
            # way to put a file on screen that read_file refuses to open.
            from urllib.parse import unquote, urlparse

            path = unquote(urlparse(argument).path)
            return "file://" + str(self.filesystem.check(path, operation="read"))
        # Any other URI scheme, with or without the ``//``. ``javascript:`` and
        # ``data:`` have no authority component, and matching on ``://`` alone
        # would let both through as if they were relative paths.
        if _URI_SCHEME.match(argument):
            raise BackendError(
                f"{argument!r} uses a scheme JARVIS will not open. Only http, "
                "https and approved file:// URLs are passed to an application."
            )
        # Anything else is treated as a path and must clear the same boundary
        # a read would (§17), so opening a file cannot reach outside the roots.
        return str(self.filesystem.check(argument, operation="read"))

    async def _close_application(self, name: str) -> tuple[dict[str, Any], str]:
        """Close only what JARVIS started.

        Refusing to close applications it did not launch is a deliberate
        limit: the user's own editor with unsaved work is exactly the thing an
        agent should not be able to terminate.
        """
        from jarvis.computer.terminal import kill_process_tree

        process = self._launched.get(name)
        if process is None:
            raise BackendError(
                f"JARVIS did not start {name!r}, so it will not close it."
            )
        if process.poll() is not None:
            return {"application": name, "already_exited": True}, f"{name} had exited."

        # Shared with the terminal's timeout path so there is one definition of
        # "kill this and its children" rather than two that disagree by
        # platform. The previous inline os.killpg raised AttributeError on
        # Windows, where that function does not exist.
        if not kill_process_tree(process):
            process.terminate()

        for _ in range(20):
            await asyncio.sleep(0.1)
            if process.poll() is not None:
                break
        self._launched.pop(name, None)
        return {"application": name, "exit_code": process.returncode}, f"Closed {name}."

    # ── verification (§9) ────────────────────────────────────────────────────

    def _observe_quietly(self):
        try:
            return self.observation.observe(ObservationOptions(include_image=True))
        except Exception as exc:
            log.debug("pre_observation_failed", error=str(exc))
            return None

    def _verify(
        self, action: ComputerAction, before: Any
    ) -> tuple[VerificationOutcome, str, str | None]:
        """Did the screen respond?

        A deliberately modest claim. This checks that *something* changed, not
        that the right thing did — deciding whether the change matches the
        intent needs the reasoner, which has the expectation in natural
        language. What this rules out is the common and otherwise silent
        failure: a click that landed on nothing.
        """
        if before is None or action.kind not in _VISUAL_ACTIONS:
            return VerificationOutcome.UNVERIFIED, "", None

        try:
            after = self.observation.observe(
                ObservationOptions(include_image=True, force_image=True)
            )
        except Exception as exc:
            return (
                VerificationOutcome.INCONCLUSIVE,
                f"Could not observe after the action: {exc}",
                None,
            )

        screenshot_id = after.state.screenshot_id
        if after.change_delta <= 2:
            return (
                VerificationOutcome.CONTRADICTED,
                "The screen did not change. The action may have missed its "
                "target, or the application may not have responded.",
                screenshot_id,
            )

        # Where it changed matters as much as whether it did. A click that
        # lands on empty space while a clock ticks elsewhere would otherwise
        # verify as CONFIRMED — the screen changed, just not because of the
        # click. Anchoring to the target turns that into INCONCLUSIVE, which
        # is what makes the agent look again instead of pressing on.
        target = _action_point(action)
        region = after.changed_region
        if target and not region:
            # Something moved, but too diffusely to localise — often just
            # antialiasing or a cursor. With a target to check against, "I
            # cannot attribute this change" is the honest answer, and it is
            # the one that makes the agent look again.
            return (
                VerificationOutcome.INCONCLUSIVE,
                f"The screen changed slightly (delta {after.change_delta}) but "
                f"not in any identifiable region, so the change cannot be "
                f"attributed to the action at {target}.",
                screenshot_id,
            )
        if target and region:
            if _within(region, target, margin=_VERIFY_MARGIN):
                return (
                    VerificationOutcome.CONFIRMED,
                    f"Screen changed at the target: region {region}.",
                    screenshot_id,
                )
            return (
                VerificationOutcome.INCONCLUSIVE,
                f"The screen changed in region {region}, which is not near "
                f"{target}. Something moved, but perhaps not because of this "
                "action.",
                screenshot_id,
            )

        return (
            VerificationOutcome.CONFIRMED,
            f"Screen changed in region {region}."
            if region
            else f"Screen changed (delta {after.change_delta}).",
            screenshot_id,
        )

    # ── confirmation and audit ───────────────────────────────────────────────

    async def _request_confirmation(
        self, action: ComputerAction, ctx: ExecutionContext, decision: PolicyDecision
    ) -> str | None:
        """Create or consume a confirmation.

        Reuses Phase 1's service, so approvals are fingerprint-bound to the
        exact action, single-use, and time-bounded — an approval for one click
        cannot authorise a different one, and one the user walked away from
        goes stale.
        """
        if self.confirmations is None:
            return None

        fingerprint_args = {**action.params}
        for key in _REDACT_PARAMS:
            if key in fingerprint_args:
                # Bound to a digest of the content rather than the content, so
                # the confirmation record never stores the text being typed
                # while still binding the approval to that exact text.
                import hashlib

                digest = hashlib.sha256(
                    str(fingerprint_args[key]).encode()
                ).hexdigest()[:16]
                fingerprint_args[key] = f"sha256:{digest}"

        existing = await self.confirmations.find_approval(
            ctx.user_id, f"computer:{action.kind.value}", fingerprint_args
        )
        if existing is not None:
            await self.confirmations.consume(existing)
            log.info("computer_action_preapproved", kind=action.kind.value)
            return None

        confirmation = await self.confirmations.request(
            _confirmation_request(action, ctx, decision, fingerprint_args)
        )
        return confirmation.id

    async def _finish(
        self,
        action: ComputerAction,
        ctx: ExecutionContext,
        decision: PolicyDecision,
        outcome: ActionOutcome,
        *,
        detail: str = "",
        data: dict[str, Any] | None = None,
        error: str | None = None,
        duration_ms: float = 0.0,
        verification: VerificationOutcome = VerificationOutcome.UNVERIFIED,
        verification_detail: str = "",
        observation_before: str | None = None,
        observation_after: str | None = None,
        confirmation_id: str | None = None,
    ) -> ActionResult:
        """Write the audit row and build the result. Every path ends here."""
        record = ComputerAudit(
            user_id=ctx.user_id,
            task_id=ctx.task_id,
            request_id=ctx.request_id,
            sequence=ctx.sequence,
            kind=action.kind.value,
            scope=action.scope.value,
            summary=action.describe()[:1000],
            reason=action.reason or None,
            params=_redact_params(action.params),
            risk=decision.risk.value,
            decision=decision.mode.value,
            decision_reason=decision.reason,
            applied_rules=decision.applied_rules,
            confirmation_id=confirmation_id,
            outcome=outcome.value,
            detail=scrub_text(detail)[:4000] if detail else None,
            error=scrub_text(error)[:2000] if error else None,
            duration_ms=duration_ms,
            verification=verification.value,
            verification_detail=verification_detail or None,
            observation_before=observation_before,
            observation_after=observation_after,
            tainted=action.tainted,
            actor=ctx.actor,
        )
        ctx.session.add(record)
        await ctx.session.flush()

        if self.activity is not None:
            from jarvis.db.models import ActivityKind

            await self.activity.record(
                ActivityKind.COMPUTER_ACTION,
                summary=f"{action.kind.value}: {outcome.value}",
                actor=ctx.actor,
                detail={
                    "action": action.kind.value,
                    "risk": decision.risk.value,
                    "decision": decision.mode.value,
                    "outcome": outcome.value,
                },
                request_id=ctx.request_id,
                status=outcome.value,
                duration_ms=duration_ms,
            )

        log.info(
            "computer_action",
            kind=action.kind.value,
            scope=action.scope.value,
            risk=decision.risk.value,
            decision=decision.mode.value,
            outcome=outcome.value,
            verification=verification.value,
            duration_ms=round(duration_ms, 1),
        )

        return ActionResult(
            outcome=outcome,
            action=action,
            detail=detail,
            data=data or {},
            duration_ms=duration_ms,
            risk=decision.risk,
            decision=decision.mode.value,
            verification=verification,
            verification_detail=verification_detail,
            confirmation_id=confirmation_id,
            audit_id=record.id,
        )


#: How far from the target a change may be and still count as caused by it.
#: A click opens a menu below the cursor or a dialog centred nearby, so the
#: neighbourhood has to be generous — this is a check against changes on the
#: other side of the screen, not a pixel assertion.
_VERIFY_MARGIN = 250


def _action_point(action: ComputerAction) -> tuple[int, int] | None:
    """Where on screen the action was aimed, if anywhere."""
    params = action.params
    if action.kind in {
        ActionKind.CLICK, ActionKind.DOUBLE_CLICK, ActionKind.RIGHT_CLICK,
        ActionKind.MOVE_MOUSE,
    } and "x" in params:
        return int(params["x"]), int(params["y"])
    if action.kind is ActionKind.DRAG and "to_x" in params:
        return int(params["to_x"]), int(params["to_y"])
    return None


def _within(
    region: tuple[int, int, int, int], point: tuple[int, int], *, margin: int
) -> bool:
    left, top, right, bottom = region
    x, y = point
    return (
        left - margin <= x <= right + margin
        and top - margin <= y <= bottom + margin
    )


def _redact_params(params: dict[str, Any]) -> dict[str, Any]:
    """Replace content values with their length before storage.

    The audit log is displayed in a UI and can be exported. Typed text and file
    content are exactly what must not be duplicated into it — the whole point
    of refusing to store credentials elsewhere is lost if they land here.
    """
    out: dict[str, Any] = {}
    for key, value in params.items():
        if key in _REDACT_PARAMS and isinstance(value, str):
            out[key] = f"<{len(value)} characters>"
        elif isinstance(value, str) and len(value) > 500:
            out[key] = value[:500] + "…"
        else:
            out[key] = value
    return out


def _confirmation_request(
    action: ComputerAction,
    ctx: ExecutionContext,
    decision: PolicyDecision,
    fingerprint_args: dict[str, Any],
):
    from jarvis.confirmations.service import ConfirmationRequest
    from jarvis.db.models import RiskLevel

    risk_map = {
        ActionRisk.LOW: RiskLevel.LOW,
        ActionRisk.MEDIUM: RiskLevel.MEDIUM,
        ActionRisk.HIGH: RiskLevel.HIGH,
        ActionRisk.PROHIBITED: RiskLevel.CRITICAL,
    }
    return ConfirmationRequest(
        user_id=ctx.user_id,
        title=f"Allow JARVIS to {action.kind.value.replace('_', ' ')}?",
        body=(
            f"{action.describe()}\n\n"
            f"Risk: {decision.risk.value}. {decision.reason}"
            + ("\n\nThis cannot be undone." if decision.irreversible else "")
        ),
        tool_name=f"computer:{action.kind.value}",
        arguments=fingerprint_args,
        risk_level=risk_map[decision.risk],
        reversible=not decision.irreversible,
        request_id=ctx.request_id,
        reason=decision.reason,
    )
