"""Who is asking, and what they are allowed to ask for (Phase D, item 6).

Until now there was one actor: the user, acting through one agent loop. A
sub-agent breaks that assumption, and the question it forces is not "how do we
run two loops" — that is easy — but *"on whose authority does the second one
act?"*

The wrong answer, and the one both multi-agent references reach for, is that a
child inherits its parent's authority and is then trusted to behave. That makes
the model's judgement the boundary. A page that persuades a planner to spawn a
"file cleanup specialist" has, at that moment, obtained a second actor with the
planner's full reach and none of its context.

## The ceiling

An agent carries a :class:`AgentIdentity` whose ``capabilities`` and
``tools`` are a *ceiling*, not a grant. It can only ever subtract:

* The permission engine intersects it with the user's grants. Anything outside
  the ceiling is ``DENY``, before grants are consulted, and no grant can lift it.
* A child's ceiling is computed at spawn as ``parent ∩ requested``. There is no
  operation that widens one — :meth:`AgentIdentity.narrowed` is the only way to
  make another, and its name is the whole API.

So authority decreases monotonically down the tree, by construction rather than
by a check somebody has to remember. This is `vierisid/jarvis`'s
``Math.min(role.authority_level, parent - 1)`` idea, expressed as set
intersection instead of a scalar — because a scalar says browser access is a
superset of file access, which the Phase 0 audit argued is false.

## What a ceiling is not

It is not a permission. An empty user grant set plus a wide ceiling is still
no authority: the ceiling bounds what the *agent* may ask for, and the grants
decide what the *user* has permitted. Both must agree. That ordering matters —
a ceiling that could grant would be a second permission system, and the one
thing this codebase has consistently refused to build is a second anything.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

from jarvis.db.base import new_id
from jarvis.db.models import Capability

#: Depth 0 is the user's own agent loop. Two levels of delegation below that is
#: as deep as anything has needed to go, and an unbounded tree is a runaway
#: waiting for a recursive prompt to find it.
MAX_DEPTH = 2

#: Capabilities a spawned agent may never hold, whatever its parent has.
#:
#: SENSITIVE_ACTION is denied by default for everyone, and a sub-agent is the
#: worst possible actor to be the exception: it is the least supervised thing
#: in the system and the furthest from the user who would have to answer for it.
FORBIDDEN_TO_CHILDREN = frozenset({Capability.SENSITIVE_ACTION})


@dataclass(slots=True, frozen=True)
class AgentIdentity:
    """An actor, and the outer bound on what it may attempt.

    Frozen because a ceiling that can be mutated after the permission engine
    has been handed it is not a ceiling. Narrowing produces a new object.
    """

    agent_id: str
    role: str = "root"
    parent_id: str | None = None
    depth: int = 0
    #: The capabilities this agent may attempt at all. ``None`` means "no
    #: ceiling" and is reserved for the root agent, which acts directly as the
    #: user. A child never has ``None`` — :meth:`narrowed` resolves it.
    capabilities: frozenset[Capability] | None = None
    #: Tool names this agent may call. ``None`` means "any tool the
    #: capabilities allow"; a set restricts further.
    tools: frozenset[str] | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def root(cls, role: str = "root") -> "AgentIdentity":
        """The user's own loop. No ceiling — the grants are the whole story."""
        return cls(agent_id=new_id("ag"), role=role)

    @property
    def is_root(self) -> bool:
        return self.parent_id is None

    def permits_capability(self, capability: Capability) -> bool:
        return self.capabilities is None or capability in self.capabilities

    def permits_tool(self, name: str) -> bool:
        return self.tools is None or name in self.tools

    def narrowed(
        self,
        *,
        role: str,
        capabilities: frozenset[Capability] | set[Capability] | None = None,
        tools: frozenset[str] | set[str] | None = None,
    ) -> "AgentIdentity":
        """A child identity that cannot exceed this one.

        Intersection, never union. Asking for a capability the parent does not
        hold is not an error — it is simply not granted, because an error would
        make the *request* meaningful and the request comes from the model.
        Silently dropping it means a prompt-injected spawn asking for
        ``SENSITIVE_ACTION`` gets an agent that cannot use it, rather than a
        refusal it can iterate against to discover the boundary.
        """
        if self.depth + 1 > MAX_DEPTH:
            raise AgentDepthExceeded(
                f"Agents may not nest more than {MAX_DEPTH} deep "
                f"(this one is at depth {self.depth})."
            )

        # The parent's own ceiling, or everything the permission model knows
        # about when the parent is the root and has none.
        inherited = (
            self.capabilities
            if self.capabilities is not None
            else frozenset(Capability)
        )
        wanted = frozenset(capabilities) if capabilities is not None else inherited
        allowed = (inherited & wanted) - FORBIDDEN_TO_CHILDREN

        inherited_tools = self.tools
        if tools is None:
            child_tools = inherited_tools
        elif inherited_tools is None:
            child_tools = frozenset(tools)
        else:
            child_tools = frozenset(tools) & inherited_tools

        return AgentIdentity(
            agent_id=new_id("ag"),
            role=role,
            parent_id=self.agent_id,
            depth=self.depth + 1,
            capabilities=allowed,
            tools=child_tools,
        )

    def describe(self) -> dict[str, Any]:
        """For the audit row. The ceiling is the interesting part."""
        return {
            "agent_id": self.agent_id,
            "role": self.role,
            "parent_id": self.parent_id,
            "depth": self.depth,
            "capabilities": (
                None if self.capabilities is None
                else sorted(c.value for c in self.capabilities)
            ),
            "tools": None if self.tools is None else sorted(self.tools),
        }

    def with_meta(self, **items: Any) -> "AgentIdentity":
        return replace(self, meta={**self.meta, **items})


class AgentDepthExceeded(Exception):
    """A spawn that would nest deeper than :data:`MAX_DEPTH`."""
