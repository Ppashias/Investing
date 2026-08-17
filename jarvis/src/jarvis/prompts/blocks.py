"""System prompt composition.

The brief (§13) asks that JARVIS's personality not be scattered through
application files. So the prompt is *assembled* from ordered, individually
addressable blocks rather than concatenated from string literals at call sites.

Why it is worth the indirection:

* **Sections evolve independently.** Security rules change on a different
  schedule from personality; project context changes every request.
* **Ordering is stable.** Identity first, security late (recency helps),
  volatile context last — which is also the right shape for prompt caching:
  stable blocks form a cacheable prefix, and per-request blocks sit after it.
* **It is inspectable.** ``/system/prompt`` can show exactly what JARVIS was
  told, which matters when behaviour surprises you.

Block *text* lives in :mod:`jarvis.prompts.identity`. This module is only the
mechanism.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum


class BlockOrder(IntEnum):
    """Assembly order. Gaps left so blocks can be inserted without renumbering."""

    IDENTITY = 100
    BEHAVIOR = 200
    CAPABILITIES = 300
    TOOL_GUIDANCE = 400
    SECURITY = 500
    USER_CONTEXT = 600
    PROJECT_CONTEXT = 700
    TASK_CONTEXT = 800
    MEMORY = 850
    # Knowledge sits after memory and before runtime: it is the largest and
    # least trusted block, so it is also the first thing worth dropping when
    # the budget is tight.
    KNOWLEDGE = 870
    RUNTIME_CONTEXT = 900


@dataclass(slots=True)
class PromptBlock:
    id: str
    title: str
    content: str
    order: BlockOrder
    enabled: bool = True
    #: Blocks whose content is identical on every request. Marked so a future
    #: caching layer can compute the stable prefix without guessing.
    cacheable: bool = True

    def render(self) -> str:
        body = self.content.strip()
        if not body:
            return ""
        return f"# {self.title}\n\n{body}"


@dataclass(slots=True)
class SystemPrompt:
    blocks: list[PromptBlock] = field(default_factory=list)

    def add(self, block: PromptBlock) -> "SystemPrompt":
        if block.enabled and block.content.strip():
            self.blocks.append(block)
        return self

    def render(self) -> str:
        ordered = sorted(self.blocks, key=lambda b: (b.order, b.id))
        return "\n\n---\n\n".join(
            rendered for b in ordered if (rendered := b.render())
        )

    def describe(self) -> list[dict[str, object]]:
        return [
            {
                "id": b.id,
                "title": b.title,
                "order": int(b.order),
                "cacheable": b.cacheable,
                "chars": len(b.content),
            }
            for b in sorted(self.blocks, key=lambda b: (b.order, b.id))
        ]

    @property
    def approx_tokens(self) -> int:
        """Rough estimate for budgeting. ~4 chars/token is close enough to
        decide what to trim; exact counts come from the provider's own
        accounting after the call."""
        return len(self.render()) // 4
