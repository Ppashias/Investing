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

## Ownership, and the single lock that follows from it

::

    BrowserService
      └── Playwright runtime      (started by launch, stopped by shutdown)
          └── Browser             (one process, JARVIS's own)
              └── BrowserContext  (one, isolated)
                  └── Pages       (up to ``max_pages``)

Every resource is created by the service and destroyed by it. Nothing is
adopted from outside, so there is no case where teardown has to decide whether
something is JARVIS's to close.

The hierarchy is why there is **one** lock rather than one per level. Every
state change reads a level and writes the one below it — launch reads "is there
a browser?" and writes a context; page creation reads the page count and writes
a page — and every one of those reads and writes is separated by an ``await``.
A lock per resource would leave the gaps between levels unguarded, which is
exactly where the interesting failures live: a page created into a context a
concurrent shutdown had already closed, or a cap checked before any concurrent
caller had registered anything. That last one was real, and measured — a limit
of two admitted five before the lock covered page creation.

## What death means

When the browser process ends, everything under it ends with it. The service
does not pretend otherwise and does not put it back: pages are dropped rather
than reopened, the context is discarded rather than reused, and a caller that
wants a page afterwards asks for one and gets a page in a new clean context. A
silent replacement would hand back something that looks like the old page and
is not — no history, no state, no cookies, and no indication anything changed.
The lost cookies are the point rather than a side effect: recovery must not
quietly restore an authentication the user never granted twice.

## Platform

Everything here is Playwright's async API and :mod:`asyncio`. There is no
``os.killpg``, no signal handling, no process group, and no X11 — Playwright
owns the browser process and its teardown on every platform it supports. The
module imports nothing from :mod:`jarvis.computer`.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from urllib.parse import urljoin

from jarvis.browser.capabilities import (
    BrowserAvailability,
    BrowserCapabilityReport,
    BrowserError,
    BrowserUnavailable,
    detect,
)
from jarvis.browser.elements import ElementRegistry
from jarvis.browser.settings import BrowserSettings
from jarvis.browser.urls import UrlPolicy
from jarvis.db.base import new_id, utcnow
from jarvis.logging import get_logger

log = get_logger(__name__)


@dataclass(slots=True)
class PageHandle:
    """One open page, and the identity later steps will refer to it by.

    The id is minted here rather than derived from the URL: a page's URL
    changes under it on every navigation, so anything keyed on the URL would
    silently start meaning a different page. Element references are scoped to
    this id for the same reason — see :mod:`jarvis.browser.elements`.
    """

    page_id: str
    page: Any  # playwright.async_api.Page — typed loosely so this module
    # imports cleanly where Playwright is absent.
    created_at: datetime = field(default_factory=utcnow)
    #: The last navigation the context guard refused for this page, or None.
    #:
    #: Set by :meth:`BrowserService._guard_navigation` at the moment a request
    #: is aborted, and read by callers that need to explain *why* a navigation
    #: failed — Chromium reports an aborted navigation as a generic transport
    #: error, which would otherwise turn a policy refusal into "could not
    #: load". Cleared by whoever is about to navigate.
    blocked: Any = None

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


@dataclass(slots=True)
class ShutdownReport:
    """What happened on the way out.

    Shutdown never raises — :meth:`JarvisCore.shutdown` must be able to finish
    — but "never raises" and "nothing went wrong" are different claims, and
    collapsing them is how a teardown failure becomes invisible. Each failed
    step is recorded here and logged; a caller that cares can look, and the
    one that cannot act on it is not forced to.
    """

    #: ``{"context": "...", "browser": "...", "playwright": "..."}`` for the
    #: steps that failed. Empty when everything closed cleanly.
    failures: dict[str, str] = field(default_factory=dict)
    #: Steps abandoned because they exceeded ``shutdown_timeout_seconds``.
    timed_out: list[str] = field(default_factory=list)
    pages_closed: int = 0

    @property
    def clean(self) -> bool:
        return not self.failures and not self.timed_out

    def describe(self) -> dict[str, Any]:
        return {
            "clean": self.clean,
            "pages_closed": self.pages_closed,
            "failures": dict(self.failures),
            "timed_out": list(self.timed_out),
        }


class BrowserService:
    """Owns the Playwright runtime. One per JARVIS process."""

    def __init__(
        self,
        settings: BrowserSettings | None = None,
        *,
        activity_bus: Any = None,
    ) -> None:
        self._settings = settings or BrowserSettings()
        self.activity_bus = activity_bus

        self.capabilities = BrowserCapabilityReport()
        if not self._settings.enabled:
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
        #: Element references, page-scoped and generation-stamped. Owned here
        #: because its lifetime is exactly the browser's: the events that
        #: invalidate references — page closed, page navigated, browser gone —
        #: are the events this class already handles.
        self.elements = ElementRegistry()
        #: Set when the browser process dies underneath us, so ``running``
        #: stops claiming otherwise before anyone tries to use it.
        self._disconnected_reason: str | None = None
        #: The one lock, held for every operation that reads-then-changes
        #: lifecycle state: launching, opening a page, closing a page, tearing
        #: down.
        #:
        #: One lock rather than one per resource, because the races worth
        #: preventing all cross resource boundaries. Page creation reads
        #: ``page_count``, awaits, and then writes it — with a separate page
        #: lock, a shutdown could still land in that await and hand the caller
        #: a page belonging to a context that no longer exists. Serialising a
        #: local browser's lifecycle costs nothing at five pages.
        #:
        #: Every method that takes it calls only ``_locked`` helpers, never
        #: another public method: :class:`asyncio.Lock` is not reentrant, so
        #: ``new_page`` calling ``launch`` would deadlock.
        self._lock = asyncio.Lock()
        self._last_shutdown: ShutdownReport | None = None

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
        """A synchronous snapshot. Never launches anything.

        Invalidates first, so a dead browser is not described as holding open
        pages. Safe to mutate state without the lock because nothing between
        here and the return awaits: on a single-threaded event loop this runs
        to completion before any locked coroutine can resume.
        """
        self._invalidate_if_browser_died()
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

    @property
    def settings(self) -> BrowserSettings:
        return self._settings

    @settings.setter
    def settings(self, value: BrowserSettings) -> None:
        """Replacing the settings discards the capability answer they produced.

        The report is a conclusion *about* a particular configuration — which
        executable, whether it is enabled at all — so keeping it across a
        settings change means answering a question nobody asked with an answer
        to a different one.

        Latent until the core began probing at startup, because until then the
        first probe happened lazily, after any reconfiguration. It surfaced as
        the whole browser suite failing with "the browser is not available":
        the probe had run against the default settings and cached a refusal
        that the tests' own configuration could no longer clear.
        """
        self._settings = value
        self.capabilities = BrowserCapabilityReport()
        if not value.enabled:
            self.capabilities.state = BrowserAvailability.DISABLED
            self.capabilities.reason = (
                "Browser control is switched off (JARVIS_BROWSER_ENABLED=false)."
            )

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
            return await self._launch_locked()

    async def _launch_locked(self) -> LaunchOutcome:
        """The launch decision. Assumes the lock is held.

        Separate from :meth:`launch` so :meth:`new_page` can launch and open a
        page as one atomic operation. Without that, the page cap was a
        check-then-act across an ``await`` and concurrent callers sailed past
        it — five simultaneous requests against a limit of two produced five
        real pages.
        """
        if self.running:
            return LaunchOutcome(ok=True, report=self.capabilities)

        if self.started and not self.running:
            # Crashed since last time. Tear the corpse down before replacing
            # it, or the old Playwright object leaks.
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
            # with no browser, or a browser with no context. A context that
            # fails to create is the likeliest case and the one that would
            # otherwise strand a live Chromium with nothing referencing it.
            # Unwind whatever did come up so the next attempt starts clean.
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

        # Bounded, because ``new_context`` takes no timeout of its own and a
        # hang here would leave a live browser behind an await that never
        # returns. Part of launching, so it shares the launch budget.
        self._context = await asyncio.wait_for(
            self._browser.new_context(**context_kwargs),
            timeout=self.settings.launch_timeout_seconds,
        )
        self._context.set_default_navigation_timeout(
            self.settings.navigation_timeout_seconds * 1000
        )

        # The URL policy, enforced where every navigation must pass — not only
        # where JARVIS asks for one. ``browser_navigate`` checks before calling
        # goto, but a click on a link, a script, a meta refresh and a redirect
        # all navigate without asking anyone. Step 11 proved that: a click
        # reached a private address that explicit navigation had just refused,
        # and the victim server logged the request.
        #
        # Routing at the context is the only layer that sees all of them, and
        # it aborts before the request is dispatched, so the destination is
        # never contacted rather than merely never read.
        await self._context.route("**/*", self._guard_navigation)

    async def _guard_navigation(self, route: Any, request: Any) -> None:
        """Abort any *document* navigation the URL policy would refuse.

        Scoped to documents on purpose, and the limit is worth stating: a page
        JARVIS is allowed to read may still fetch its own images and scripts
        from wherever it likes, exactly as it would in the user's own browser.
        What this stops is the browser being *steered* to a destination the
        policy refuses — which is the boundary Step 11 found broken.

        The policy is rebuilt per request from the live settings rather than
        captured at launch, so it always agrees with the copy the tools build
        from the same source. There is deliberately no second policy here.

        Fails closed. An error deciding whether a destination is permitted
        aborts the navigation, because the alternative is dispatching a request
        nobody vouched for.
        """
        try:
            if getattr(request, "resource_type", None) != "document":
                await route.continue_()
                return

            policy = UrlPolicy(
                allow_localhost=self.settings.allow_localhost,
                allow_private_networks=self.settings.allow_private_networks,
            )
            decision = policy.check(request.url)

            if not decision.allowed:
                await self._block(route, request, decision)
                return

            await self._follow(route, request, policy)
        except Exception as exc:  # pragma: no cover - defensive, fails closed
            log.warning("browser_navigation_guard_failed", error=str(exc))
            with suppress(Exception):
                await route.abort("failed")

    async def _follow(self, route: Any, request: Any, policy: UrlPolicy) -> None:
        """Take a permitted document request, one redirect hop at a time.

        Chromium does not consult a route handler again for a hop the *server*
        chose: ``route.continue_()`` on a URL that answers 302 follows the
        Location internally, and the handler sees nothing. Measured, not
        assumed — a click on a permitted link whose server bounced it into a
        refused address reached that address with the handler never called for
        it.

        So the hop is taken here instead of by the network stack. The request
        is issued with redirects disabled; a 3xx is checked against the policy
        before anything else happens, and only then handed back to Chromium,
        which re-requests the new location and arrives at this handler again.
        Each hop is therefore checked *before* it is dispatched, which is the
        difference between "the response was not read" and "the request was
        never made" — and only the second is worth anything against a
        destination that acts on being contacted at all.

        The cost is that every document load goes through Playwright's own
        fetch rather than Chromium's, so this deliberately does not apply to
        sub-resources: a permitted page's own images and scripts are its
        business, and routing them through here would buy nothing and change
        much.
        """
        response = await route.fetch(max_redirects=0)
        location = (response.headers or {}).get("location", "")

        if 300 <= response.status < 400 and location:
            landed = policy.check_redirect(
                urljoin(request.url, location), from_url=request.url
            )
            if not landed.allowed:
                await self._block(route, request, landed)
                return

        await route.fulfill(response=response)

    async def _block(self, route: Any, request: Any, decision: Any) -> None:
        """Refuse the navigation, and leave enough behind to explain it.

        The refusal is recorded on the page *before* the abort: Chromium turns
        an aborted navigation into a generic transport error, so without this
        the tool layer could only report "could not load" — indistinguishable
        from the site being down, for a page that was deliberately refused.
        """
        self._record_block(request, decision)
        log.warning(
            "browser_navigation_blocked",
            url=decision.url,
            verdict=decision.verdict.value,
            reason=decision.reason,
        )
        await route.abort("blockedbyclient")

    def _record_block(self, request: Any, decision: Any) -> None:
        """Attach the refusal to the page it was aimed at, if we can find it.

        Best effort by design: the decision is already logged, and losing the
        attribution costs an explanation rather than the protection.
        """
        try:
            page = request.frame.page
        except Exception:  # pragma: no cover - a frame without a page
            return
        for handle in self._pages.values():
            if handle.page is page:
                handle.blocked = decision
                return

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

        The whole operation — launch if necessary, check the cap, create, and
        register — happens under one lock. That is not tidiness: the cap is a
        check-then-act across an ``await``, and without the lock concurrent
        callers each saw a count taken before any of them had registered
        anything. A limit of two admitted five.

        Raises rather than returning an outcome, because unlike launch there is
        no useful degraded behaviour: a caller asking for a page has already
        decided it needs one.
        """
        async with self._lock:
            outcome = await self._launch_locked()
            outcome.require()

            self._reap_closed_pages()
            if self.page_count >= self.settings.max_pages:
                raise BrowserError(
                    f"The browser already has {self.page_count} pages open and "
                    f"the limit is {self.settings.max_pages}. Close a page "
                    "first.",
                )

            try:
                page = await asyncio.wait_for(
                    self._context.new_page(),
                    timeout=self.settings.launch_timeout_seconds,
                )
            except Exception as exc:
                # Nothing is registered on failure, so the cap is not consumed
                # by a page that does not exist. A page failing to open is not
                # evidence the browser is dead, so the browser is left alone —
                # confusing the two would tear down a working browser because
                # one tab misbehaved.
                if isinstance(exc, asyncio.TimeoutError):
                    raise BrowserError(
                        "The browser did not open a new page within "
                        f"{self.settings.launch_timeout_seconds:g}s."
                    ) from exc
                raise BrowserError(f"The browser could not open a page: {exc}") from exc

            handle = PageHandle(page_id=new_id("pg"), page=page)
            self._pages[handle.page_id] = handle

            # Every main-frame navigation invalidates the references issued
            # against the page it replaced. Subscribed here rather than in a
            # navigation tool: the page can navigate without JARVIS asking —
            # a redirect, a meta refresh, a script — and references must go
            # stale for those too, or the ones that matter most survive.
            page.on(
                "framenavigated",
                lambda frame, _page_id=handle.page_id: self._frame_navigated(
                    frame, _page_id
                ),
            )

            log.info("browser_page_opened", page_id=handle.page_id,
                     pages=self.page_count)
            return handle

    def _frame_navigated(self, frame: Any, page_id: str) -> None:
        """A frame moved. Decide whether the page's references survive it.

        The only reason to keep them is positive knowledge that this was a
        sub-frame. Anything else — an attribute that raises, a frame detached
        between the event and this call — means the page may have been replaced
        and we cannot tell, so the references go.

        That default is not caution for its own sake. A locator is a lazily
        resolved selector, so a reference that survives a navigation resolves
        against the *new* DOM and matches whatever now sits where the old
        element was. Acting on it is the "clicks whatever happens to be there"
        failure this registry exists to prevent, and it would be reported as a
        success. Losing references costs an extra inspection; keeping the wrong
        ones costs the guarantee.

        A named method rather than the closure it replaced, so the unreadable
        frame case can be exercised by a test instead of only reasoned about.
        """
        try:
            is_subframe = frame.parent_frame is not None
        except Exception as exc:
            log.warning(
                "browser_frame_state_unknown", page_id=page_id, error=str(exc)
            )
            is_subframe = False
        if is_subframe:
            return  # a sub-frame; the page itself is unchanged
        self.elements.page_navigated(page_id)

    def page(self, page_id: str) -> PageHandle:
        """Look up an open page. Raises if it is unknown, closed, or orphaned.

        The three refusals say different things on purpose. "There is no such
        page" is a caller error; "you closed it" is a caller fact; "the browser
        died" is neither, and reporting it as a closed page would send someone
        looking for a bug in their own bookkeeping.
        """
        self._invalidate_if_browser_died()
        handle = self._pages.get(page_id)
        if handle is None:
            if self.started and not self.running:
                raise BrowserError(
                    f"Page {page_id!r} is gone: the browser process ended. "
                    "Launch a new browser and open a new page — the old one "
                    "cannot be restored."
                )
            raise BrowserError(f"There is no open page {page_id!r}.")
        if handle.closed:
            self._pages.pop(page_id, None)
            raise BrowserError(f"Page {page_id!r} has been closed.")
        return handle

    def pages(self) -> list[PageHandle]:
        self._invalidate_if_browser_died()
        self._reap_closed_pages()
        return list(self._pages.values())

    async def close_page(self, page_id: str) -> bool:
        """Close one page. Returns False if it was not open.

        Under the lock, so a page cannot be closed halfway through a teardown
        that is already closing it.
        """
        async with self._lock:
            handle = self._pages.pop(page_id, None)
            if handle is None:
                return False
            try:
                await asyncio.wait_for(
                    handle.page.close(),
                    timeout=self.settings.shutdown_timeout_seconds,
                )
            except Exception as exc:  # already gone, or the browser died
                log.debug("browser_page_close_failed", page_id=page_id,
                          error=str(exc))
            self.elements.forget_page(page_id)
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
            self.elements.forget_page(page_id)
            log.debug("browser_page_reaped", page_id=page_id)

    def _invalidate_if_browser_died(self) -> None:
        """Drop every page handle once the browser is gone.

        When the browser process ends, its context and every page inside it go
        with it — there is nothing left to hold a handle to. Keeping the
        bookkeeping would let :meth:`describe` report open pages that do not
        exist, and would let the cap refuse a new page on a browser that has
        none.

        Deliberately not a resurrection: the pages are dropped, not reopened.
        A caller that wants a page after a crash asks for one, and gets a page
        in a new clean context rather than a silent replacement for the one it
        lost.
        """
        if self._browser is None or self.running:
            return
        if self._pages:
            log.warning(
                "browser_pages_invalidated",
                pages=len(self._pages),
                reason=self._disconnected_reason or "the browser process ended",
            )
            self._pages.clear()
        self.elements.clear()
        self._context = None

    # ── shutdown ─────────────────────────────────────────────────────────────

    async def shutdown(self) -> ShutdownReport:
        """Close everything, in order, and survive any of it having failed.

        Safe to call when nothing started, when only part of it started, and
        repeatedly. Called from :meth:`JarvisCore.shutdown`, which must not
        raise on the way out — a process that cannot exit cleanly leaves the
        browser it owns running, which is precisely the outcome to avoid.

        Returns rather than raises, and returns something rather than nothing:
        see :class:`ShutdownReport` for why "did not raise" is not the same
        claim as "closed cleanly".
        """
        async with self._lock:
            return await self._teardown()

    @property
    def last_shutdown(self) -> ShutdownReport | None:
        """The most recent teardown's outcome, for anyone who asks later."""
        return self._last_shutdown

    async def _teardown(self) -> ShutdownReport:
        """The unwinding itself. Assumes the lock is held.

        Every step is independent and bounded. Independent because a context
        whose ``close`` raises must not prevent the browser from closing —
        stopping early is how a failed teardown leaves Chromium running.
        Bounded because a step that *hangs* is worse than one that raises: it
        would hold JARVIS's exit open forever. Abandoning a hung step costs
        nothing, since stopping the Playwright driver terminates the browser
        regardless of whether ``browser.close()`` ever returned.
        """
        report = ShutdownReport()
        budget = self.settings.shutdown_timeout_seconds

        for page_id, handle in list(self._pages.items()):
            try:
                await asyncio.wait_for(handle.page.close(), timeout=budget)
                report.pages_closed += 1
            except Exception as exc:
                log.debug("browser_page_close_failed", page_id=page_id,
                          error=str(exc))
        self._pages.clear()
        # References are meaningless without the pages they point into, and
        # they hold locators that hold those pages. Cleared here so shutdown
        # releases them rather than leaving the registry as the one thing
        # keeping a dead browser's objects reachable.
        self.elements.clear()

        # Innermost first: a context outliving its browser is not a thing
        # Playwright allows, but a browser outliving the process is. The
        # driver goes last on purpose — it is the backstop that kills the
        # browser if the browser's own close did not.
        for label, resource, closer in (
            ("context", self._context, "close"),
            ("browser", self._browser, "close"),
            ("playwright", self._playwright, "stop"),
        ):
            if resource is None:
                continue
            try:
                await asyncio.wait_for(getattr(resource, closer)(), timeout=budget)
            except asyncio.TimeoutError:
                report.timed_out.append(label)
                log.warning(f"browser_{label}_close_timed_out", seconds=budget)
            except Exception as exc:
                report.failures[label] = str(exc)
                log.warning(f"browser_{label}_close_failed", error=str(exc))

        had_browser = self._browser is not None
        self._context = None
        self._browser = None
        self._playwright = None
        self._disconnected_reason = None
        self.capabilities.verified = False
        self._last_shutdown = report
        if had_browser:
            log.info("browser_shutdown", **report.describe())
        return report
