"""Emergency stop (§27).

§27's hard requirement is that the stop "work independently of the AI
reasoning process". That rules out the obvious implementation — a tool the
model calls, or a flag a pipeline stage checks — because both run *inside* the
thing being stopped. If the model is looping, confused, or acting on injected
instructions, anything it mediates is exactly what cannot be relied on.

So the stop is a **process-global latch**, set from the HTTP layer without
touching the orchestrator, and checked synchronously by the action executor
immediately before every action. Three properties follow:

* **Nothing the model emits can clear it.** There is no tool, and the API
  route that clears it is a separate explicit call by the user.
* **It is checked at the last possible moment**, after the permission decision
  and after any confirmation, so an action approved a minute ago still does
  not run.
* **It survives a confused agent**, because the check is a plain boolean read
  rather than a message the agent must choose to handle.

In-process state is the right scope: a stop must take effect in microseconds,
and a database round trip is neither fast enough nor available when the
database is the thing that is wedged. The trade-off is that a restart clears
it, which is correct — a fresh process has no in-flight actions to stop — and
the engagement is written to the audit log either way, so the history survives.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from jarvis.errors import JarvisError
from jarvis.logging import get_logger

log = get_logger(__name__)


class EmergencyStopError(JarvisError):
    """Raised when an action is attempted while the stop is engaged."""

    code = "emergency_stop_engaged"
    http_status = 423  # Locked
    retryable = False

    def __init__(self, message: str = "Emergency stop is engaged") -> None:
        super().__init__(
            message,
            user_message=(
                "The emergency stop is engaged. No computer actions will run "
                "until it is released."
            ),
        )


@dataclass(frozen=True, slots=True)
class StopState:
    engaged: bool
    reason: str = ""
    engaged_at: str | None = None
    engaged_by: str = ""
    #: Actions refused since the stop was engaged. Evidence that it is working.
    blocked_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "engaged": self.engaged,
            "reason": self.reason,
            "engaged_at": self.engaged_at,
            "engaged_by": self.engaged_by,
            "blocked_count": self.blocked_count,
        }


class EmergencyStop:
    """A latch. Deliberately the least clever object in the system.

    Lock-guarded rather than a bare boolean because the count is mutated from
    request threads and the executor concurrently, and a torn read of the
    reason string alongside the flag would produce a confusing audit entry.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._engaged = False
        self._reason = ""
        self._at: str | None = None
        self._by = ""
        self._blocked = 0
        #: Cancellation flags for running tasks, so a stop interrupts work
        #: already in flight rather than only preventing new work.
        self._cancelled_tasks: set[str] = set()

    # ── the hot path ─────────────────────────────────────────────────────────

    @property
    def engaged(self) -> bool:
        """Read on every action. Kept trivial for that reason."""
        return self._engaged

    def check(self) -> None:
        """Raise if engaged. Called immediately before execution."""
        if self._engaged:
            with self._lock:
                self._blocked += 1
            log.warning("emergency_stop_blocked_action", reason=self._reason)
            raise EmergencyStopError(f"Emergency stop engaged: {self._reason}")

    # ── control ──────────────────────────────────────────────────────────────

    def engage(self, *, reason: str = "User pressed stop", by: str = "user") -> StopState:
        with self._lock:
            self._engaged = True
            self._reason = reason
            self._at = datetime.now(timezone.utc).isoformat()
            self._by = by
        log.warning("emergency_stop_engaged", reason=reason, by=by)
        return self.state()

    def release(self, *, by: str = "user") -> StopState:
        """Release. Only the user does this — there is no tool for it, and the
        model has no way to reach this method."""
        with self._lock:
            self._engaged = False
            self._reason = ""
            self._at = None
            self._by = ""
            self._blocked = 0
            self._cancelled_tasks.clear()
        log.info("emergency_stop_released", by=by)
        return self.state()

    def state(self) -> StopState:
        with self._lock:
            return StopState(
                engaged=self._engaged,
                reason=self._reason,
                engaged_at=self._at,
                engaged_by=self._by,
                blocked_count=self._blocked,
            )

    # ── task cancellation ────────────────────────────────────────────────────

    def cancel_task(self, task_id: str) -> None:
        """Cancel one task without engaging the global stop."""
        with self._lock:
            self._cancelled_tasks.add(task_id)
        log.info("computer_task_cancel_requested", task_id=task_id)

    def is_cancelled(self, task_id: str | None) -> bool:
        """True when this task, or everything, should stop.

        The loop checks between steps, which is what makes a cancel land
        mid-task rather than at the end of it.
        """
        if self._engaged:
            return True
        if task_id is None:
            return False
        with self._lock:
            return task_id in self._cancelled_tasks

    def clear_task(self, task_id: str) -> None:
        with self._lock:
            self._cancelled_tasks.discard(task_id)
