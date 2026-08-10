"""Safe built-in tools that exercise the orchestration path.

These exist to prove the architecture end-to-end, not to be useful in
themselves. All are read-only, side-effect free, and rated ``NONE`` risk, so
Phase 1 ships nothing that can damage anything.
"""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import func, select

from jarvis.db.models import (
    ActivityLog,
    Capability,
    Conversation,
    Message,
    RiskLevel,
    Task,
    TaskStatus,
    ToolExecution,
)
from jarvis.tools.base import ToolContext, ToolResult, tool


@tool(
    name="get_current_time",
    description=(
        "Get the current date and time. Use this whenever the answer depends on "
        "what time it is now — you have no other way to know. Optionally pass an "
        "IANA timezone such as 'Europe/Nicosia' or 'America/New_York'."
    ),
    parameters={
        "type": "object",
        "properties": {
            "timezone": {
                "type": "string",
                "description": "IANA timezone name. Defaults to UTC.",
            }
        },
        "required": [],
        "additionalProperties": False,
    },
    capability=Capability.READ,
    risk_level=RiskLevel.NONE,
    category="system",
)
async def get_current_time(*, ctx: ToolContext, timezone: str | None = None) -> ToolResult:
    tz_name = timezone or "UTC"
    try:
        tz = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError):
        return ToolResult.error(
            f"Unknown timezone '{tz_name}'. Use an IANA name such as 'Europe/London'."
        )

    now = datetime.now(tz)
    return ToolResult.ok(
        f"{now.strftime('%A, %d %B %Y at %H:%M:%S')} ({tz_name})",
        iso=now.isoformat(),
        timezone=tz_name,
        utc_offset=now.strftime("%z"),
        unix=int(now.timestamp()),
    )


@tool(
    name="system_status",
    description=(
        "Report JARVIS's own operational status: how many conversations, tasks, "
        "and tool executions exist, and what is currently outstanding. Use this "
        "when asked how you are doing, what you are working on, or what state "
        "you are in."
    ),
    parameters={
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": False,
    },
    capability=Capability.READ,
    risk_level=RiskLevel.NONE,
    category="system",
)
async def system_status(*, ctx: ToolContext) -> ToolResult:
    session = ctx.session

    async def count(stmt) -> int:  # type: ignore[no-untyped-def]
        return (await session.execute(stmt)).scalar_one()

    conversations = await count(
        select(func.count(Conversation.id)).where(Conversation.user_id == ctx.user_id)
    )
    messages = await count(select(func.count(Message.id)))
    total_tasks = await count(
        select(func.count(Task.id)).where(Task.user_id == ctx.user_id)
    )
    open_tasks = await count(
        select(func.count(Task.id)).where(
            Task.user_id == ctx.user_id,
            Task.status.notin_([TaskStatus.COMPLETED, TaskStatus.CANCELLED]),
        )
    )
    tool_calls = await count(select(func.count(ToolExecution.id)))
    activity = await count(select(func.count(ActivityLog.id)))

    summary = (
        f"Operational. {open_tasks} open task(s) of {total_tasks} total, "
        f"{conversations} conversation(s), {tool_calls} tool call(s) executed."
    )
    return ToolResult.ok(
        summary,
        conversations=conversations,
        messages=messages,
        tasks_total=total_tasks,
        tasks_open=open_tasks,
        tool_executions=tool_calls,
        activity_records=activity,
        server_time=datetime.now(timezone.utc).isoformat(),
    )


TOOLS = [get_current_time, system_status]
