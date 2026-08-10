"""Computer-control vocabulary.

A leaf module, like :mod:`jarvis.memory.types` — no database, no service
imports — so :mod:`jarvis.db.models` can depend on it.

The centrepiece is :class:`ComputerAction`: a **structured object**, never a
string. §8 is explicit that the model must not emit low-level commands, and the
enforcement mechanism is that there is no code path from model output to the
operating system that does not first become one of these, get classified, and
get a policy decision. A string like ``"rm -rf /"`` cannot be executed because
nothing executes strings.

## Risk is computed, not declared

An action's risk comes from :func:`classify_risk`, which reads the action's
*content* — not from a field the caller sets. A caller that could set its own
risk could set it to ``LOW``, and the whole policy layer would be advisory.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any


class ActionKind(str, enum.Enum):
    """Every operation the computer agent can perform.

    Declared in full even where this machine cannot perform them, because the
    *policy* for an action must exist before a backend does. Availability is a
    separate question answered by :mod:`jarvis.computer.capabilities`, and an
    unavailable action is refused explicitly rather than silently skipped.
    """

    # observation
    OBSERVE_SCREEN = "observe_screen"
    SCREENSHOT = "screenshot"
    GET_WINDOWS = "get_windows"
    GET_ACTIVE_WINDOW = "get_active_window"
    GET_CURSOR = "get_cursor"

    # mouse
    MOVE_MOUSE = "move_mouse"
    CLICK = "click"
    DOUBLE_CLICK = "double_click"
    RIGHT_CLICK = "right_click"
    DRAG = "drag"
    SCROLL = "scroll"

    # keyboard
    TYPE_TEXT = "type_text"
    PRESS_KEY = "press_key"
    HOTKEY = "hotkey"

    # windows and applications
    OPEN_APPLICATION = "open_application"
    CLOSE_APPLICATION = "close_application"
    FOCUS_WINDOW = "focus_window"

    # clipboard
    READ_CLIPBOARD = "read_clipboard"
    WRITE_CLIPBOARD = "write_clipboard"

    # filesystem
    READ_FILE = "read_file"
    WRITE_FILE = "write_file"
    LIST_DIRECTORY = "list_directory"
    CREATE_DIRECTORY = "create_directory"
    DELETE_PATH = "delete_path"
    MOVE_PATH = "move_path"

    # terminal
    EXECUTE_COMMAND = "execute_command"

    # control
    WAIT = "wait"


class ActionRisk(str, enum.Enum):
    """§12's classification. Ordered, unlike ``Capability``."""

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    #: Beyond HIGH: never permitted in Phase 3 by any configuration.
    PROHIBITED = "PROHIBITED"

    @property
    def rank(self) -> int:
        return _RISK_RANK[self]


_RISK_RANK = {
    ActionRisk.LOW: 0,
    ActionRisk.MEDIUM: 1,
    ActionRisk.HIGH: 2,
    ActionRisk.PROHIBITED: 3,
}


class ComputerScope(str, enum.Enum):
    """§16's granular permission scopes.

    Deliberately not one ``ALLOW_COMPUTER`` flag. Granting screen observation
    is a different decision from granting terminal execution, and a system that
    cannot express the difference will be configured with whichever setting
    unblocks the user.
    """

    SCREEN = "SCREEN"
    MOUSE = "MOUSE"
    KEYBOARD = "KEYBOARD"
    WINDOW = "WINDOW"
    APPLICATION = "APPLICATION"
    FILESYSTEM = "FILESYSTEM"
    TERMINAL = "TERMINAL"
    CLIPBOARD = "CLIPBOARD"
    NETWORK = "NETWORK"
    BROWSER = "BROWSER"
    COMMUNICATION = "COMMUNICATION"
    FINANCIAL = "FINANCIAL"
    SYSTEM_SETTINGS = "SYSTEM_SETTINGS"


class ComputerMode(str, enum.Enum):
    """§15's operating modes, ordered from most to least restrictive.

    The mode is a *ceiling*, not a grant: it caps what may happen without
    asking. It can never widen a scope the user has not enabled.
    """

    LOCKDOWN = "LOCKDOWN"
    SAFE = "SAFE"
    ASSISTED = "ASSISTED"
    AUTONOMOUS = "AUTONOMOUS"


class ComputerTaskStatus(str, enum.Enum):
    """§11."""

    PENDING = "PENDING"
    PLANNING = "PLANNING"
    RUNNING = "RUNNING"
    WAITING_FOR_USER = "WAITING_FOR_USER"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"

    @property
    def is_terminal(self) -> bool:
        return self in {
            ComputerTaskStatus.COMPLETED,
            ComputerTaskStatus.FAILED,
            ComputerTaskStatus.CANCELLED,
        }


class ActionOutcome(str, enum.Enum):
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    DENIED = "DENIED"
    AWAITING_CONFIRMATION = "AWAITING_CONFIRMATION"
    TIMED_OUT = "TIMED_OUT"
    #: Emergency stop was engaged before or during execution.
    ABORTED = "ABORTED"
    UNAVAILABLE = "UNAVAILABLE"


class VerificationOutcome(str, enum.Enum):
    """§9. ``UNVERIFIED`` is a real answer and must not be confused with
    success: it means nobody checked."""

    CONFIRMED = "CONFIRMED"
    CONTRADICTED = "CONTRADICTED"
    INCONCLUSIVE = "INCONCLUSIVE"
    UNVERIFIED = "UNVERIFIED"


#: Which scope each action needs. One action, one scope — an action that
#: plausibly needs two is a sign it should be two actions.
ACTION_SCOPE: dict[ActionKind, ComputerScope] = {
    ActionKind.OBSERVE_SCREEN: ComputerScope.SCREEN,
    ActionKind.SCREENSHOT: ComputerScope.SCREEN,
    ActionKind.GET_WINDOWS: ComputerScope.WINDOW,
    ActionKind.GET_ACTIVE_WINDOW: ComputerScope.WINDOW,
    ActionKind.GET_CURSOR: ComputerScope.SCREEN,
    ActionKind.MOVE_MOUSE: ComputerScope.MOUSE,
    ActionKind.CLICK: ComputerScope.MOUSE,
    ActionKind.DOUBLE_CLICK: ComputerScope.MOUSE,
    ActionKind.RIGHT_CLICK: ComputerScope.MOUSE,
    ActionKind.DRAG: ComputerScope.MOUSE,
    ActionKind.SCROLL: ComputerScope.MOUSE,
    ActionKind.TYPE_TEXT: ComputerScope.KEYBOARD,
    ActionKind.PRESS_KEY: ComputerScope.KEYBOARD,
    ActionKind.HOTKEY: ComputerScope.KEYBOARD,
    ActionKind.OPEN_APPLICATION: ComputerScope.APPLICATION,
    ActionKind.CLOSE_APPLICATION: ComputerScope.APPLICATION,
    ActionKind.FOCUS_WINDOW: ComputerScope.WINDOW,
    ActionKind.READ_CLIPBOARD: ComputerScope.CLIPBOARD,
    ActionKind.WRITE_CLIPBOARD: ComputerScope.CLIPBOARD,
    ActionKind.READ_FILE: ComputerScope.FILESYSTEM,
    ActionKind.WRITE_FILE: ComputerScope.FILESYSTEM,
    ActionKind.LIST_DIRECTORY: ComputerScope.FILESYSTEM,
    ActionKind.CREATE_DIRECTORY: ComputerScope.FILESYSTEM,
    ActionKind.DELETE_PATH: ComputerScope.FILESYSTEM,
    ActionKind.MOVE_PATH: ComputerScope.FILESYSTEM,
    ActionKind.EXECUTE_COMMAND: ComputerScope.TERMINAL,
    ActionKind.WAIT: ComputerScope.SCREEN,
}

#: Baseline risk before content is considered. ``classify_risk`` may raise it
#: — for example a delete of a directory, or a command matching a dangerous
#: pattern — but never lowers it.
BASE_RISK: dict[ActionKind, ActionRisk] = {
    ActionKind.OBSERVE_SCREEN: ActionRisk.LOW,
    ActionKind.SCREENSHOT: ActionRisk.LOW,
    ActionKind.GET_WINDOWS: ActionRisk.LOW,
    ActionKind.GET_ACTIVE_WINDOW: ActionRisk.LOW,
    ActionKind.GET_CURSOR: ActionRisk.LOW,
    ActionKind.WAIT: ActionRisk.LOW,
    ActionKind.MOVE_MOUSE: ActionRisk.LOW,
    ActionKind.SCROLL: ActionRisk.LOW,
    ActionKind.LIST_DIRECTORY: ActionRisk.LOW,
    ActionKind.READ_FILE: ActionRisk.LOW,
    ActionKind.GET_WINDOWS: ActionRisk.LOW,
    # Clicking is where observation becomes interference: a click lands on
    # whatever is under the cursor, which may not be what was observed.
    ActionKind.CLICK: ActionRisk.MEDIUM,
    ActionKind.DOUBLE_CLICK: ActionRisk.MEDIUM,
    ActionKind.RIGHT_CLICK: ActionRisk.MEDIUM,
    ActionKind.DRAG: ActionRisk.MEDIUM,
    ActionKind.TYPE_TEXT: ActionRisk.MEDIUM,
    ActionKind.PRESS_KEY: ActionRisk.MEDIUM,
    ActionKind.HOTKEY: ActionRisk.MEDIUM,
    ActionKind.OPEN_APPLICATION: ActionRisk.MEDIUM,
    ActionKind.FOCUS_WINDOW: ActionRisk.LOW,
    ActionKind.CLOSE_APPLICATION: ActionRisk.MEDIUM,
    ActionKind.WRITE_FILE: ActionRisk.MEDIUM,
    ActionKind.CREATE_DIRECTORY: ActionRisk.MEDIUM,
    ActionKind.MOVE_PATH: ActionRisk.MEDIUM,
    ActionKind.WRITE_CLIPBOARD: ActionRisk.MEDIUM,
    # Reading the clipboard is HIGH because of what is usually in it: §20
    # names passwords explicitly, and a password manager's paste buffer is the
    # normal case rather than the exotic one.
    ActionKind.READ_CLIPBOARD: ActionRisk.HIGH,
    ActionKind.DELETE_PATH: ActionRisk.HIGH,
    ActionKind.EXECUTE_COMMAND: ActionRisk.HIGH,
}


@dataclass(slots=True)
class ComputerAction:
    """One structured, policy-evaluable operation.

    ``reason`` is required by convention rather than by the type system: every
    tool that constructs an action supplies it, and it is what the confirmation
    dialog and the audit log show. "Click at (850, 430)" is not something a
    human can approve; "Click the Save button" is.
    """

    kind: ActionKind
    #: Action-specific arguments. Validated per-kind by the backend, and by a
    #: JSON Schema at the tool boundary before it ever gets here.
    params: dict[str, Any] = field(default_factory=dict)
    reason: str = ""
    #: Set by the agent when the action belongs to a multi-step task.
    task_id: str | None = None
    #: What the caller expects to be true afterwards, in natural language.
    #: Drives verification (§9).
    expectation: str | None = None
    #: True when the action was proposed from, or is influenced by, untrusted
    #: content. Forces confirmation regardless of mode (§32).
    tainted: bool = False

    @property
    def scope(self) -> ComputerScope:
        return ACTION_SCOPE[self.kind]

    def describe(self) -> str:
        """One line for a human. Used by the confirmation dialog and audit."""
        detail = _describe_params(self.kind, self.params)
        base = f"{self.kind.value}{f' {detail}' if detail else ''}"
        return f"{base} — {self.reason}" if self.reason else base

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "params": self.params,
            "reason": self.reason,
            "task_id": self.task_id,
            "expectation": self.expectation,
            "tainted": self.tainted,
            "scope": self.scope.value,
        }


@dataclass(slots=True)
class ActionResult:
    outcome: ActionOutcome
    action: ComputerAction
    #: Human-readable summary of what happened.
    detail: str = ""
    #: Structured return value — file content, window list, command output.
    data: dict[str, Any] = field(default_factory=dict)
    duration_ms: float = 0.0
    risk: ActionRisk = ActionRisk.LOW
    decision: str = ""
    verification: VerificationOutcome = VerificationOutcome.UNVERIFIED
    verification_detail: str = ""
    confirmation_id: str | None = None
    audit_id: str | None = None

    @property
    def ok(self) -> bool:
        return self.outcome is ActionOutcome.SUCCEEDED

    def to_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome.value,
            "action": self.action.to_dict(),
            "detail": self.detail,
            "data": self.data,
            "duration_ms": round(self.duration_ms, 2),
            "risk": self.risk.value,
            "decision": self.decision,
            "verification": self.verification.value,
            "verification_detail": self.verification_detail,
            "confirmation_id": self.confirmation_id,
            "audit_id": self.audit_id,
        }


@dataclass(slots=True)
class WindowInfo:
    id: str
    title: str
    width: int
    height: int
    x: int
    y: int
    application: str | None = None
    pid: int | None = None
    active: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "bounds": {"x": self.x, "y": self.y, "width": self.width,
                       "height": self.height},
            "application": self.application,
            "pid": self.pid,
            "active": self.active,
        }


@dataclass(slots=True)
class ScreenState:
    """The structured observation the reasoner sees (§5).

    Deliberately *not* just an image. The window list, geometry and cursor
    position are cheap, exact, and answer most questions without a single
    token of vision — which is the point of §5's "do not continuously send
    full-resolution screenshots".
    """

    width: int
    height: int
    windows: list[WindowInfo] = field(default_factory=list)
    active_window: WindowInfo | None = None
    cursor: tuple[int, int] | None = None
    #: Reference into the screenshot store, not the image itself.
    screenshot_id: str | None = None
    #: Set when the frame is byte-identical to the previous observation, so a
    #: caller can skip re-sending it to the model.
    unchanged: bool = False
    captured_at: str | None = None
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "screen": {"width": self.width, "height": self.height},
            "windows": [w.to_dict() for w in self.windows],
            "active_window": self.active_window.to_dict() if self.active_window else None,
            "cursor": {"x": self.cursor[0], "y": self.cursor[1]} if self.cursor else None,
            "screenshot_id": self.screenshot_id,
            "unchanged": self.unchanged,
            "captured_at": self.captured_at,
            "notes": self.notes,
        }

    def summarise(self) -> str:
        """Compact text for the model. This is what gets sent when nothing
        needs looking at pixel by pixel."""
        lines = [f"Screen {self.width}x{self.height}."]
        if self.active_window:
            w = self.active_window
            lines.append(
                f"Active window: {w.title!r} ({w.application or 'unknown app'}) "
                f"at {w.x},{w.y} size {w.width}x{w.height}."
            )
        else:
            lines.append("No active window.")
        if self.windows:
            lines.append(f"{len(self.windows)} visible window(s):")
            lines.extend(
                f"  - {w.title!r} {w.width}x{w.height}+{w.x}+{w.y}"
                for w in self.windows[:10]
            )
        if self.cursor:
            lines.append(f"Cursor at {self.cursor[0]},{self.cursor[1]}.")
        lines.extend(self.notes)
        return "\n".join(lines)


def _describe_params(kind: ActionKind, params: dict[str, Any]) -> str:
    """Readable parameter summary. Never includes clipboard or file *content*
    — the description goes into logs and the confirmation dialog, and content
    is exactly what should not be duplicated there."""
    if kind in {ActionKind.CLICK, ActionKind.DOUBLE_CLICK, ActionKind.RIGHT_CLICK,
                ActionKind.MOVE_MOUSE}:
        return f"at ({params.get('x')}, {params.get('y')})"
    if kind is ActionKind.DRAG:
        return (f"from ({params.get('from_x')}, {params.get('from_y')}) "
                f"to ({params.get('to_x')}, {params.get('to_y')})")
    if kind is ActionKind.SCROLL:
        return f"{params.get('direction', 'down')} x{params.get('amount', 3)}"
    if kind is ActionKind.TYPE_TEXT:
        text = str(params.get("text", ""))
        # Length, not content: typed text can be a password.
        return f"{len(text)} character(s)"
    if kind in {ActionKind.PRESS_KEY, ActionKind.HOTKEY}:
        return str(params.get("keys") or params.get("key") or "")
    if kind in {ActionKind.OPEN_APPLICATION, ActionKind.CLOSE_APPLICATION}:
        return str(params.get("application", ""))
    if kind is ActionKind.EXECUTE_COMMAND:
        return str(params.get("command", ""))
    if kind in {ActionKind.READ_FILE, ActionKind.WRITE_FILE, ActionKind.DELETE_PATH,
                ActionKind.LIST_DIRECTORY, ActionKind.CREATE_DIRECTORY}:
        return str(params.get("path", ""))
    if kind is ActionKind.MOVE_PATH:
        return f"{params.get('source', '')} -> {params.get('destination', '')}"
    if kind is ActionKind.FOCUS_WINDOW:
        return str(params.get("window_id", ""))
    if kind is ActionKind.WAIT:
        return f"{params.get('seconds', 1)}s"
    return ""
