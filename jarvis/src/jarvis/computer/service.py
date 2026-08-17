"""The computer service — assembly and the §38 API surface.

Owns the objects that must be process-wide singletons and cannot be rebuilt per
request:

* the **backend**, because an X11 connection is expensive and stateful;
* the **screenshot store**, because a TTL that resets every request is not a
  TTL;
* the **emergency stop**, because a latch with per-request scope stops nothing;
* the **capability report**, probed once at startup.

Everything session-scoped — the policy engine, the executor, the agent — is
constructed per request around the caller's database session, the same pattern
Phase 1 uses for the tool executor.

The service is created even when the machine has no display. It reports what it
cannot do rather than being absent, so ``/api/computer/status`` answers the
question "why can't JARVIS see my screen?" instead of returning 404.
"""

from __future__ import annotations

import platform
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from jarvis.computer.agent import ComputerAgent
from jarvis.computer.backends.base import DesktopBackend
from jarvis.computer.backends.unavailable import UnavailableBackend
from jarvis.computer.capabilities import CapabilityReport, detect, start_virtual_display
from jarvis.computer.control import EmergencyStop
from jarvis.computer.executor import ActionExecutor, ExecutionContext
from jarvis.computer.filesystem import FilesystemGuard, FilesystemPolicy
from jarvis.computer.observation import ObservationProcessor, ScreenshotStore
from jarvis.computer.policy import (
    ComputerPolicy,
    ComputerPolicyEngine,
    load_policy,
    save_policy,
)
from jarvis.computer.reasoner import ComputerReasoner
from jarvis.computer.terminal import TerminalExecutor
from jarvis.computer.types import (
    ActionKind,
    ActionResult,
    ComputerAction,
    ComputerTaskStatus,
)
from jarvis.db.models import ComputerAudit, ComputerTask
from jarvis.logging import get_logger

log = get_logger(__name__)


@dataclass(slots=True)
class ComputerSettings:
    enabled: bool = True
    display: str | None = None
    use_virtual_display: bool = False
    virtual_width: int = 1280
    virtual_height: int = 800
    file_roots: list[Path] | None = None
    can_write_files: bool = False
    can_delete_files: bool = False
    working_directory: Path | None = None
    screenshot_ttl_seconds: int = 300
    screenshot_retain: bool = False
    screenshot_dir: Path | None = None
    max_steps: int = 25
    task_timeout_seconds: float = 300.0


class ComputerService:
    def __init__(
        self,
        settings: ComputerSettings,
        *,
        router: Any = None,
        activity_bus: Any = None,
    ) -> None:
        self.settings = settings
        self.router = router
        self.activity_bus = activity_bus
        self.emergency_stop = EmergencyStop()

        self._virtual_display: subprocess.Popen[bytes] | None = None
        self._display: str | None = settings.display
        self.capabilities: CapabilityReport = CapabilityReport()
        self.backend: DesktopBackend = UnavailableBackend("Not initialised.")
        self.screenshots = ScreenshotStore(
            ttl_seconds=settings.screenshot_ttl_seconds,
            persist_dir=settings.screenshot_dir if settings.screenshot_retain else None,
        )
        self.observation = ObservationProcessor(self.backend, self.screenshots)

    # ── lifecycle ────────────────────────────────────────────────────────────

    def start(self) -> CapabilityReport:
        """Probe, optionally create a virtual display, and pick a backend."""
        if not self.settings.enabled:
            self.capabilities = detect()
            self.capabilities.notes.append(
                "Computer control is disabled (JARVIS_COMPUTER_ENABLED=false)."
            )
            self.backend = UnavailableBackend("Computer control is disabled.")
            self.observation = ObservationProcessor(self.backend, self.screenshots)
            return self.capabilities

        self.capabilities = detect(probe_display=self._display)

        if (
            not self.capabilities.display
            and self.settings.use_virtual_display
            and self.capabilities.can_create_virtual_display
        ):
            started = start_virtual_display(
                width=self.settings.virtual_width,
                height=self.settings.virtual_height,
            )
            if started:
                self._display, self._virtual_display = started
                self.capabilities = detect(probe_display=self._display)

        if platform.system() == "Windows":
            # Windows has no X display and never will, so the display probe
            # above says "no desktop" on a machine that plainly has one. The
            # platform answers this question, not the display server.
            #
            # UNVERIFIED — WINDOWS RUNTIME: this branch has never executed.
            from jarvis.computer.backends.windows import WindowsBackend

            try:
                self.backend = WindowsBackend()
            except Exception as exc:
                log.warning("windows_backend_failed", error=str(exc))
                self.backend = UnavailableBackend(str(exc))
        elif self.capabilities.display:
            from jarvis.computer.backends.x11 import X11Backend

            try:
                self.backend = X11Backend(self.capabilities.display)
            except Exception as exc:
                log.warning("x11_backend_failed", error=str(exc))
                self.backend = UnavailableBackend(str(exc))
        else:
            self.backend = UnavailableBackend(
                self.capabilities.reason_unavailable(ActionKind.SCREENSHOT)
                or "No display is available."
            )

        self.observation = ObservationProcessor(self.backend, self.screenshots)
        log.info(
            "computer_service_started",
            backend=self.backend.key,
            display=self.capabilities.display,
            virtual=self._virtual_display is not None,
        )
        return self.capabilities

    def shutdown(self) -> None:
        try:
            self.backend.close()
        except Exception:
            pass
        self.screenshots.clear()
        if self._virtual_display is not None:
            self._virtual_display.terminate()
            try:
                self._virtual_display.wait(timeout=5)
            except Exception:
                self._virtual_display.kill()
            self._virtual_display = None

    # ── per-request assembly ─────────────────────────────────────────────────

    def filesystem_guard(self) -> FilesystemGuard:
        return FilesystemGuard(
            FilesystemPolicy(
                allowed_paths=list(self.settings.file_roots or []),
                can_read=True,
                can_write=self.settings.can_write_files,
                can_delete=self.settings.can_delete_files,
            )
        )

    def terminal(self) -> TerminalExecutor:
        roots = list(self.settings.file_roots or [])
        working = self.settings.working_directory or (roots[0] if roots else Path.cwd())
        return TerminalExecutor(working_directory=working, allowed_roots=roots)

    async def executor(
        self, session: AsyncSession, user_id: str, *, policy: ComputerPolicy | None = None
    ) -> ActionExecutor:
        from jarvis.activity.service import ActivityService
        from jarvis.confirmations.service import ConfirmationService

        resolved = policy or await load_policy(session, user_id)
        return ActionExecutor(
            backend=self.backend,
            observation=self.observation,
            policy_engine=ComputerPolicyEngine(
                session, capabilities=self.capabilities, policy=resolved
            ),
            emergency_stop=self.emergency_stop,
            filesystem=self.filesystem_guard(),
            terminal=self.terminal(),
            applications=self.capabilities.known_applications,
            confirmations=ConfirmationService(session),
            activity=ActivityService(session, self.activity_bus),
        )

    async def agent(
        self, session: AsyncSession, user_id: str
    ) -> tuple[ComputerAgent, ComputerPolicy]:
        policy = await load_policy(session, user_id)
        executor = await self.executor(session, user_id, policy=policy)
        agent = ComputerAgent(
            executor=executor,
            observation=self.observation,
            reasoner=ComputerReasoner(router=self.router),
            emergency_stop=self.emergency_stop,
            enabled_scopes=set(policy.enabled_scopes),
            max_steps=self.settings.max_steps,
            task_timeout_seconds=self.settings.task_timeout_seconds,
        )
        return agent, policy

    # ── §38 surface ──────────────────────────────────────────────────────────

    async def execute_action(
        self,
        session: AsyncSession,
        user_id: str,
        action: ComputerAction,
        *,
        request_id: str | None = None,
        actor: str = "user",
    ) -> ActionResult:
        executor = await self.executor(session, user_id)
        return await executor.execute(
            action,
            ExecutionContext(
                user_id=user_id, session=session, request_id=request_id, actor=actor
            ),
        )

    async def status(self, session: AsyncSession, user_id: str) -> dict[str, Any]:
        policy = await load_policy(session, user_id)
        stop = self.emergency_stop.state()

        active = (
            await session.execute(
                select(ComputerTask)
                .where(
                    ComputerTask.user_id == user_id,
                    ComputerTask.status.in_(
                        [
                            ComputerTaskStatus.RUNNING.value,
                            ComputerTaskStatus.PLANNING.value,
                            ComputerTaskStatus.WAITING_FOR_USER.value,
                        ]
                    ),
                )
                .order_by(desc(ComputerTask.created_at))
                .limit(1)
            )
        ).scalars().first()

        window = None
        current_application = None
        if self.capabilities.display and self.backend.key != "unavailable":
            try:
                info = self.backend.active_window()
                if info:
                    window = info.to_dict()
                    current_application = info.application or info.title
            except Exception as exc:
                log.debug("status_active_window_failed", error=str(exc))

        return {
            "enabled": self.settings.enabled,
            "connected": self.backend.key != "unavailable",
            "backend": self.backend.key,
            "capabilities": self.capabilities.to_dict(),
            "policy": policy.to_dict(),
            "emergency_stop": stop.to_dict(),
            "active_window": window,
            "current_application": current_application,
            "active_task": (
                {
                    "id": active.id,
                    "description": active.description,
                    "status": active.status,
                    "current_step": active.current_step,
                    "steps": active.step_count,
                    "waiting_reason": active.waiting_reason,
                }
                if active
                else None
            ),
            "filesystem": self.filesystem_guard().policy.to_dict(),
            "screenshots": {
                "held": len(self.screenshots.list()),
                "ttl_seconds": self.settings.screenshot_ttl_seconds,
                "retention": bool(self.settings.screenshot_retain),
            },
            "reasoner_available": self.router is not None,
        }

    async def audit(
        self,
        session: AsyncSession,
        user_id: str,
        *,
        limit: int = 100,
        task_id: str | None = None,
        outcome: str | None = None,
    ) -> list[dict[str, Any]]:
        stmt = select(ComputerAudit).where(ComputerAudit.user_id == user_id)
        if task_id:
            stmt = stmt.where(ComputerAudit.task_id == task_id)
        if outcome:
            stmt = stmt.where(ComputerAudit.outcome == outcome)
        stmt = stmt.order_by(desc(ComputerAudit.created_at)).limit(min(limit, 500))

        rows = (await session.execute(stmt)).scalars().all()
        return [
            {
                "id": row.id,
                "at": row.created_at.isoformat() if row.created_at else None,
                "task_id": row.task_id,
                "kind": row.kind,
                "scope": row.scope,
                "summary": row.summary,
                "reason": row.reason,
                "params": row.params,
                "risk": row.risk,
                "decision": row.decision,
                "decision_reason": row.decision_reason,
                "outcome": row.outcome,
                "detail": row.detail,
                "error": row.error,
                "verification": row.verification,
                "verification_detail": row.verification_detail,
                "duration_ms": row.duration_ms,
                "actor": row.actor,
                "tainted": row.tainted,
            }
            for row in rows
        ]

    async def audit_summary(self, session: AsyncSession, user_id: str) -> dict[str, Any]:
        by_outcome = {
            outcome: int(count)
            for outcome, count in (
                await session.execute(
                    select(ComputerAudit.outcome, func.count())
                    .where(ComputerAudit.user_id == user_id)
                    .group_by(ComputerAudit.outcome)
                )
            ).all()
        }
        by_risk = {
            risk: int(count)
            for risk, count in (
                await session.execute(
                    select(ComputerAudit.risk, func.count())
                    .where(ComputerAudit.user_id == user_id)
                    .group_by(ComputerAudit.risk)
                )
            ).all()
        }
        return {
            "total": sum(by_outcome.values()),
            "by_outcome": by_outcome,
            "by_risk": by_risk,
        }

    async def tasks(
        self, session: AsyncSession, user_id: str, *, limit: int = 50
    ) -> list[dict[str, Any]]:
        rows = (
            await session.execute(
                select(ComputerTask)
                .where(ComputerTask.user_id == user_id)
                .order_by(desc(ComputerTask.created_at))
                .limit(limit)
            )
        ).scalars().all()
        return [
            {
                "id": t.id,
                "description": t.description,
                "objective": t.objective,
                "status": t.status,
                "current_step": t.current_step,
                "steps": t.step_count,
                "completed": t.completed_actions,
                "failed": t.failed_actions,
                "result": t.result,
                "error": t.error,
                "waiting_reason": t.waiting_reason,
                "observations": t.observations,
                "created_at": t.created_at.isoformat() if t.created_at else None,
                "finished_at": t.finished_at.isoformat() if t.finished_at else None,
            }
            for t in rows
        ]

    async def cancel_task(
        self, session: AsyncSession, user_id: str, task_id: str
    ) -> dict[str, Any]:
        task = await session.get(ComputerTask, task_id)
        if task is None or task.user_id != user_id:
            from jarvis.errors import NotFoundError

            raise NotFoundError(f"Task {task_id} not found")

        self.emergency_stop.cancel_task(task_id)
        if not ComputerTaskStatus(task.status).is_terminal:
            task.status = ComputerTaskStatus.CANCELLED.value
            task.finished_at = utcnow_safe()
            task.error = "Cancelled by the user."
            await session.flush()
        return {"task_id": task_id, "status": task.status}

    async def update_policy(
        self, session: AsyncSession, user_id: str, policy: ComputerPolicy
    ) -> ComputerPolicy:
        return await save_policy(session, user_id, policy)


def utcnow_safe():
    from jarvis.db.base import utcnow

    return utcnow()
