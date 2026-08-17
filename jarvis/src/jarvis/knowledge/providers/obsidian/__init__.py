"""Obsidian integration (Phase 2.5).

Four modules, one responsibility each:

* :mod:`.vault` — the transport. The only code that knows a vault is files.
* :mod:`.provider` — the generic :class:`KnowledgeProvider` implementation.
* :mod:`.sync` — incremental indexing and conflict handling.
* :mod:`.service` — permissions, audit, and the connection record.
* :mod:`.discovery` — finding a vault on this machine, without guessing.

Nothing outside this package imports any of them except the API routes, the
tools, and the provider registration in ``memory_routes._knowledge`` — which is
the boundary §3 asks for: the rest of JARVIS sees a knowledge provider, not
Obsidian.
"""

from jarvis.knowledge.providers.obsidian.discovery import discover
from jarvis.knowledge.providers.obsidian.provider import ObsidianProvider
from jarvis.knowledge.providers.obsidian.service import ObsidianConfig, ObsidianService
from jarvis.knowledge.providers.obsidian.sync import ObsidianSync, Resolution
from jarvis.knowledge.providers.obsidian.vault import (
    ConflictError,
    Note,
    VaultError,
    VaultTransport,
)

__all__ = [
    "ConflictError",
    "Note",
    "ObsidianConfig",
    "ObsidianProvider",
    "ObsidianService",
    "ObsidianSync",
    "Resolution",
    "VaultError",
    "VaultTransport",
    "discover",
]
