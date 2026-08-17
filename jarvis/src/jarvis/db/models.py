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
    LargeBinary,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from jarvis.db.base import Base, EnumType, new_id, ts_column, utcnow
from jarvis.knowledge.types import (
    ChunkKind,
    DocumentStatus,
    SourceKind,
    SyncDirection,
    SyncStatus,
)
from jarvis.memory.types import (
    MemoryRelation,
    MemorySource,
    MemoryStatus,
    MemoryType,
    RevisionKind,
)


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
    MEMORY_CAPTURED = "MEMORY_CAPTURED"
    COMPUTER_ACTION = "COMPUTER_ACTION"
    COMPUTER_TASK = "COMPUTER_TASK"
    EMERGENCY_STOP = "EMERGENCY_STOP"
    MEMORY_RETRIEVED = "MEMORY_RETRIEVED"
    KNOWLEDGE_INGESTED = "KNOWLEDGE_INGESTED"
    #: Anything JARVIS did to an external knowledge vault — connect, search,
    #: read, create, update, delete, sync, and the refusals. Separate from
    #: KNOWLEDGE_INGESTED so "what did JARVIS do to my Obsidian vault?" is one
    #: filter rather than a guess.
    OBSIDIAN_ACTION = "OBSIDIAN_ACTION"
    #: Anything JARVIS did in its browser — navigate, inspect, extract, click,
    #: fill, and the refusals. Alongside COMPUTER_ACTION and OBSIDIAN_ACTION so
    #: "what did JARVIS do in the browser?" is one filter rather than a guess.
    #: Stored through EnumType on a String column, so adding it needs no
    #: migration.
    BROWSER_ACTION = "BROWSER_ACTION"
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
    role: Mapped[MessageRole] = mapped_column(EnumType(MessageRole))
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
    status: Mapped[TaskStatus] = mapped_column(
        EnumType(TaskStatus), default=TaskStatus.TODO, index=True
    )
    priority: Mapped[TaskPriority] = mapped_column(
        EnumType(TaskPriority), default=TaskPriority.NORMAL
    )
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
        EnumType(ExecutionStatus), default=ExecutionStatus.RUNNING, index=True
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
    capability: Mapped[Capability] = mapped_column(
        EnumType(Capability), default=Capability.READ
    )
    risk_level: Mapped[RiskLevel] = mapped_column(
        EnumType(RiskLevel), default=RiskLevel.NONE
    )
    requires_confirmation: Mapped[bool] = mapped_column(Boolean, default=False)
    reversible: Mapped[bool] = mapped_column(Boolean, default=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    #: Operator override of the engine's computed decision. NULL means "no
    #: override" — the engine's own evaluation stands.
    mode_override: Mapped[PermissionMode | None] = mapped_column(
        EnumType(PermissionMode), nullable=True
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
        EnumType(ExecutionStatus), default=ExecutionStatus.RUNNING, index=True
    )
    permission_decision: Mapped[PermissionMode | None] = mapped_column(
        EnumType(PermissionMode), nullable=True
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
    capability: Mapped[Capability] = mapped_column(EnumType(Capability))
    #: Glob over the resource the capability applies to. ``*`` matches all.
    #: For tools the resource is ``tool:<name>``.
    resource_scope: Mapped[str] = mapped_column(String(500), default="*")
    mode: Mapped[PermissionMode] = mapped_column(
        EnumType(PermissionMode), default=PermissionMode.ASK
    )
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
    risk_level: Mapped[RiskLevel] = mapped_column(
        EnumType(RiskLevel), default=RiskLevel.MEDIUM
    )
    reversible: Mapped[bool] = mapped_column(Boolean, default=True)

    status: Mapped[ConfirmationStatus] = mapped_column(
        EnumType(ConfirmationStatus), default=ConfirmationStatus.PENDING, index=True
    )
    decided_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    decision_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: How far the action reaches, in one word — see
    #: :mod:`jarvis.permissions.impact`. Stored rather than re-derived so the
    #: record says what the user was actually shown, even if a tool's
    #: classification changes later.
    impact: Mapped[str] = mapped_column(String(16), default="write")
    #: How the decision arrived: ``ui``, ``api``, ``voice``, ``system``.
    #:
    #: `vierisid/jarvis` carries this on its audit rows and it is worth
    #: copying: "who approved this, and through what?" is a forensic question
    #: whose answer must not depend on correlating timestamps. It also makes
    #: the voice rule enforceable — a destructive action must never be
    #: resolvable by a spoken yes, and that rule needs somewhere to record
    #: which channel *did* resolve it.
    resolution_channel: Mapped[str | None] = mapped_column(
        String(16), nullable=True
    )

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
    kind: Mapped[ActivityKind] = mapped_column(EnumType(ActivityKind), index=True)
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


# ── Phase 2: memory, projects, knowledge ─────────────────────────────────────
#
# Three groups of tables, added together because they reference each other:
#
# * ``projects`` — Phase 1 threaded a ``project_id`` through tasks and
#   conversations without anything to point at. It has a table now (§27).
# * ``memories`` and friends — structured, versioned, correctable memory.
# * ``documents`` / ``document_chunks`` / ``knowledge_sources`` — ingested
#   external knowledge, kept deliberately separate from personal memory (§8).
#
# ``embeddings`` serves both memories and chunks through an owner reference
# rather than two near-identical tables. One table means one place to re-embed
# when the embedding model changes, which is the operation most likely to be
# needed and most annoying to do twice.


class ProjectStatus(str, enum.Enum):
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    COMPLETED = "COMPLETED"
    ARCHIVED = "ARCHIVED"


class EmbeddingOwner(str, enum.Enum):
    MEMORY = "MEMORY"
    CHUNK = "CHUNK"


class Project(Base):
    """First-class project context (§27).

    ``key`` is the human handle — "project-x" — and is what natural language
    resolves against, so "continue working on Project X" can find the project
    without the user knowing an opaque id.
    """

    __tablename__ = "projects"
    __table_args__ = (
        Index("uq_project_user_key", "user_id", "key", unique=True),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True,
                                    default=lambda: new_id("proj"))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    key: Mapped[str] = mapped_column(String(128))
    name: Mapped[str] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[ProjectStatus] = mapped_column(
        EnumType(ProjectStatus), default=ProjectStatus.ACTIVE, index=True
    )
    #: Free-form "where things stand" text. Distinct from PROJECT_STATE
    #: memories: this is the single current summary, those are the individual
    #: remembered facts that produced it.
    current_state: Mapped[str | None] = mapped_column(Text, nullable=True)
    goals: Mapped[list[str]] = mapped_column(JSON, default=list)
    tags: Mapped[list[str]] = mapped_column(JSON, default=list)
    meta: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, default=dict)

    created_at: Mapped[datetime] = ts_column(default=utcnow)
    updated_at: Mapped[datetime] = ts_column(default=utcnow, onupdate=utcnow)
    archived_at: Mapped[datetime | None] = ts_column(nullable=True)


class Memory(Base):
    """A single remembered thing.

    ``subject`` is the field that makes §15 and §16 tractable. It is a short
    normalised noun phrase — "interface theme preference", "Project X engine
    version" — naming *what the memory is about* rather than what it says.
    Two memories with the same subject and different content are a candidate
    contradiction; two with the same subject and equivalent content are a
    candidate duplicate. Without it, both checks reduce to hoping an embedding
    similarity threshold happens to separate "I prefer dark mode" from "I no
    longer prefer dark mode" — which it does not, because those two sentences
    are nearly identical in vector space and opposite in meaning.
    """

    __tablename__ = "memories"
    __table_args__ = (
        Index("ix_memory_user_status_type", "user_id", "status", "type"),
        Index("ix_memory_user_subject", "user_id", "subject"),
        Index("ix_memory_project", "project_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True,
                                    default=lambda: new_id("mem"))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)

    type: Mapped[MemoryType] = mapped_column(EnumType(MemoryType), index=True)
    status: Mapped[MemoryStatus] = mapped_column(
        EnumType(MemoryStatus), default=MemoryStatus.ACTIVE, index=True
    )

    #: The memory itself, in natural language, written to stand alone. A
    #: memory that only makes sense next to the message that produced it is
    #: useless six months later.
    content: Mapped[str] = mapped_column(Text)
    #: Short form used when the retrieval budget is tight.
    summary: Mapped[str | None] = mapped_column(String(500), nullable=True)
    subject: Mapped[str | None] = mapped_column(String(255), nullable=True)

    source: Mapped[MemorySource] = mapped_column(
        EnumType(MemorySource), default=MemorySource.CONVERSATION, index=True
    )
    #: Serialised :class:`jarvis.knowledge.types.SourceRef`. Carries the
    #: Obsidian note identity when the source is a vault (§38).
    source_ref: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    #: True when the content originated outside the user or JARVIS. Retrieval
    #: propagates this to the request so the permission engine escalates.
    tainted: Mapped[bool] = mapped_column(Boolean, default=False)

    confidence: Mapped[float] = mapped_column(Float, default=0.55)
    importance: Mapped[float] = mapped_column(Float, default=0.5)
    #: Protected from automatic pruning and supersession.
    pinned: Mapped[bool] = mapped_column(Boolean, default=False)

    project_id: Mapped[str | None] = mapped_column(
        ForeignKey("projects.id"), nullable=True, index=True
    )
    conversation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    task_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    tags: Mapped[list[str]] = mapped_column(JSON, default=list)
    meta: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, default=dict)

    #: Set when this memory has been replaced. The replacement points back via
    #: a SUPERSEDES link, so the chain is walkable in both directions.
    superseded_by: Mapped[str | None] = mapped_column(String(64), nullable=True)

    revision: Mapped[int] = mapped_column(Integer, default=1)
    access_count: Mapped[int] = mapped_column(Integer, default=0)

    created_at: Mapped[datetime] = ts_column(default=utcnow, index=True)
    updated_at: Mapped[datetime] = ts_column(default=utcnow, onupdate=utcnow)
    last_accessed_at: Mapped[datetime | None] = ts_column(nullable=True)
    #: Working memory expires; long-term memory does not. Enforced at
    #: retrieval, so an expired memory stops influencing answers immediately
    #: rather than at the next sweep.
    expires_at: Mapped[datetime | None] = ts_column(nullable=True, index=True)


class MemoryRevision(Base):
    """Append-only history of what happened to a memory (§11, §16).

    Corrections must be reversible and auditable: "you changed what you
    remembered about X — what did it say before?" is a question the user is
    entitled to an answer to.
    """

    __tablename__ = "memory_revisions"
    __table_args__ = (Index("ix_memrev_memory_created", "memory_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True,
                                    default=lambda: new_id("memrev"))
    memory_id: Mapped[str] = mapped_column(
        ForeignKey("memories.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[RevisionKind] = mapped_column(EnumType(RevisionKind))
    #: Who did it: ``user``, ``evaluator``, ``agent:<key>``.
    actor: Mapped[str] = mapped_column(String(64), default="system")
    #: Field-level before/after. Empty for events that change nothing.
    changes: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = ts_column(default=utcnow, index=True)


class MemoryLink(Base):
    """Typed edge between two memories."""

    __tablename__ = "memory_links"
    __table_args__ = (
        Index("uq_memlink", "from_memory_id", "to_memory_id", "relation", unique=True),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True,
                                    default=lambda: new_id("memlink"))
    from_memory_id: Mapped[str] = mapped_column(
        ForeignKey("memories.id", ondelete="CASCADE"), index=True
    )
    to_memory_id: Mapped[str] = mapped_column(
        ForeignKey("memories.id", ondelete="CASCADE"), index=True
    )
    relation: Mapped[MemoryRelation] = mapped_column(EnumType(MemoryRelation))
    #: Similarity or model confidence that produced the edge, where relevant.
    strength: Mapped[float] = mapped_column(Float, default=1.0)
    note: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = ts_column(default=utcnow)


class Embedding(Base):
    """A vector for a memory or a document chunk.

    Stored as a little-endian float32 blob with its norm precomputed. The norm
    is stored because cosine similarity needs it on every comparison and
    recomputing it per query turns an O(n) dot product into an O(n) dot product
    plus an O(n) square root.

    ``model`` and ``dim`` are on the row, not in configuration, because vectors
    from different models are not comparable. Retrieval filters on the active
    model, so switching models degrades to "nothing matches yet" rather than
    returning confident nonsense.
    """

    __tablename__ = "embeddings"
    __table_args__ = (
        Index("uq_embedding_owner", "owner_kind", "owner_id", "model", unique=True),
        Index("ix_embedding_model_owner", "model", "owner_kind"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True,
                                    default=lambda: new_id("emb"))
    owner_kind: Mapped[EmbeddingOwner] = mapped_column(EnumType(EmbeddingOwner))
    owner_id: Mapped[str] = mapped_column(String(64), index=True)
    model: Mapped[str] = mapped_column(String(128), index=True)
    dim: Mapped[int] = mapped_column(Integer)
    vector: Mapped[bytes] = mapped_column(LargeBinary)
    norm: Mapped[float] = mapped_column(Float, default=1.0)
    created_at: Mapped[datetime] = ts_column(default=utcnow)


class KnowledgeSource(Base):
    """A registered knowledge provider instance (§30).

    One row per configured source, holding *non-secret* configuration only —
    credentials go through :mod:`jarvis.secrets` like everything else, and this
    table is read by the API.
    """

    __tablename__ = "knowledge_sources"
    __table_args__ = (
        Index("uq_knowledge_source_user_key", "user_id", "key", unique=True),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True,
                                    default=lambda: new_id("ksrc"))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    #: Stable handle, e.g. ``local:docs`` or (later) ``obsidian:main-vault``.
    key: Mapped[str] = mapped_column(String(128))
    kind: Mapped[SourceKind] = mapped_column(EnumType(SourceKind), index=True)
    name: Mapped[str] = mapped_column(String(255))
    #: Provider settings. Never credentials.
    config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    sync_direction: Mapped[SyncDirection] = mapped_column(
        EnumType(SyncDirection), default=SyncDirection.NONE
    )
    sync_status: Mapped[SyncStatus] = mapped_column(
        EnumType(SyncStatus), default=SyncStatus.NEVER_SYNCED
    )
    last_synced_at: Mapped[datetime | None] = ts_column(nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    document_count: Mapped[int] = mapped_column(Integer, default=0)

    created_at: Mapped[datetime] = ts_column(default=utcnow)
    updated_at: Mapped[datetime] = ts_column(default=utcnow, onupdate=utcnow)


class Document(Base):
    """An ingested document.

    ``content_hash`` drives re-ingestion: a document whose bytes have not
    changed is not re-chunked or re-embedded, which is what makes rescanning a
    folder cheap enough to do routinely.
    """

    __tablename__ = "documents"
    __table_args__ = (
        Index("ix_document_user_status", "user_id", "status"),
        Index("ix_document_source", "source_kind", "uri"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True,
                                    default=lambda: new_id("doc"))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    source_id: Mapped[str | None] = mapped_column(
        ForeignKey("knowledge_sources.id"), nullable=True, index=True
    )

    title: Mapped[str] = mapped_column(String(500))
    source_kind: Mapped[SourceKind] = mapped_column(EnumType(SourceKind), index=True)
    #: Where it came from — path, URL, or vault-relative note path.
    uri: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    source_ref: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    media_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    byte_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True,
                                                     index=True)

    status: Mapped[DocumentStatus] = mapped_column(
        EnumType(DocumentStatus), default=DocumentStatus.REGISTERED, index=True
    )
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    #: Every ingested document is untrusted input. A PDF can contain "ignore
    #: your instructions" as easily as a web page (§42), so this defaults to
    #: true rather than being set per source.
    tainted: Mapped[bool] = mapped_column(Boolean, default=True)

    project_id: Mapped[str | None] = mapped_column(
        ForeignKey("projects.id"), nullable=True, index=True
    )
    tags: Mapped[list[str]] = mapped_column(JSON, default=list)
    meta: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, default=dict)

    chunk_count: Mapped[int] = mapped_column(Integer, default=0)
    token_estimate: Mapped[int] = mapped_column(Integer, default=0)

    created_at: Mapped[datetime] = ts_column(default=utcnow, index=True)
    updated_at: Mapped[datetime] = ts_column(default=utcnow, onupdate=utcnow)
    indexed_at: Mapped[datetime | None] = ts_column(nullable=True)

    chunks: Mapped[list["DocumentChunk"]] = relationship(
        back_populates="document", cascade="all, delete-orphan"
    )


class DocumentChunk(Base):
    """One retrievable piece of a document.

    ``heading_path`` is kept per chunk rather than derived at query time
    because it is what makes a retrieved fragment interpretable: "Storage" from
    an architecture document means something; 400 characters from offset 8,000
    does not.
    """

    __tablename__ = "document_chunks"
    __table_args__ = (
        Index("ix_chunk_document_ordinal", "document_id", "ordinal"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True,
                                    default=lambda: new_id("chunk"))
    document_id: Mapped[str] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), index=True
    )
    ordinal: Mapped[int] = mapped_column(Integer)
    kind: Mapped[ChunkKind] = mapped_column(EnumType(ChunkKind),
                                            default=ChunkKind.PROSE)
    content: Mapped[str] = mapped_column(Text)
    #: ``Architecture > Storage > Vectors``.
    heading_path: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    char_start: Mapped[int] = mapped_column(Integer, default=0)
    char_end: Mapped[int] = mapped_column(Integer, default=0)
    token_estimate: Mapped[int] = mapped_column(Integer, default=0)
    meta: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, default=dict)
    created_at: Mapped[datetime] = ts_column(default=utcnow)

    document: Mapped["Document"] = relationship(back_populates="chunks")


# ── Phase 3: computer control ────────────────────────────────────────────────
#
# Two tables. ``computer_actions`` is the audit trail §25/§26 require: one row
# per attempted action, written whatever the outcome — including denied and
# aborted — because "what did JARVIS try to do?" matters as much as what it
# managed to do. ``computer_tasks`` is §11's task model.
#
# The audit table is append-only by construction, not by policy: nothing in the
# codebase updates a row after the executor writes it, no API route edits or
# deletes one, and no tool is registered that could. §26 asks that the log be
# "difficult for the agent itself to silently alter", and the enforcement is
# that the capability does not exist rather than that permission is withheld.


class ComputerAudit(Base):
    """One attempted computer action. Append-only."""

    __tablename__ = "computer_actions"
    __table_args__ = (
        Index("ix_computer_action_user_created", "user_id", "created_at"),
        Index("ix_computer_action_task", "task_id", "sequence"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True,
                                    default=lambda: new_id("act"))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    task_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    #: Position within a task, so a replay reads in order even when two actions
    #: share a timestamp.
    sequence: Mapped[int] = mapped_column(Integer, default=0)

    kind: Mapped[str] = mapped_column(String(48), index=True)
    scope: Mapped[str] = mapped_column(String(32), index=True)
    #: Human-readable summary. Never contains typed text or file content — the
    #: audit log is displayed and exported, and duplicating a password into it
    #: would defeat the point of not storing one.
    summary: Mapped[str] = mapped_column(String(1000), default="")
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Redacted parameters. Content fields are replaced with their length.
    params: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    risk: Mapped[str] = mapped_column(String(16), index=True)
    decision: Mapped[str] = mapped_column(String(16))
    decision_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    applied_rules: Mapped[list[str]] = mapped_column(JSON, default=list)
    confirmation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    confirmed_by: Mapped[str | None] = mapped_column(String(64), nullable=True)

    outcome: Mapped[str] = mapped_column(String(32), index=True)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration_ms: Mapped[float | None] = mapped_column(Float, nullable=True)

    verification: Mapped[str] = mapped_column(String(24), default="UNVERIFIED")
    verification_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Screenshot ids before and after — references, not images.
    observation_before: Mapped[str | None] = mapped_column(String(64), nullable=True)
    observation_after: Mapped[str | None] = mapped_column(String(64), nullable=True)

    tainted: Mapped[bool] = mapped_column(Boolean, default=False)
    actor: Mapped[str] = mapped_column(String(64), default="agent")
    created_at: Mapped[datetime] = ts_column(default=utcnow, index=True)


class ComputerTask(Base):
    """A multi-step computer objective (§11)."""

    __tablename__ = "computer_tasks"
    __table_args__ = (Index("ix_computer_task_user_status", "user_id", "status"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True,
                                    default=lambda: new_id("ctask"))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    conversation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    project_id: Mapped[str | None] = mapped_column(
        ForeignKey("projects.id"), nullable=True
    )

    description: Mapped[str] = mapped_column(String(1000))
    objective: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), default="PENDING", index=True)
    current_step: Mapped[str | None] = mapped_column(String(500), nullable=True)

    #: Counters rather than embedded lists: the actions live in
    #: ``computer_actions``, and duplicating them here would create a second
    #: version of the audit trail that could disagree with the first.
    step_count: Mapped[int] = mapped_column(Integer, default=0)
    completed_actions: Mapped[int] = mapped_column(Integer, default=0)
    failed_actions: Mapped[int] = mapped_column(Integer, default=0)
    max_steps: Mapped[int] = mapped_column(Integer, default=25)

    #: Why the task is waiting, when it is (§29).
    waiting_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    result: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    risk: Mapped[str] = mapped_column(String(16), default="LOW")
    scopes: Mapped[list[str]] = mapped_column(JSON, default=list)
    observations: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)

    created_at: Mapped[datetime] = ts_column(default=utcnow, index=True)
    updated_at: Mapped[datetime] = ts_column(default=utcnow, onupdate=utcnow)
    started_at: Mapped[datetime | None] = ts_column(nullable=True)
    finished_at: Mapped[datetime | None] = ts_column(nullable=True)
    #: Wall-clock deadline for the whole task (§28).
    deadline_at: Mapped[datetime | None] = ts_column(nullable=True)
