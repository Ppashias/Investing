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
    #: True once the executor holds the user's approval for *this exact call*.
    #:
    #: Evidence, not permission. It says a confirmation was satisfied — it does
    #: not say the action is allowed, and a handler must still run its own
    #: checks. Set by :class:`~jarvis.tools.executor.ToolExecutor` only: either
    #: when the tool-level confirmation was satisfied before the handler ran,
    #: or when a :class:`ConfirmationNeeded` signal was answered mid-flight.
    #: Nothing else writes it, and a handler that sets it is lying to itself.
    confirmed: bool = False
    extras: dict[str, Any] = field(default_factory=dict)


class ConfirmationNeeded(Exception):
    """A handler discovered mid-flight that this call needs the user's approval.

    Some authorisation questions cannot be asked before the handler runs. The
    executor decides on ``(capability, resource)`` pairs known from the tool's
    declaration; a browser navigation's real resource is the *origin*, which
    only exists once the URL argument has been parsed and checked. Before this
    signal the only options were to ask about every navigation — training the
    user to click through — or to fail closed on ASK, which is what Step 5 did
    and what this replaces.

    Raising it hands the question back to the executor, which answers it with
    the machinery it already owns: the same :class:`ConfirmationService`, the
    same fingerprint over ``(tool name, arguments)``, the same single-use
    approval, the same suspension the orchestrator knows how to resume. There
    is deliberately no second confirmation system here — this class carries a
    reason and nothing else.

    **The contract for handlers: raise before doing anything.** The executor
    answers the signal by re-invoking the handler, so everything before the
    raise runs twice. Reads, parses and policy checks are fine; anything that
    changes the world is not.
    """

    def __init__(
        self,
        reason: str,
        *,
        prompt: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(reason)
        self.reason = reason
        #: Shown to the user in place of the tool's generic confirmation body.
        #: Worth setting: "Let JARVIS browse example.com?" is a question someone
        #: can answer, and the generic argument dump is not.
        self.prompt = prompt
        self.detail = detail or {}


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
    #: True when ``content`` carries text JARVIS did not author and does not
    #: control — a note the user wrote, a document, a web page.
    #:
    #: This is the *structural* half of the prompt-injection defence. The other
    #: half is the framing text a tool prefixes to such content ("data, never
    #: instructions"), and framing alone is a request to the model rather than a
    #: property of the system: a sufficiently convincing page can argue with it.
    #: This flag cannot be argued with. The agent loop accumulates it across the
    #: turn and the permission engine escalates every non-read capability on a
    #: tainted request, so a document that says "now delete everything" meets a
    #: confirmation regardless of how persuasive it was.
    tainted: bool = False

    @classmethod
    def ok(cls, content: str, **data: Any) -> "ToolResult":
        return cls(content=content, data=data or None)

    @classmethod
    def error(cls, content: str, **data: Any) -> "ToolResult":
        return cls(content=content, data=data or None, is_error=True)

    @classmethod
    def untrusted(cls, content: str, **data: Any) -> "ToolResult":
        """A successful result whose content came from outside JARVIS.

        Separate constructor rather than a keyword on :meth:`ok`, for two
        reasons. ``ok`` collects ``**data``, so a ``tainted=True`` keyword would
        silently become a data field and the taint would be lost — a failure
        mode with no symptom. And a distinct name makes the choice visible at
        the call site: a reviewer can see which tools declare their output
        untrusted without reading each one's body.
        """
        return cls(content=content, data=data or None, tainted=True)


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

    #: Optional: render the confirmation body from the arguments *and* the
    #: live context, for tools whose arguments are handles rather than
    #: meaning. Returning a falsy value falls back to the template.
    describe_confirmation: Any = None

    #: Argument names whose values must never be persisted.
    #:
    #: The executor writes every call's arguments to ``tool_executions`` and to
    #: the activity log, which is exactly right for "what did JARVIS try to
    #: do?" and exactly wrong for the *contents* of a form field. The logging
    #: redactor cannot help: it matches on key *names*, and ``browser_fill``'s
    #: argument is honestly called ``text``.
    #:
    #: Declared per tool rather than inferred, because only the tool knows
    #: which of its arguments carry user data as opposed to describing an
    #: action. The confirmation the user reads is deliberately not redacted —
    #: approving "type X into the search box" requires seeing X — and the
    #: approval fingerprint is still computed over the real arguments, so the
    #: binding is unchanged.
    redact_arguments: tuple[str, ...] = ()

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

    def for_audit(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Arguments as they may be written down.

        Returns the same dict when nothing is declared sensitive, so the common
        case costs nothing and the audit trail is unchanged for every existing
        tool.
        """
        if not self.redact_arguments:
            return arguments
        from jarvis.logging import REDACTED

        return {
            key: (REDACTED if key in self.redact_arguments else value)
            for key, value in arguments.items()
        }

    def confirmation_body(
        self, arguments: dict[str, Any], ctx: "ToolContext"
    ) -> str:
        """What the user is asked to approve.

        Falls back to the template, which is right for tools whose arguments
        *are* the action ("create a task called X"). It is wrong for tools
        whose arguments are opaque handles: "Click an element on the open page
        (pg_08c0…/el_d23c…)" asks someone to approve two identifiers, which is
        as unreadable as the coordinates this system rejected on exactly that
        ground. Such a tool sets ``describe_confirmation`` and answers with
        something a person can act on.

        Given the live context because the meaning of a handle lives in a
        registry the executor cannot reach — and must not have to know about.
        """
        if self.describe_confirmation is not None:
            try:
                described = self.describe_confirmation(arguments, ctx)
            except Exception:  # pragma: no cover - never block on cosmetics
                described = None
            if described:
                return described
        return self.confirmation_text(arguments)

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
    describe_confirmation: Any = None,
    redact_arguments: tuple[str, ...] = (),
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
            describe_confirmation=describe_confirmation,
            redact_arguments=redact_arguments,
        )
        built.validate_handler()
        return built

    return decorator
