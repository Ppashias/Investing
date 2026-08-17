"""Sub-agents (Phase D, item 6).

One agent loop was the whole system until now. This package adds more, and the
part worth reading is :mod:`jarvis.agents.identity`: the authority model that
makes a second actor safe, rather than the machinery that runs it.
"""

from jarvis.agents.identity import (
    MAX_DEPTH,
    AgentDepthExceeded,
    AgentIdentity,
)

__all__ = ["AgentIdentity", "AgentDepthExceeded", "MAX_DEPTH"]
