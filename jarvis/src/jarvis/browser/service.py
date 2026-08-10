"""The browser JARVIS owns (Phase 4, §2 and §3).

One Playwright instance, one browser process, one isolated context, and a
bounded set of pages inside it. Nothing here navigates, clicks, reads a page or
consults a permission — those arrive in later steps. This is the runtime the
rest of Phase 4 is built on, and the properties it has to establish are
lifecycle properties.

## Why the browser is JARVIS's own

The service launches Chromium. It never connects to a browser that already
exists — no ``connect_over_cdp``, no attaching to a debugging port, no reuse of
the user's profile directory. Those calls are not merely unused; the class does
not import them, and a test asserts the module does not mention them.

The reason is that attaching hands over everything at once. The user's browser
holds their logged-in sessions for email, their bank, their employer, and
attaching to it grants JARVIS all of them invisibly — there is no moment where
the user permits a specific site, because the permission arrived with the
process. A browser JARVIS launched can only reach what it was told to visit.

## Why the context is isolated, and what "isolated" costs

Every launch creates a fresh :class:`BrowserContext`: its own cookie jar, its
own storage, its own cache. By default nothing in it is written to disk and
everything is discarded on shutdown.

The cleanliness is the lesser reason. The real one is that an isolated context
is what makes any later statement about credentials *checkable*. "JARVIS is not
logged into anything" is a claim you can only make if there is a boundary to
make it about.

``BrowserSettings.storage_dir`` opts out, and the opt-out is deliberately
awkward to arrive at by accident: it defaults to ``None``, it is named for what
it does, and :meth:`launch` records in the log and in the capability notes that
browsing state will survive the session.

## Lazy, because a browser is not free

Nothing launches at import, at construction, or during
:meth:`JarvisCore.startup`. A Chromium process is hundreds of megabytes and a
handful of subprocesses; a user who never asks JARVIS to browse should never
pay for one. :meth:`launch` is called on first use and is idempotent.

## Platform

Everything here is Playwright's async API and :mod:`asyncio`. There is no
``os.killpg``, no signal handling, no process group, and no X11 — Playwright
owns the browser process and its teardown on every platform it supports. The
module imports nothing from :mod:`jarvis.computer`.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from jarvis.browser.capabilities import (
    BrowserAvailability,
    BrowserCapabilityReport,
    BrowserError,
    BrowserUnavailable,
    detect,
)
from jarvis.browser.settings import BrowserSettings
from jarvis.db.base import new_id, utcnow
from jarvis.logging import get_logger

log = get_logger(__name__)


@dataclass(slots=True)
class PageHandle:
    """One open page, and the identity later steps will refer to it by.

    The id is minted here rather than derived from the URL: a page's URL
    changes under it on every navigation, so anything keyed on the URL would
    silently start meaning a different page. Element references (a later step)
    will be scoped to this id for the same reason.
    """

    page_id: str
    page: Any  # playwright.async_api.Page — typed loosely so this module
    # imports cleanly where Playwright is absent.
    created_at: datetime = field(default_factory=utcnow)

    @property
    def closed(self) -> bool:
        try:
            return bool(self.page.is_closed())
        except Exception:  # pragma: no cover - a dead page is a closed page
            return True

    def describe(self) -> dict[str, Any]:
        """Cheap, synchronous state. ``page.url`` does not do I/O."""
        try:
            url = self.page.url
        except Exception:  # pragma: no cover
            url = None
        return {
            "page_id": self.page_id,
            "url": url,
            "closed": self.closed,
            "created_at": self.created_at.isoformat(),
        }


@dataclass(slots=True)
class LaunchOutcome:
    """The result of trying to start a browser.

    A dataclass rather than an exception because launch failure is an ordinary,
    expected outcome — Chromium is not installed, the machine is locked down,
    the operator switched it off. JARVIS must keep running and be able to say
    why browsing is not available, which is exactly the shape the computer
    subsystem uses for a missing display.
    """

    ok: bool
    report: BrowserCapabilityReport
    #: Present when ``ok`` is False. The same text as ``report.reason`` for an
    #: availability failure, and the launch error for anything else.
    reason: str | None = None

    def require(self) -> None:
        if not self.ok:
            raise BrowserUnavailable(self.reason or "The browser is unavailable.")


class BrowserService:
    """Owns the Playwright runtime. One per JARVIS process."""

    def __init__(
        self,
        settings: BrowserSettings | None = None,
        *,
        activity_bus: Any = None,
    ) -> None:
        self.settings = settings or BrowserSettings()
        self.activity_bus = activity_bus

        self.capabilities = BrowserCapabilityReport()
        if not self.settings.enabled:
            # Knowable without probing, and worth knowing before anyone asks:
            # the status endpoint should say "switched off" immediately rather
            # than "not probed yet".
            self.capabilities.state = BrowserAvailability.DISABLED
            self.capabilities.reason = (
                "Browser control is switched off (JARVIS_BROWSER_ENABLED=false)."
            )

        self._playwright: Any = None
        self._browser: Any = None
        self._context: Any = None
        self._pages: dict[str, PageHandle] = {}
        #: Set when the browser process dies underneath us, so ``running``
        #: stops claiming otherwise before anyone tries to use it.
        self._disconnected_reason: str | None = None
        #: Serialises launch and shutdown. Two concurrent first-uses would
        #: otherwise race to create two browsers and leak one.
        self._lock = asyncio.Lock()

    # ── state ────────────────────────────────────────────────────────────────

    @property
    def started(self) -> bool:
        """True once a browser process exists, whether or not it is healthy."""
        return self._browser is not None

    @property
    def running(self) -> bool:
        """True when there is a browser and it is still connected.

        Distinct from :attr:`started` on purpose. A crashed browser has still
        been started, and the difference between the two is exactly the
        question "may I use it?" — answering that from ``started`` alone is how
        a service ends up reporting a browser it no longer has.
        """
        if self._browser is None:
            return False
        try:
            return bool(self._browser.is_connected())
        except Exception:  # pragma: no cover - an unaskable browser is gone
            return False

    @property
    def page_count(self) -> int:
        return len(self._pages)

    def describe(self) -> dict[str, Any]:
        """A synchronous snapshot. Never launches anything."""
        return {
            "enabled": self.settings.enabled,
            "started": self.started,
            "running": self.running,
            "headless": self.settings.headless,
            "isolated_context": self._context is not None,
            "persists_storage": self.settings.persists_storage,
            "pages": [handle.describe() for handle in self._pages.values()],
            "page_count": self.page_count,
            "max_pages": self.settings.max_pages,
            "disconnected_reason": self._disconnected_reason,
            "capabilities": self.capabilities.to_dict(),
        }

    # ── capability ───────────────────────────────────────────────────────────

    async def detect(self, *, refresh: bool = False) -> BrowserCapabilityReport:
        """Probe once and remember the answer.

        Cached because the probe starts a driver process, and re-answering
        "is Chromium installed?" on every status call would make the cheap
        endpoint the expensive one. ``refresh`` is for an operator who has just
        installed a browser and does not want to restart JARVIS.
        """
        if refresh or self.capabilities.state is BrowserAvailability.UNPROBED:
            self.capabilities = await detect(self.settings)
            log.info(
                "browser_capabilities_detected",
                state=self.capabilities.state.value,
                executable_source=self.capabilities.executable_source,
                playwright=self.capabilities.playwright_version,
            )
        return self.capabilities

    # ── lifecycle ────────────────────────────────────────────────────────────

    async def launch(self) -> LaunchOutcome:
        """Start the browser if it is not already running.

        Idempotent and safe to call concurrently. Returns a structured outcome
        rather than raising: a machine without Chromium is a machine JARVIS
        still has to work on.
        """
        async with self._lock:
            if self.running:
                return LaunchOutcome(ok=True, report=self.capabilities)

            if self.started and not self.running:
                # Crashed since last time. Tear the corpse down before
                # replacing it, or the old Playwright object leaks.
                log.warning(
                    "browser_relaunch_after_disconnect",
                    reason=self._disconnected_reason,
                )
                await self._teardown()

            report = await self.detect()
            if not report.available:
                return LaunchOutcome(ok=False, report=report, reason=report.reason)

            try:
                await self._start_locked(report)
            except Exception as exc:
                # Partial state is the dangerous outcome — a Playwright driver
                # with no browser, or a browser with no context. Unwind whatever
                # did come up so the next attempt starts from nothing.
                await self._teardown()
                reason = f"The browser failed to start: {exc}"
                self.capabilities.verified = False
                log.warning("browser_launch_failed", error=str(exc))
                return LaunchOutcome(ok=False, report=self.capabilities, reason=reason)

            self.capabilities.verified = True
            log.info(
                "browser_launched",
                headless=self.settings.headless,
                persists_storage=self.settings.persists_storage,
                executable_source=report.executable_source,
            )
            return LaunchOutcome(ok=True, report=self.capabilities)

    async def _start_locked(self, report: BrowserCapabilityReport) -> None:
        """The launch itself. Called with the lock held."""
        from playwright.async_api import async_playwright

        # ``.start()`` rather than ``async with``: the runtime outlives this
        # call and is stopped by ``shutdown``.
        self._playwright = await async_playwright().start()

        launch_kwargs: dict[str, Any] = {
            "headless": self.settings.headless,
            "timeout": self.settings.launch_timeout_seconds * 1000,
        }
        if self.settings.launch_args:
            launch_kwargs["args"] = list(self.settings.launch_args)
        if report.executable_source == "configured" and report.executable_path:
            launch_kwargs["executable_path"] = report.executable_path

        # launch(), never connect_over_cdp() or launch_persistent_context()
        # against a user profile. See the module docstring.
        self._browser = await self._playwright.chromium.launch(**launch_kwargs)
        self._disconnected_reason = None
        self._browser.on("disconnected", self._on_disconnected)

        context_kwargs: dict[str, Any] = {}
        if self.settings.storage_dir is not None:
            # The explicit opt-out from ephemerality. Said out loud in the log
            # and in the report, because "JARVIS stays logged in" should never
            # be something an operator discovers later.
            state_file = self.settings.storage_dir / "storage_state.json"
            self.settings.storage_dir.mkdir(parents=True, exist_ok=True)
            if state_file.is_file():
                context_kwargs["storage_state"] = str(state_file)
            note = (
                "Browsing state persists between sessions "
                f"({self.settings.storage_dir}); cookies and logins set in one "
                "session are present in the next."
            )
            if note not in self.capabilities.notes:
                self.capabilities.notes.append(note)
            log.info("browser_storage_persistent", directory=str(self.settings.storage_dir))

        self._context = await self._browser.new_context(**context_kwargs)
        self._context.set_default_navigation_timeout(
            self.settings.navigation_timeout_seconds * 1000
        )

    def _on_disconnected(self, _browser: Any) -> None:
        """Playwright's callback when the browser process goes away.

        Synchronous by Playwright's contract, so it records the fact and
        nothing more. Cleanup happens on the next :meth:`launch` or
        :meth:`shutdown`, where there is a lock and an event loop to do it in.
        """
        self._disconnected_reason = "The browser process disconnected."
        log.warning("browser_disconnected")

    # ── pages ────────────────────────────────────────────────────────────────

    async def new_page(self) -> PageHandle:
        """Open a page in the isolated context, launching the browser if needed.

        Raises rather than returning an outcome, because unlike launch there is
        no useful degraded behaviour: a caller asking for a page has already
        decided it needs one.
        """
        outcome = await self.launch()
        outcome.require()

        self._reap_closed_pages()
        if self.page_count >= self.settings.max_pages:
            raise BrowserError(
                f"The browser already has {self.page_count} pages open and the "
                f"limit is {self.settings.max_pages}. Close a page first.",
            )

        page = await self._context.new_page()
        handle = PageHandle(page_id=new_id("pg"), page=page)
        self._pages[handle.page_id] = handle
        log.info("browser_page_opened", page_id=handle.page_id,
                 pages=self.page_count)
        return handle

    def page(self, page_id: str) -> PageHandle:
        """Look up an open page. Raises if it is unknown or already closed."""
        handle = self._pages.get(page_id)
        if handle is None:
            raise BrowserError(f"There is no open page {page_id!r}.")
        if handle.closed:
            self._pages.pop(page_id, None)
            raise BrowserError(f"Page {page_id!r} has been closed.")
        return handle

    def pages(self) -> list[PageHandle]:
        self._reap_closed_pages()
        return list(self._pages.values())

    async def close_page(self, page_id: str) -> bool:
        """Close one page. Returns False if it was not open."""
        handle = self._pages.pop(page_id, None)
        if handle is None:
            return False
        try:
            await handle.page.close()
        except Exception as exc:  # already gone, or the browser died
            log.debug("browser_page_close_failed", page_id=page_id, error=str(exc))
        log.info("browser_page_closed", page_id=page_id, pages=self.page_count)
        return True

    def _reap_closed_pages(self) -> None:
        """Forget pages the browser closed on its own.

        A page can close without JARVIS asking — ``window.close()``, a crash,
        a renderer kill. Without this the cap counts corpses and the service
        refuses to open a page while holding none.
        """
        for page_id in [pid for pid, h in self._pages.items() if h.closed]:
            self._pages.pop(page_id, None)
            log.debug("browser_page_reaped", page_id=page_id)

    # ── shutdown ─────────────────────────────────────────────────────────────

    async def shutdown(self) -> None:
        """Close everything, in order, and survive any of it having failed.

        Safe to call when nothing started, when only part of it started, and
        repeatedly. Called from :meth:`JarvisCore.shutdown`, which must not
        raise on the way out — a process that cannot exit cleanly leaves the
        browser it owns running, which is precisely the outcome to avoid.
        """
        async with self._lock:
            await self._teardown()

    async def _teardown(self) -> None:
        """The unwinding itself. Assumes the lock is held."""
        for page_id, handle in list(self._pages.items()):
            try:
                await handle.page.close()
            except Exception as exc:
                log.debug("browser_page_close_failed", page_id=page_id, error=str(exc))
        self._pages.clear()

        # Innermost first: a context outliving its browser is not a thing
        # Playwright allows, but a browser outliving the process is.
        for label, resource, closer in (
            ("context", self._context, "close"),
            ("browser", self._browser, "close"),
            ("playwright", self._playwright, "stop"),
        ):
            if resource is None:
                continue
            try:
                await getattr(resource, closer)()
            except Exception as exc:
                log.warning(f"browser_{label}_close_failed", error=str(exc))

        had_browser = self._browser is not None
        self._context = None
        self._browser = None
        self._playwright = None
        self._disconnected_reason = None
        self.capabilities.verified = False
        if had_browser:
            log.info("browser_shutdown")
