"""Tool registration and discovery.

Tools are declared in code — that is the source of truth for schema and
handler. The registry additionally mirrors each tool into ``tool_definitions``
so operator policy (disable a tool, force it to always ask) survives restarts
and has somewhere to live that the permission engine can read.

Mirroring is one-directional and careful: it writes schema/description on every
startup so the DB never drifts from the code, but it never overwrites the
operator-owned columns (``enabled``, ``mode_override``).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from jarvis.db.models import ToolDefinition
from jarvis.errors import ToolNotFoundError
from jarvis.logging import get_logger
from jarvis.providers.base import ToolSpec
from jarvis.tools.base import Tool

log = get_logger(__name__)


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    # ── registration ─────────────────────────────────────────────────────────

    def register(self, tool: Tool, *, replace: bool = False) -> Tool:
        if tool.name in self._tools and not replace:
            raise ValueError(f"Tool '{tool.name}' is already registered")
        tool.validate_handler()
        self._tools[tool.name] = tool
        log.debug(
            "tool_registered",
            tool=tool.name,
            capability=tool.capability.value,
            risk=tool.risk_level.value,
        )
        return tool

    def register_all(self, tools: Iterable[Tool], *, replace: bool = False) -> None:
        for t in tools:
            self.register(t, replace=replace)

    def unregister(self, name: str) -> None:
        self._tools.pop(name, None)

    # ── discovery ────────────────────────────────────────────────────────────

    def get(self, name: str) -> Tool:
        tool = self._tools.get(name)
        if tool is None:
            raise ToolNotFoundError(
                f"Tool '{name}' is not registered",
                details={"available": sorted(self._tools)},
            )
        return tool

    def try_get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def has(self, name: str) -> bool:
        return name in self._tools

    def all(self) -> list[Tool]:
        return sorted(self._tools.values(), key=lambda t: t.name)

    def enabled(self) -> list[Tool]:
        return [t for t in self.all() if t.enabled]

    def by_category(self, category: str) -> list[Tool]:
        return [t for t in self.all() if t.category == category]

    def categories(self) -> list[str]:
        return sorted({t.category for t in self._tools.values()})

    # ── provider view ────────────────────────────────────────────────────────

    def provider_specs(self, names: Sequence[str] | None = None) -> list[ToolSpec]:
        """Model-facing schemas.

        Disabled tools are excluded — a disabled tool must not be advertised,
        or the model wastes a turn calling something that will be refused.
        """
        pool = self.enabled() if names is None else [
            self.get(n) for n in names if self.has(n)
        ]
        return [t.to_provider_spec() for t in pool if t.enabled]

    def describe(self) -> list[dict[str, object]]:
        return [t.describe() for t in self.all()]

    # ── persistence mirror ───────────────────────────────────────────────────

    async def sync_to_db(self, session: AsyncSession) -> int:
        """Write code-declared schema into ``tool_definitions``.

        Operator-owned columns are preserved. Rows for tools that no longer
        exist in code are disabled rather than deleted, so their policy is not
        lost if the tool comes back.
        """
        existing = {
            row.name: row
            for row in (await session.execute(select(ToolDefinition))).scalars().all()
        }
        touched = 0

        for tool in self.all():
            row = existing.get(tool.name)
            if row is None:
                session.add(
                    ToolDefinition(
                        name=tool.name,
                        version=tool.version,
                        description=tool.description,
                        parameters_schema=tool.parameters,
                        capability=tool.capability,
                        risk_level=tool.risk_level,
                        requires_confirmation=tool.requires_confirmation,
                        reversible=tool.reversible,
                        enabled=True,
                    )
                )
            else:
                # Code owns these.
                row.version = tool.version
                row.description = tool.description
                row.parameters_schema = tool.parameters
                row.capability = tool.capability
                row.risk_level = tool.risk_level
                row.requires_confirmation = tool.requires_confirmation
                row.reversible = tool.reversible
                # `enabled` and `mode_override` are the operator's — untouched.
                tool.enabled = row.enabled
            touched += 1

        for name, row in existing.items():
            if not self.has(name) and row.enabled:
                row.enabled = False
                log.info("tool_definition_orphaned", tool=name)

        await session.flush()
        log.info("tools_synced", count=touched)
        return touched


def build_default_registry() -> ToolRegistry:
    """The Phase 1-3 tool set.

    Four groups with genuinely different reach: the Phase 1-2 tools cannot
    touch anything outside JARVIS's own store, the Obsidian tools write to the
    user's notes, the computer tools operate the machine, and the browser tools
    reach the internet. Each group routes through its own chokepoint — the tool
    executor for all of them, plus ``ObsidianService``, ``ComputerService`` and
    ``BrowserPolicy``/``UrlPolicy`` for the three that leave.
    """
    from jarvis.tools.builtin import (
        agent_tools,
        browser_tools,
        computer_tools,
        memory_tools,
        obsidian_tools,
        system_tools,
        task_tools,
    )

    registry = ToolRegistry()
    registry.register_all(system_tools.TOOLS)
    registry.register_all(agent_tools.TOOLS)
    registry.register_all(task_tools.TOOLS)
    registry.register_all(memory_tools.TOOLS)
    registry.register_all(obsidian_tools.OBSIDIAN_TOOLS)
    registry.register_all(computer_tools.TOOLS)
    registry.register_all(browser_tools.TOOLS)
    return registry
