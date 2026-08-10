"""Tool interface.

Every capability JARVIS can perform is eventually a tool: filesystem access,
browser navigation, screenshots, shell commands, and the safe built-ins that
ship in Phase 1. They all present the same shape, so the executor, the
permission engine, the activity log, and the UI need no per-tool special cases.

A tool carries three kinds of metadata, and the separation matters:

* **Schema** — name, description, JSON Schema. This is all the model ever sees.
* **Policy** — capability, risk level, reversibility, confirmation. This is
  what the permission engine reads. The model never sees it and cannot
  influence it.
* **Handler** — the implementation, invoked only after a permission decision.

Keeping policy out of the model's view is deliberate: a tool's risk rating must
not be something a prompt injection can argue with.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, TYPE_CHECKING

from jarvis.db.models import Capability, RiskLevel
from jarvis.providers.base import ToolSpec

if TYPE_CHECKING:  # avoid a cycle: services import tools, tools type-hint them
    from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(slots=True)
class ToolContext:
    """Everything a handler is allowed to reach.

    Passing a context object rather than globals keeps handlers testable and
    makes the blast radius of a tool explicit — a tool can only touch what is
    on here.
    """

    user_id: str
    session: "AsyncSession"
    request_id: str | None = None
    conversation_id: str | None = None
    task_execution_id: str | None = None
    #: Set when untrusted content is in scope. Handlers may tighten their own
    #: behaviour; the permission engine already escalates on it.
    tainted: bool = False
    extras: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ToolResult:
    """What a handler returns.

    ``content`` is what goes back to the model — it must be a string, because
    that is what the tool_result block carries. ``data`` is the structured form
    for the UI and the activity log, which should not have to re-parse prose.
    """

    content: str
    data: dict[str, Any] | None = None
    is_error: bool = False
    #: Opaque token a future undo mechanism can use. Phase 1 tools are all
    #: reversible-by-nature so nothing sets it yet.
    undo_token: str | None = None

    @classmethod
    def ok(cls, content: str, **data: Any) -> "ToolResult":
        return cls(content=content, data=data or None)

    @classmethod
    def error(cls, content: str, **data: Any) -> "ToolResult":
        return cls(content=content, data=data or None, is_error=True)


ToolHandler = Callable[..., Awaitable[ToolResult]]


@dataclass(slots=True)
class Tool:
    name: str
    description: str
    #: JSON Schema for the arguments object.
    parameters: dict[str, Any]
    handler: ToolHandler

    # ── policy ───────────────────────────────────────────────────────────────
    capability: Capability = Capability.READ
    risk_level: RiskLevel = RiskLevel.NONE
    requires_confirmation: bool = False
    reversible: bool = True
    enabled: bool = True

    # ── metadata ─────────────────────────────────────────────────────────────
    version: str = "1"
    #: Grouping for the UI ("system", "tasks", "files", ...).
    category: str = "general"
    #: Human-readable template for the confirmation dialog. ``{args}`` is
    #: substituted with the formatted arguments.
    confirmation_template: str | None = None

    def to_provider_spec(self) -> ToolSpec:
        """The model-facing view. Policy fields are deliberately absent."""
        return ToolSpec(
            name=self.name,
            description=self.description,
            input_schema=self.parameters,
        )

    @property
    def resource(self) -> str:
        """Resource identifier used in permission matching."""
        return f"tool:{self.name}"

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
            "capability": self.capability.value,
            "risk_level": self.risk_level.value,
            "requires_confirmation": self.requires_confirmation,
            "reversible": self.reversible,
            "enabled": self.enabled,
            "category": self.category,
            "version": self.version,
        }

    def confirmation_text(self, arguments: dict[str, Any]) -> str:
        if self.confirmation_template:
            try:
                return self.confirmation_template.format(**arguments, args=arguments)
            except (KeyError, IndexError):
                pass  # fall through to the generic description
        pretty = ", ".join(f"{k}={v!r}" for k, v in arguments.items()) or "no arguments"
        return f"{self.description}\n\nArguments: {pretty}"

    def validate_handler(self) -> None:
        """Fail at registration, not at first use, if the handler is wrong."""
        if not inspect.iscoroutinefunction(self.handler):
            raise TypeError(
                f"Tool '{self.name}' handler must be an async function"
            )
        params = inspect.signature(self.handler).parameters
        if "ctx" not in params:
            raise TypeError(
                f"Tool '{self.name}' handler must accept a 'ctx' parameter"
            )


def tool(
    name: str,
    description: str,
    *,
    parameters: dict[str, Any] | None = None,
    capability: Capability = Capability.READ,
    risk_level: RiskLevel = RiskLevel.NONE,
    requires_confirmation: bool = False,
    reversible: bool = True,
    category: str = "general",
    confirmation_template: str | None = None,
) -> Callable[[ToolHandler], Tool]:
    """Decorator turning an async function into a :class:`Tool`."""

    def decorator(func: ToolHandler) -> Tool:
        built = Tool(
            name=name,
            description=description,
            parameters=parameters
            or {"type": "object", "properties": {}, "additionalProperties": False},
            handler=func,
            capability=capability,
            risk_level=risk_level,
            requires_confirmation=requires_confirmation,
            reversible=reversible,
            category=category,
            confirmation_template=confirmation_template,
        )
        built.validate_handler()
        return built

    return decorator
