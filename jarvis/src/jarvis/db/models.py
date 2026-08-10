"""SQLAlchemy models for the Phase 1 schema.

Design notes worth knowing before extending this:

* **Task and TaskExecution are separate.** A task is the intent; an execution
  is one attempt at it. A task that failed twice and then succeeded has one
  ``Task`` row and three ``TaskExecution`` rows. Phase 10's autonomous retry
  loop depends on this split existing from the start.
* **ToolDefinition is a registry mirror, not the source of truth.** Tools are
  declared in code. This table persists per-tool policy overrides and gives
  the permission engine something to join against.
* **ActivityLog is append-only.** Nothing updates it. It is the observability
  feed in Phase 1 and grows into the audit trail the computer-control phases
  require, which is why the actor and decision columns exist now.
"""

from __future__ import annotations

import enum
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    Float,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from jarvis.db.base import Base, new_id, ts_column, utcnow


# ── enums ────────────────────────────────────────────────────────────────────


class TaskStatus(str, enum.Enum):
    TODO = "TODO"
    IN_PROGRESS = "IN_PROGRESS"
    WAITING = "WAITING"
    BLOCKED = "BLOCKED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"

    @property
    def is_terminal(self) -> bool:
        return self in {TaskStatus.COMPLETED, TaskStatus.CANCELLED}


class TaskPriority(str, enum.Enum):
    LOW = "LOW"
    NORMAL = "NORMAL"
    HIGH = "HIGH"
    URGENT = "URGENT"


class ExecutionStatus(str, enum.Enum):
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    AWAITING_CONFIRMATION = "AWAITING_CONFIRMATION"


class MessageRole(str, enum.Enum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"
    TOOL = "tool"


class ConfirmationStatus(str, enum.Enum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    DENIED = "DENIED"
    EXPIRED = "EXPIRED"


class PermissionMode(str, enum.Enum):
    """Three-valued, not boolean.

    ``ASK`` is what makes supervised and semi-autonomous modes possible; a
    two-valued system forces every ambiguous case to be pre-decided.
    """

    ALLOW = "ALLOW"
    ASK = "ASK"
    DENY = "DENY"


class Capability(str, enum.Enum):
    """Capability classes a tool can require.

    Deliberately *not* a 0-7 ladder — these are orthogonal domains, per the
    Phase 0 audit. A grant is (capability, resource_scope, mode).
    """

    READ = "READ"
    WRITE = "WRITE"
    EXECUTE = "EXECUTE"
    EXTERNAL_ACTION = "EXTERNAL_ACTION"
    SENSITIVE_ACTION = "SENSITIVE_ACTION"


class RiskLevel(str, enum.Enum):
    NONE = "NONE"
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class ActivityKind(str, enum.Enum):
    REQUEST_STARTED = "REQUEST_STARTED"
    REQUEST_COMPLETED = "REQUEST_COMPLETED"
    REQUEST_FAILED = "REQUEST_FAILED"
    STAGE_STARTED = "STAGE_STARTED"
    STAGE_COMPLETED = "STAGE_COMPLETED"
    MODEL_CALL = "MODEL_CALL"
    TOOL_CALL = "TOOL_CALL"
    PERMISSION_DECISION = "PERMISSION_DECISION"
    CONFIRMATION_REQUESTED = "CONFIRMATION_REQUESTED"
    CONFIRMATION_RESOLVED = "CONFIRMATION_RESOLVED"
    TASK_CREATED = "TASK_CREATED"
    TASK_UPDATED = "TASK_UPDATED"
    ERROR = "ERROR"


# ── tables ───────────────────────────────────────────────────────────────────


class User(Base):
    """Single-user today, but authorisation needs a subject to attach to."""

    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(64), primary_key=True,
                                    default=lambda: new_id("user"))
    name: Mapped[str] = mapped_column(String(255), default="operator")
    display_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    settings: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = ts_column(default=utcnow)
    updated_at: Mapped[datetime] = ts_column(default=utcnow, onupdate=utcnow)

    conversations: Mapped[list["Conversation"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    tasks: Mapped[list["Task"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[str] = mapped_column(String(64), primary_key=True,
                                    default=lambda: new_id("conv"))
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    title: Mapped[str] = mapped_column(String(500), default="New conversation")
    #: Reserved for Phase 7 project scoping; no FK yet because the projects
    #: table does not exist until then.
    project_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    meta: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = ts_column(default=utcnow, index=True)
    updated_at: Mapped[datetime] = ts_column(default=utcnow, onupdate=utcnow)
    archived_at: Mapped[datetime | None] = ts_column(nullable=True)

    user: Mapped[User] = relationship(back_populates="conversations")
    messages: Mapped[list["Message"]] = relationship(
        back_populates="conversation",
        cascade="all, delete-orphan",
        order_by="Message.sequence",
    )


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (
        Index("ix_messages_conv_seq", "conversation_id", "sequence", unique=True),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True,
                                    default=lambda: new_id("msg"))
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), index=True
    )
    #: Monotonic per conversation. Ordering by timestamp alone is unsafe when
    #: several messages are written inside one turn.
    sequence: Mapped[int] = mapped_column(Integer, default=0)
    role: Mapped[MessageRole] = mapped_column(String(32))
    content: Mapped[str] = mapped_column(Text, default="")
    #: Structured content blocks (tool_use / tool_result / text) preserved
    #: verbatim so a turn can be replayed to a provider without lossy
    #: reconstruction from ``content``.
    blocks: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, nullable=True)

    provider: Mapped[str | None] = mapped_column(String(64), nullable=True)
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cost_micros: Mapped[int | None] = mapped_column(Integer, nullable=True)
    stop_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    latency_ms: Mapped[float | None] = mapped_column(Float, nullable=True)

    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    meta: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = ts_column(default=utcnow, index=True)

    conversation: Mapped[Conversation] = relationship(back_populates="messages")


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[str] = mapped_column(String(64), primary_key=True,
                                    default=lambda: new_id("task"))
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    parent_task_id: Mapped[str | None] = mapped_column(
        ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True, index=True
    )
    project_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    conversation_id: Mapped[str | None] = mapped_column(
        ForeignKey("conversations.id", ondelete="SET NULL"), nullable=True, index=True
    )

    title: Mapped[str] = mapped_column(String(500))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[TaskStatus] = mapped_column(String(32), default=TaskStatus.TODO,
                                               index=True)
    priority: Mapped[TaskPriority] = mapped_column(String(16),
                                                   default=TaskPriority.NORMAL)
    #: Free-form agent key. No FK — agents are code-defined and the set changes
    #: between releases; a dangling FK would block startup.
    assigned_agent: Mapped[str | None] = mapped_column(String(64), nullable=True,
                                                       index=True)
    tags: Mapped[list[str]] = mapped_column(JSON, default=list)
    meta: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    due_at: Mapped[datetime | None] = ts_column(nullable=True, index=True)
    created_at: Mapped[datetime] = ts_column(default=utcnow, index=True)
    updated_at: Mapped[datetime] = ts_column(default=utcnow, onupdate=utcnow)
    completed_at: Mapped[datetime | None] = ts_column(nullable=True)

    user: Mapped[User] = relationship(back_populates="tasks")
    executions: Mapped[list["TaskExecution"]] = relationship(
        back_populates="task",
        cascade="all, delete-orphan",
        order_by="TaskExecution.attempt",
    )
    subtasks: Mapped[list["Task"]] = relationship(
        back_populates="parent", cascade="all, delete-orphan"
    )
    parent: Mapped["Task | None"] = relationship(
        back_populates="subtasks", remote_side=[id]
    )
    history: Mapped[list["TaskHistory"]] = relationship(
        back_populates="task",
        cascade="all, delete-orphan",
        order_by="TaskHistory.created_at",
    )


class TaskHistory(Base):
    """Append-only field-level change log for a task."""

    __tablename__ = "task_history"

    id: Mapped[str] = mapped_column(String(64), primary_key=True,
                                    default=lambda: new_id("th"))
    task_id: Mapped[str] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), index=True
    )
    field: Mapped[str] = mapped_column(String(64))
    old_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    new_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    actor: Mapped[str] = mapped_column(String(64), default="system")
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = ts_column(default=utcnow, index=True)

    task: Mapped[Task] = relationship(back_populates="history")


class TaskExecution(Base):
    """One attempt at accomplishing a task."""

    __tablename__ = "task_executions"
    __table_args__ = (
        Index("ix_task_executions_task_attempt", "task_id", "attempt", unique=True),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True,
                                    default=lambda: new_id("exec"))
    task_id: Mapped[str] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), index=True
    )
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[ExecutionStatus] = mapped_column(
        String(32), default=ExecutionStatus.RUNNING, index=True
    )
    trigger: Mapped[str] = mapped_column(String(64), default="manual")
    agent: Mapped[str | None] = mapped_column(String(64), nullable=True)
    provider: Mapped[str | None] = mapped_column(String(64), nullable=True)
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)

    result: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration_ms: Mapped[float | None] = mapped_column(Float, nullable=True)

    started_at: Mapped[datetime] = ts_column(default=utcnow, index=True)
    finished_at: Mapped[datetime | None] = ts_column(nullable=True)

    task: Mapped[Task] = relationship(back_populates="executions")
    tool_executions: Mapped[list["ToolExecution"]] = relationship(
        back_populates="task_execution"
    )


class ToolDefinition(Base):
    """Persisted policy for a code-declared tool.

    The registry in :mod:`jarvis.tools.registry` is authoritative for schema
    and handler. This row exists so policy (enabled, required mode, risk
    override) survives restarts and can be edited from the UI.
    """

    __tablename__ = "tool_definitions"

    name: Mapped[str] = mapped_column(String(128), primary_key=True)
    version: Mapped[str] = mapped_column(String(32), default="1")
    description: Mapped[str] = mapped_column(Text, default="")
    parameters_schema: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    capability: Mapped[Capability] = mapped_column(String(32), default=Capability.READ)
    risk_level: Mapped[RiskLevel] = mapped_column(String(16), default=RiskLevel.NONE)
    requires_confirmation: Mapped[bool] = mapped_column(Boolean, default=False)
    reversible: Mapped[bool] = mapped_column(Boolean, default=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    #: Operator override of the engine's computed decision. NULL means "no
    #: override" — the engine's own evaluation stands.
    mode_override: Mapped[PermissionMode | None] = mapped_column(
        String(16), nullable=True
    )
    created_at: Mapped[datetime] = ts_column(default=utcnow)
    updated_at: Mapped[datetime] = ts_column(default=utcnow, onupdate=utcnow)


class ToolExecution(Base):
    __tablename__ = "tool_executions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True,
                                    default=lambda: new_id("tx"))
    tool_name: Mapped[str] = mapped_column(String(128), index=True)
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    conversation_id: Mapped[str | None] = mapped_column(
        ForeignKey("conversations.id", ondelete="SET NULL"), nullable=True, index=True
    )
    message_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    task_execution_id: Mapped[str | None] = mapped_column(
        ForeignKey("task_executions.id", ondelete="SET NULL"), nullable=True, index=True
    )

    arguments: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[ExecutionStatus] = mapped_column(
        String(32), default=ExecutionStatus.RUNNING, index=True
    )
    permission_decision: Mapped[PermissionMode | None] = mapped_column(
        String(16), nullable=True
    )
    confirmation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    result: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration_ms: Mapped[float | None] = mapped_column(Float, nullable=True)

    #: Populated when a tool can describe how to undo itself. Unused in Phase 1
    #: (no destructive tools ship yet) but the reversibility model in the audit
    #: depends on the column existing before those tools arrive.
    undo_token: Mapped[str | None] = mapped_column(String(255), nullable=True)

    created_at: Mapped[datetime] = ts_column(default=utcnow, index=True)
    finished_at: Mapped[datetime | None] = ts_column(nullable=True)

    task_execution: Mapped["TaskExecution | None"] = relationship(
        back_populates="tool_executions"
    )


class PermissionGrant(Base):
    """A (capability, resource_scope) -> mode rule.

    Matching is most-specific-wins; see :mod:`jarvis.permissions.engine`.
    """

    __tablename__ = "permissions"
    __table_args__ = (
        Index("ix_permissions_lookup", "user_id", "capability", "revoked_at"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True,
                                    default=lambda: new_id("perm"))
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    capability: Mapped[Capability] = mapped_column(String(32))
    #: Glob over the resource the capability applies to. ``*`` matches all.
    #: For tools the resource is ``tool:<name>``.
    resource_scope: Mapped[str] = mapped_column(String(500), default="*")
    mode: Mapped[PermissionMode] = mapped_column(String(16), default=PermissionMode.ASK)
    #: Additional gates evaluated by the engine, e.g. ``{"max_risk": "MEDIUM"}``.
    conditions: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    granted_at: Mapped[datetime] = ts_column(default=utcnow)
    expires_at: Mapped[datetime | None] = ts_column(nullable=True)
    revoked_at: Mapped[datetime | None] = ts_column(nullable=True)


class Confirmation(Base):
    """A pending human decision.

    Persisted rather than held in memory so a restart does not silently drop a
    half-approved action, and so the same record serves the API, the UI, and
    (later) a push notification.
    """

    __tablename__ = "confirmations"

    id: Mapped[str] = mapped_column(String(64), primary_key=True,
                                    default=lambda: new_id("confirm"))
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    conversation_id: Mapped[str | None] = mapped_column(String(64), nullable=True,
                                                        index=True)

    title: Mapped[str] = mapped_column(String(500))
    body: Mapped[str] = mapped_column(Text, default="")
    #: What is being asked for, so the UI can render specifics and the executor
    #: can verify the approval matches the action actually performed.
    action: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    risk_level: Mapped[RiskLevel] = mapped_column(String(16), default=RiskLevel.MEDIUM)
    reversible: Mapped[bool] = mapped_column(Boolean, default=True)

    status: Mapped[ConfirmationStatus] = mapped_column(
        String(16), default=ConfirmationStatus.PENDING, index=True
    )
    decided_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    decision_note: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = ts_column(default=utcnow, index=True)
    expires_at: Mapped[datetime] = ts_column(default=utcnow)
    decided_at: Mapped[datetime | None] = ts_column(nullable=True)


class ActivityLog(Base):
    """Append-only activity feed. Never updated, never deleted in normal use."""

    __tablename__ = "activity_logs"
    __table_args__ = (
        Index("ix_activity_request_created", "request_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True,
                                    default=lambda: new_id("act"))
    kind: Mapped[ActivityKind] = mapped_column(String(48), index=True)
    #: Who caused it — ``user``, ``orchestrator``, ``tool:<name>``, ``agent:<key>``.
    actor: Mapped[str] = mapped_column(String(64), default="system")
    summary: Mapped[str] = mapped_column(String(1000), default="")
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    conversation_id: Mapped[str | None] = mapped_column(String(64), nullable=True,
                                                        index=True)
    task_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    execution_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tool_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    provider: Mapped[str | None] = mapped_column(String(64), nullable=True)
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)

    status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    duration_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)

    created_at: Mapped[datetime] = ts_column(default=utcnow, index=True)
