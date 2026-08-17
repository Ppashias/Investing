"""Self-observation (Phase D, item 13).

"What is failing most often?" was previously answerable only by reading logs by
hand. This answers it from the rows JARVIS already writes.

## It does not transmit anything, and that is the design

`vierisid/jarvis` ships anonymous telemetry to a collector, with an anonymous
id, an opt-in and a config module to govern it. That is a reasonable choice for
a product with users to learn from. It is the wrong choice here: this is a
local-first personal assistant whose whole proposition is that your notes, your
screen and your browsing stay on your machine. Adding an egress path — even an
opt-in, even anonymised — creates a channel that has to be right forever, and
the value it buys is telemetry nobody is collecting.

So there is no client, no endpoint, no id, and no opt-in switch, because there
is nothing to opt out of. Everything here is a ``SELECT`` over local tables,
rendered for the operator through the existing API.

## What it deliberately does not read

Not arguments, not results, not error *messages*, not confirmation bodies, not
memory content. Only names, codes, statuses, counts and durations.

That is not squeamishness. An observability surface that reads content is one
prompt-injection away from being an exfiltration surface — a page that gets
itself quoted into an error message would otherwise appear in a panel, and
panels get screenshotted and pasted. Counting an ``error_code`` cannot leak
what the error was about.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from jarvis.db.base import utcnow
from jarvis.db.models import (
    ActivityKind,
    ActivityLog,
    Confirmation,
    ConfirmationStatus,
    ExecutionStatus,
    PermissionMode,
    ToolExecution,
)

#: How far back a report looks by default. A week is long enough for a pattern
#: and short enough that "recently" still means something.
DEFAULT_WINDOW_HOURS = 168


@dataclass(slots=True)
class ToolStats:
    tool_name: str
    calls: int = 0
    failures: int = 0
    denied: int = 0
    awaiting: int = 0
    p50_ms: float = 0.0
    p95_ms: float = 0.0

    @property
    def failure_rate(self) -> float:
        return round(self.failures / self.calls, 3) if self.calls else 0.0

    def describe(self) -> dict[str, Any]:
        return {
            "tool": self.tool_name,
            "calls": self.calls,
            "failures": self.failures,
            "failure_rate": self.failure_rate,
            "denied": self.denied,
            "awaiting_confirmation": self.awaiting,
            "p50_ms": self.p50_ms,
            "p95_ms": self.p95_ms,
        }


@dataclass(slots=True)
class TelemetryReport:
    window_hours: int
    tools: list[ToolStats] = field(default_factory=list)
    permission_decisions: dict[str, int] = field(default_factory=dict)
    confirmations: dict[str, int] = field(default_factory=dict)
    activity_kinds: dict[str, int] = field(default_factory=dict)
    top_error_codes: list[dict[str, Any]] = field(default_factory=list)

    def describe(self) -> dict[str, Any]:
        return {
            "window_hours": self.window_hours,
            "tools": [t.describe() for t in self.tools],
            "permission_decisions": self.permission_decisions,
            "confirmations": self.confirmations,
            "activity_kinds": self.activity_kinds,
            "top_error_codes": self.top_error_codes,
            # Stated in the payload, not only in the docs. Somebody reading
            # this over the API should not have to go looking for whether it
            # went anywhere.
            "transmitted_anywhere": False,
        }


def _percentile(values: list[float], fraction: float) -> float:
    """Nearest-rank, because interpolating between two samples invents a
    duration that never happened."""
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, round(fraction * len(ordered)) - 1))
    return round(ordered[index], 1)


class TelemetryService:
    """Reads local rows. Writes nothing, sends nothing."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def report(self, *, window_hours: int = DEFAULT_WINDOW_HOURS) -> TelemetryReport:
        since = utcnow() - timedelta(hours=window_hours)
        report = TelemetryReport(window_hours=window_hours)

        rows = (
            await self.session.execute(
                select(
                    ToolExecution.tool_name,
                    ToolExecution.status,
                    ToolExecution.permission_decision,
                    ToolExecution.duration_ms,
                    ToolExecution.error_code,
                ).where(ToolExecution.created_at >= since)
            )
        ).all()

        by_tool: dict[str, ToolStats] = {}
        durations: dict[str, list[float]] = {}
        decisions: dict[str, int] = {}
        error_codes: dict[str, int] = {}

        for name, status, decision, duration, error_code in rows:
            stats = by_tool.setdefault(name, ToolStats(tool_name=name))
            stats.calls += 1
            if status is ExecutionStatus.FAILED:
                stats.failures += 1
            elif status is ExecutionStatus.AWAITING_CONFIRMATION:
                stats.awaiting += 1
            if decision is PermissionMode.DENY:
                stats.denied += 1
            if decision is not None:
                decisions[decision.value] = decisions.get(decision.value, 0) + 1
            if duration is not None:
                durations.setdefault(name, []).append(float(duration))
            if error_code:
                # The code, never the message. A message can quote a page.
                error_codes[error_code] = error_codes.get(error_code, 0) + 1

        for name, stats in by_tool.items():
            samples = durations.get(name, [])
            stats.p50_ms = _percentile(samples, 0.5)
            stats.p95_ms = _percentile(samples, 0.95)

        # Noisiest first: the report exists to answer "what is failing most
        # often?", so sorting by name would bury the answer.
        report.tools = sorted(
            by_tool.values(), key=lambda s: (s.failures, s.calls), reverse=True
        )
        report.permission_decisions = decisions
        report.top_error_codes = [
            {"code": code, "count": count}
            for code, count in sorted(
                error_codes.items(), key=lambda kv: kv[1], reverse=True
            )[:10]
        ]

        confirmations = (
            await self.session.execute(
                select(Confirmation.status, func.count())
                .where(Confirmation.created_at >= since)
                .group_by(Confirmation.status)
            )
        ).all()
        report.confirmations = {
            status.value if isinstance(status, ConfirmationStatus) else str(status):
            count
            for status, count in confirmations
        }

        kinds = (
            await self.session.execute(
                select(ActivityLog.kind, func.count())
                .where(ActivityLog.created_at >= since)
                .group_by(ActivityLog.kind)
            )
        ).all()
        report.activity_kinds = {
            kind.value if isinstance(kind, ActivityKind) else str(kind): count
            for kind, count in kinds
        }

        return report
