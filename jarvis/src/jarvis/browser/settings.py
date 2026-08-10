"""Browser subsystem configuration (Phase 4, §1).

A leaf module: no Playwright import, no service import, no database. It exists
so the settings object can be constructed and inspected on a machine where
Playwright is not installed at all.

The defaults are the conservative end of every choice. Headless, ephemeral
storage, a small page cap, and short timeouts — a browser that JARVIS owns
should be boring, and an operator who wants otherwise says so explicitly.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class BrowserSettings:
    """What the browser subsystem is allowed to be.

    Mirrors :class:`~jarvis.computer.service.ComputerSettings`: a plain
    dataclass built from :class:`~jarvis.config.Settings` in
    :meth:`JarvisCore.build`, so the subsystem takes its configuration as an
    argument rather than reaching for a global.
    """

    #: Master switch. Off means the service still reports *why* nothing works,
    #: the same way the computer subsystem does — the capability is disabled,
    #: not missing, and those are different answers.
    enabled: bool = True

    #: Explicit Chromium binary. First in the resolution order, and the escape
    #: hatch for a machine where Playwright's own bundled build is absent or
    #: mismatched. Left unset, Playwright resolves its own — which is the
    #: normal case after ``playwright install chromium``.
    executable_path: Path | None = None

    #: Headless by default. A visible window is useful for watching JARVIS work
    #: and is the operator's choice, not a default.
    headless: bool = True

    #: How long the browser process gets to come up. Playwright's own
    #: ``timeout`` argument, so a hung launch fails as a Playwright error
    #: rather than a stuck coroutine.
    launch_timeout_seconds: float = 30.0

    #: Default per-navigation budget. Stored here so the value has one home;
    #: navigation itself arrives in a later step.
    navigation_timeout_seconds: float = 20.0

    #: How many pages may exist at once. A cap rather than a limit on what is
    #: reachable: an agent that has opened twenty tabs has lost track of what
    #: it is doing, and each page is a live renderer process.
    max_pages: int = 5

    #: Where the browser context persists cookies and storage. ``None`` — the
    #: default — means a fully ephemeral context that exists only in memory and
    #: is discarded on shutdown. Setting it is an explicit decision to let
    #: login state outlive a session; see :meth:`BrowserService.launch` for
    #: what that boundary means.
    storage_dir: Path | None = None

    #: Extra Chromium flags. Needed on machines where the sandbox cannot be
    #: used (unprivileged containers), and deliberately not defaulted to
    #: ``--no-sandbox`` — turning the browser's own sandbox off is not
    #: something JARVIS should do on the operator's behalf.
    launch_args: tuple[str, ...] = ()

    @property
    def persists_storage(self) -> bool:
        """True when browsing state survives shutdown.

        Named as a question rather than read as ``storage_dir is not None`` at
        each call site, because "does JARVIS remember it was logged in?" is a
        security question and should read like one.
        """
        return self.storage_dir is not None
