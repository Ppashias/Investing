"""Computer tools (§34).

Every computer capability the model can reach, registered through the Phase 1
tool architecture so it inherits JSON-Schema validation, the permission engine,
confirmation, timeouts and audit — and then passes through the *computer*
policy engine as well. Two gates, not one, because the Phase 1 engine knows
about capabilities and the Phase 3 engine knows about modes, scopes and risk.

Every tool here does the same thing: build a :class:`ComputerAction` and hand it
to :meth:`ComputerService.execute_action`. None of them touches a backend. That
is what makes §13 checkable — grep for ``backend.`` outside
``computer/executor.py`` and ``computer/backends/`` and there are no hits.

## Risk declared here versus risk computed there

The ``risk_level`` on each ``@tool`` is the Phase 1 registry's estimate, used
for the tool list and the initial capability check. It is *not* what gates
execution: :func:`jarvis.computer.risk.classify_risk` recomputes risk from the
action's actual content, so ``execute_command`` shows as HIGH in the tool list
while ``pwd`` is classified LOW when it actually runs. Declaring it here too
keeps the tool list honest about the worst case.
"""

from __future__ import annotations

from typing import Any

from jarvis.computer.types import ActionKind, ComputerAction
from jarvis.db.models import Capability, RiskLevel
from jarvis.errors import JarvisError
from jarvis.tools.base import ToolContext, ToolResult, tool


class ComputerUnavailable(JarvisError):
    code = "computer_unavailable"
    http_status = 501
    retryable = False


async def _run(
    ctx: ToolContext, kind: ActionKind, params: dict[str, Any], reason: str,
    *, expectation: str | None = None,
) -> ToolResult:
    """Build an action and send it through the one chokepoint."""
    service = ctx.extras.get("computer")
    if service is None:
        return ToolResult.error(
            "Computer control is not available in this build."
        )

    action = ComputerAction(
        kind=kind,
        params=params,
        reason=reason,
        expectation=expectation,
        # Inherited from the request. A turn that read a web page or a
        # document is tainted, and the computer policy escalates every
        # non-read action on a tainted request to a confirmation (§32).
        tainted=ctx.tainted,
    )

    result = await service.execute_action(
        ctx.session, ctx.user_id, action, request_id=ctx.request_id, actor="model"
    )

    if result.ok:
        payload = dict(result.data)
        # Verification is reported to the model, not just logged: "the screen
        # did not change" is the signal that stops it building on a click
        # that missed.
        if result.verification.value in {"CONTRADICTED", "INCONCLUSIVE"}:
            return ToolResult.ok(
                f"{result.detail}\n\nVerification: {result.verification.value} — "
                f"{result.verification_detail}",
                verification=result.verification.value,
                **payload,
            )
        return ToolResult.ok(result.detail, verification=result.verification.value,
                             **payload)

    return ToolResult.error(
        f"{result.outcome.value}: {result.detail}", outcome=result.outcome.value
    )


# ── observation ──────────────────────────────────────────────────────────────


@tool(
    name="observe_screen",
    description=(
        "Look at the screen. Returns the window list, the active window, its "
        "geometry and the cursor position, plus a screenshot unless the screen "
        "is unchanged. Use this before acting, and after anything that should "
        "have changed something. Coordinates for clicking must come from what "
        "you see here, never from memory."
    ),
    parameters={
        "type": "object",
        "properties": {
            "include_image": {
                "type": "boolean",
                "description": (
                    "Default true. Set false when you only need window "
                    "geometry — it is far faster and costs no tokens."
                ),
            },
            "window_id": {
                "type": "string",
                "description": "Crop to this window from observe_screen output.",
            },
        },
        "additionalProperties": False,
    },
    capability=Capability.READ,
    risk_level=RiskLevel.LOW,
    category="computer",
)
async def observe_screen(
    *, ctx: ToolContext, include_image: bool = True, window_id: str | None = None
) -> ToolResult:
    return await _run(
        ctx, ActionKind.OBSERVE_SCREEN,
        {"include_image": include_image, "window_id": window_id},
        "Look at the current screen",
    )


@tool(
    name="list_windows",
    description=(
        "List open windows with exact titles, ids and bounds. Cheaper and more "
        "reliable than reading geometry off a screenshot — prefer it when you "
        "need to know where a window is."
    ),
    parameters={"type": "object", "properties": {}, "additionalProperties": False},
    capability=Capability.READ,
    category="computer",
)
async def list_windows(*, ctx: ToolContext) -> ToolResult:
    return await _run(ctx, ActionKind.GET_WINDOWS, {}, "List open windows")


# ── mouse ────────────────────────────────────────────────────────────────────


@tool(
    name="click",
    description=(
        "Click at screen coordinates. Observe first — coordinates from an "
        "earlier screenshot may be stale. If the image you looked at was "
        "scaled, convert back to screen coordinates before calling this. "
        "Afterwards you are told whether the screen actually changed near the "
        "click; if it did not, do not simply click again."
    ),
    parameters={
        "type": "object",
        "properties": {
            "x": {"type": "integer", "minimum": 0},
            "y": {"type": "integer", "minimum": 0},
            "button": {"type": "string", "enum": ["left", "right", "double"]},
            "target": {
                "type": "string",
                "description": "What you are clicking, e.g. 'the Save button'.",
            },
        },
        "required": ["x", "y", "target"],
        "additionalProperties": False,
    },
    capability=Capability.EXECUTE,
    risk_level=RiskLevel.MEDIUM,
    category="computer",
    confirmation_template="Click the screen?\n\n{args}",
)
async def click(
    *, ctx: ToolContext, x: int, y: int, target: str, button: str = "left"
) -> ToolResult:
    kind = {
        "left": ActionKind.CLICK,
        "right": ActionKind.RIGHT_CLICK,
        "double": ActionKind.DOUBLE_CLICK,
    }[button]
    return await _run(
        ctx, kind, {"x": x, "y": y}, f"Click {target}",
        expectation=f"{target} responds",
    )


@tool(
    name="scroll",
    description="Scroll the window under the pointer.",
    parameters={
        "type": "object",
        "properties": {
            "direction": {"type": "string", "enum": ["up", "down", "left", "right"]},
            "amount": {"type": "integer", "minimum": 1, "maximum": 20},
            "x": {"type": "integer"},
            "y": {"type": "integer"},
        },
        "required": ["direction"],
        "additionalProperties": False,
    },
    capability=Capability.EXECUTE,
    risk_level=RiskLevel.LOW,
    category="computer",
)
async def scroll(
    *, ctx: ToolContext, direction: str, amount: int = 3,
    x: int | None = None, y: int | None = None,
) -> ToolResult:
    return await _run(
        ctx, ActionKind.SCROLL,
        {"direction": direction, "amount": amount, "x": x, "y": y},
        f"Scroll {direction}",
    )


# ── keyboard ─────────────────────────────────────────────────────────────────


@tool(
    name="type_text",
    description=(
        "Type text into whatever has keyboard focus. Click the field first. "
        "Never type passwords, keys or other credentials — the call is refused "
        "if the text looks like one."
    ),
    parameters={
        "type": "object",
        "properties": {
            "text": {"type": "string", "maxLength": 5000},
            "target": {
                "type": "string",
                "description": "Where this is going, e.g. 'the search box'.",
            },
        },
        "required": ["text", "target"],
        "additionalProperties": False,
    },
    capability=Capability.EXECUTE,
    risk_level=RiskLevel.MEDIUM,
    category="computer",
    confirmation_template="Type into the focused field?\n\n{args}",
)
async def type_text(*, ctx: ToolContext, text: str, target: str) -> ToolResult:
    return await _run(
        ctx, ActionKind.TYPE_TEXT, {"text": text}, f"Type into {target}",
        expectation=f"{target} contains the text",
    )


@tool(
    name="press_key",
    description=(
        "Press one key, or a combination. Names: enter, tab, escape, "
        "backspace, delete, up/down/left/right, home, end, f1-f12, ctrl, alt, "
        "shift, super."
    ),
    parameters={
        "type": "object",
        "properties": {
            "keys": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
                "maxItems": 4,
                "description": "One key, or modifiers plus a key: ['ctrl','s'].",
            },
            "reason": {"type": "string"},
        },
        "required": ["keys", "reason"],
        "additionalProperties": False,
    },
    capability=Capability.EXECUTE,
    risk_level=RiskLevel.MEDIUM,
    category="computer",
)
async def press_key(*, ctx: ToolContext, keys: list[str], reason: str) -> ToolResult:
    if len(keys) == 1:
        return await _run(ctx, ActionKind.PRESS_KEY, {"key": keys[0]}, reason)
    return await _run(ctx, ActionKind.HOTKEY, {"keys": keys}, reason)


# ── applications ─────────────────────────────────────────────────────────────


@tool(
    name="open_application",
    description=(
        "Launch an approved application by name. Only applications on the "
        "allow-list can be opened — check computer_status for the list. "
        "Paths are not accepted."
    ),
    parameters={
        "type": "object",
        "properties": {
            "application": {"type": "string"},
            "arguments": {
                "type": "array", "items": {"type": "string"}, "maxItems": 8,
                "description": "Optional arguments, e.g. a URL or file path.",
            },
            "reason": {"type": "string"},
        },
        "required": ["application", "reason"],
        "additionalProperties": False,
    },
    capability=Capability.EXECUTE,
    risk_level=RiskLevel.MEDIUM,
    category="computer",
    confirmation_template="Open an application?\n\n{args}",
)
async def open_application(
    *, ctx: ToolContext, application: str, reason: str,
    arguments: list[str] | None = None,
) -> ToolResult:
    return await _run(
        ctx, ActionKind.OPEN_APPLICATION,
        {"application": application, "arguments": arguments or []},
        reason, expectation=f"{application} opens a window",
    )


# ── filesystem ───────────────────────────────────────────────────────────────


@tool(
    name="read_file",
    description=(
        "Read a text file from an approved directory. Binary files and files "
        "outside the approved roots are refused."
    ),
    parameters={
        "type": "object",
        "properties": {"path": {"type": "string"}, "reason": {"type": "string"}},
        "required": ["path", "reason"],
        "additionalProperties": False,
    },
    capability=Capability.READ,
    risk_level=RiskLevel.LOW,
    category="computer",
)
async def read_file(*, ctx: ToolContext, path: str, reason: str) -> ToolResult:
    return await _run(ctx, ActionKind.READ_FILE, {"path": path}, reason)


@tool(
    name="write_file",
    description=(
        "Write a text file inside an approved directory. Overwriting needs "
        "overwrite=true and is treated as irreversible. Executable file types "
        "are refused."
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "content": {"type": "string", "maxLength": 200000},
            "overwrite": {"type": "boolean"},
            "reason": {"type": "string"},
        },
        "required": ["path", "content", "reason"],
        "additionalProperties": False,
    },
    capability=Capability.WRITE,
    risk_level=RiskLevel.MEDIUM,
    category="computer",
    reversible=False,
    confirmation_template="Write a file?\n\n{args}",
)
async def write_file(
    *, ctx: ToolContext, path: str, content: str, reason: str,
    overwrite: bool = False,
) -> ToolResult:
    return await _run(
        ctx, ActionKind.WRITE_FILE,
        {"path": path, "content": content, "overwrite": overwrite}, reason,
    )


@tool(
    name="list_directory",
    description="List a directory inside an approved root.",
    parameters={
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
        "additionalProperties": False,
    },
    capability=Capability.READ,
    category="computer",
)
async def list_directory(*, ctx: ToolContext, path: str) -> ToolResult:
    return await _run(ctx, ActionKind.LIST_DIRECTORY, {"path": path},
                      f"List {path}")


# ── terminal ─────────────────────────────────────────────────────────────────


@tool(
    name="run_command",
    description=(
        "Run one command. No shell: pipes, redirects, chaining and "
        "substitution are unavailable, and a command containing them is "
        "refused — run one command at a time. Read-only commands run freely; "
        "anything that changes state or is unrecognised needs confirmation. "
        "Some commands are refused outright."
    ),
    parameters={
        "type": "object",
        "properties": {
            "command": {"type": "string", "minLength": 1, "maxLength": 2000},
            "reason": {"type": "string"},
            "working_directory": {"type": "string"},
            "timeout": {"type": "number", "minimum": 1, "maximum": 300},
        },
        "required": ["command", "reason"],
        "additionalProperties": False,
    },
    capability=Capability.EXECUTE,
    risk_level=RiskLevel.HIGH,
    reversible=False,
    category="computer",
    confirmation_template="Run a command?\n\n{args}",
)
async def run_command(
    *, ctx: ToolContext, command: str, reason: str,
    working_directory: str | None = None, timeout: float | None = None,
) -> ToolResult:
    return await _run(
        ctx, ActionKind.EXECUTE_COMMAND,
        {"command": command, "working_directory": working_directory,
         "timeout": timeout},
        reason,
    )


# ── status ───────────────────────────────────────────────────────────────────


@tool(
    name="computer_status",
    description=(
        "What JARVIS can and cannot do on this machine right now: display, "
        "permission mode, enabled scopes, approved applications and "
        "directories, and whether the emergency stop is engaged. Check this "
        "before attempting computer work, especially if an action was refused."
    ),
    parameters={"type": "object", "properties": {}, "additionalProperties": False},
    capability=Capability.READ,
    category="computer",
)
async def computer_status(*, ctx: ToolContext) -> ToolResult:
    service = ctx.extras.get("computer")
    if service is None:
        return ToolResult.error("Computer control is not available in this build.")

    status = await service.status(ctx.session, ctx.user_id)
    capabilities = status["capabilities"]
    lines = [
        f"Display: {capabilities['display']['value'] or 'none'} "
        f"({capabilities['display']['kind']})",
        f"Backend: {status['backend']}, connected={status['connected']}",
        f"Mode: {status['policy']['mode']}",
        f"Enabled scopes: {', '.join(status['policy']['enabled_scopes']) or 'none'}",
        f"Automatic scopes: {', '.join(status['policy']['auto_scopes']) or 'none'}",
        f"Applications: {', '.join(capabilities['applications']) or 'none'}",
        f"File roots: {', '.join(status['filesystem']['allowed_paths']) or 'none'} "
        f"(write={status['filesystem']['write']}, "
        f"delete={status['filesystem']['delete']})",
    ]
    if status["emergency_stop"]["engaged"]:
        lines.append(
            f"EMERGENCY STOP ENGAGED: {status['emergency_stop']['reason']}. "
            "No computer actions will run."
        )
    if status["active_window"]:
        lines.append(f"Active window: {status['active_window']['title']!r}")
    lines.extend(capabilities["notes"])

    unavailable = [
        f"  {name}: {info['reason']}"
        for name, info in capabilities["actions"].items()
        if not info["available"]
    ]
    if unavailable:
        lines.append("Unavailable actions:")
        lines.extend(unavailable[:6])

    return ToolResult.ok("\n".join(lines), **status)


#: The action each tool performs, for deciding whether to offer it at all.
#:
#: A tool whose action this machine cannot perform is not advertised to the
#: model — the registry already applies that rule to disabled tools, on the
#: grounds that advertising something that will be refused wastes a turn.
#: Here it prevents something worse than a wasted turn. Several of these tools
#: declare ``requires_confirmation``, and the executor obtains that approval
#: *before* the handler runs, so a machine with no display would ask the user
#: to approve a click and only then tell them clicking is impossible. An
#: approval collected for an action that was never going to happen teaches the
#: user their approvals are ceremonial.
#:
#: This changes only what is offered. Nothing is weakened: the policy engine
#: still denies the action on capability grounds for every other caller, and
#: ``computer_status`` is deliberately absent from this map so the model can
#: always ask why.
TOOL_ACTIONS: dict[str, ActionKind] = {
    "observe_screen": ActionKind.OBSERVE_SCREEN,
    "list_windows": ActionKind.GET_WINDOWS,
    "click": ActionKind.CLICK,
    "scroll": ActionKind.SCROLL,
    "type_text": ActionKind.TYPE_TEXT,
    "press_key": ActionKind.PRESS_KEY,
    "open_application": ActionKind.OPEN_APPLICATION,
    "read_file": ActionKind.READ_FILE,
    "write_file": ActionKind.WRITE_FILE,
    "list_directory": ActionKind.LIST_DIRECTORY,
    "run_command": ActionKind.EXECUTE_COMMAND,
}


TOOLS = [
    observe_screen,
    list_windows,
    click,
    scroll,
    type_text,
    press_key,
    open_application,
    read_file,
    write_file,
    list_directory,
    run_command,
    computer_status,
]
