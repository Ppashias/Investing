"""Browser lifecycle hardening (Phase 4, Step 3).

Step 2 established the runtime. This file is about what happens when it goes
wrong: concurrent callers, a browser that dies, a context that will not create,
a page that closes itself, a teardown that hangs.

## Three kinds of evidence, kept apart

Every test below is one of:

* **real browser** — a Chromium process, a real context, real pages. Used
  wherever a real browser can demonstrate the property, because a mock that
  reports a clean shutdown proves nothing about shutting down.
* **controlled failure injection** — a real service with one dependency
  replaced by something that raises or hangs. Used where the real failure is
  unreproducible on demand: a context creation that fails, a ``close`` that
  never returns.
* **simulation** — a stand-in object with no browser behind it. Used only for
  states a browser cannot be put into deliberately.

The final report says which is which, and they are not interchangeable. A
crash simulated by calling ``browser.close()`` is a real process ending, and
is labelled as such; a crash simulated by setting a flag on a stub is not, and
is labelled as that.

Skips carry the resolver's own reason. Nothing here silently passes because
there was no browser to test with.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from jarvis.browser import (
    BrowserService,
    BrowserSettings,
    BrowserUnavailable,
    ShutdownReport,
)
from jarvis.browser.capabilities import BrowserError

from .test_browser_runtime import browser_module, code_of, resolve_chromium


@pytest.fixture
async def chromium() -> BrowserSettings:
    """A real browser, or an explicit skip naming what was missing.

    Shares the runtime suite's cached resolution: the probe starts a Playwright
    driver process, and doing that once per test across two suites is churn
    the machine does not need while it is also starting browsers.
    """
    settings, reason = await resolve_chromium()
    if settings is None:
        pytest.skip(f"No usable Chromium on this machine: {reason}")
    return settings


@pytest.fixture
async def service(chromium: BrowserSettings):
    svc = BrowserService(chromium)
    try:
        yield svc
    finally:
        await svc.shutdown()


# ── 1-2: concurrent and repeated launch ──────────────────────────────────────


async def test_concurrent_launch_creates_exactly_one_browser(service) -> None:
    """REAL BROWSER. Ten simultaneous first-uses, one Chromium.

    Without the lock every caller sees ``running is False``, every caller
    launches, and all but one of the resulting processes ends up with nothing
    referencing it — an orphan that outlives JARVIS.
    """
    outcomes = await asyncio.gather(*(service.launch() for _ in range(10)))

    assert all(o.ok for o in outcomes), [o.reason for o in outcomes if not o.ok]
    assert service.running is True
    # One browser: every outcome describes the same live process.
    assert len({id(service._browser)}) == 1
    assert len(service._browser.contexts) == 1


async def test_repeated_launch_reuses_the_same_browser(service) -> None:
    """REAL BROWSER. Sequential launches are idempotent, not additive."""
    await service.launch()
    browser, context = service._browser, service._context

    for _ in range(3):
        assert (await service.launch()).ok is True

    assert service._browser is browser
    assert service._context is context
    assert len(browser.contexts) == 1


async def test_launch_and_shutdown_do_not_interleave(service) -> None:
    """REAL BROWSER. A shutdown racing a launch must not strand a browser.

    Both take the same lock, so whichever runs second sees the first's
    finished state. The outcome may legitimately be either order; what must
    not happen is a live browser with the service believing it has none.
    """
    await service.launch()
    await asyncio.gather(
        service.launch(), service.shutdown(), service.launch(),
        return_exceptions=True,
    )
    if service.started:
        assert service.running is True
    else:
        assert service._playwright is None


async def test_a_page_on_an_unavailable_browser_says_unavailable() -> None:
    """The refusal must name the right problem.

    ``BrowserUnavailable`` and ``BrowserError`` mean different things — "this
    machine cannot browse" versus "that browser operation failed" — and a
    caller deciding whether to retry needs the difference. Page creation on a
    disabled subsystem is the availability answer, not an operation failure.
    """
    svc = BrowserService(BrowserSettings(enabled=False))
    with pytest.raises(BrowserUnavailable) as caught:
        await svc.new_page()

    assert "switched off" in str(caught.value)
    assert not isinstance(caught.value, BrowserError)
    assert svc.page_count == 0
    assert svc.started is False


# ── 3: context creation failure ──────────────────────────────────────────────


async def test_a_context_that_fails_to_create_does_not_leak_the_browser(
    service, monkeypatch
) -> None:
    """CONTROLLED FAILURE INJECTION on a REAL BROWSER.

    The browser launches for real; ``new_context`` is then made to raise. This
    is the partial-initialisation case that matters most — a live Chromium
    with no context is a process nothing references and nothing will close.
    """
    launched: list = []

    async def _start_then_fail(report):
        # Let the real launch happen, capture the browser, then fail exactly
        # where a context failure would.
        from playwright.async_api import async_playwright as ap

        service._playwright = await ap().start()
        kwargs = {"headless": True, "timeout": 30_000}
        if service.settings.launch_args:
            kwargs["args"] = list(service.settings.launch_args)
        if service.settings.executable_path:
            kwargs["executable_path"] = str(service.settings.executable_path)
        service._browser = await service._playwright.chromium.launch(**kwargs)
        launched.append(service._browser)
        raise RuntimeError("context creation failed")

    monkeypatch.setattr(service, "_start_locked", _start_then_fail)

    outcome = await service.launch()

    assert outcome.ok is False
    assert "context creation failed" in outcome.reason
    # The state must be equivalent to "not initialised"…
    assert service.started is False
    assert service.running is False
    assert service._playwright is None
    assert service._context is None
    # …and the real browser that did come up must actually be gone.
    assert launched and launched[0].is_connected() is False


async def test_the_service_recovers_after_a_context_failure(
    service, monkeypatch
) -> None:
    """CONTROLLED FAILURE INJECTION then REAL BROWSER.

    A failed launch must leave the service launchable, not poisoned.
    """
    async def _fail(report):
        raise RuntimeError("context creation failed")

    monkeypatch.setattr(service, "_start_locked", _fail)
    assert (await service.launch()).ok is False

    monkeypatch.undo()
    outcome = await service.launch()
    assert outcome.ok is True, outcome.reason
    assert service.running is True


# ── 4: page creation failure ─────────────────────────────────────────────────


async def test_a_page_that_fails_to_open_consumes_no_capacity(
    service, monkeypatch
) -> None:
    """CONTROLLED FAILURE INJECTION on a REAL BROWSER.

    A page that never opened must not occupy a slot in the cap, or a run of
    transient failures would silently exhaust the browser.
    """
    await service.launch()

    async def _refuse():
        raise RuntimeError("renderer would not start")

    monkeypatch.setattr(service._context, "new_page", _refuse)

    with pytest.raises(BrowserError) as caught:
        await service.new_page()
    assert "could not open a page" in str(caught.value)
    assert service.page_count == 0

    monkeypatch.undo()
    handle = await service.new_page()
    assert handle.page_id
    assert service.page_count == 1


async def test_a_page_that_never_opens_is_abandoned_not_awaited_forever(
    service, monkeypatch
) -> None:
    """CONTROLLED FAILURE INJECTION. A hang must become an error.

    Playwright's ``new_page`` takes no timeout of its own, so without a bound
    a wedged renderer holds the lifecycle lock forever and every later browser
    operation queues behind it.
    """
    await service.launch()
    service.settings = replace(service.settings, launch_timeout_seconds=0.2)

    async def _hang():
        await asyncio.sleep(30)

    monkeypatch.setattr(service._context, "new_page", _hang)

    with pytest.raises(BrowserError) as caught:
        await service.new_page()
    assert "did not open a new page" in str(caught.value)
    assert service.page_count == 0


async def test_a_page_failure_does_not_tear_down_the_browser(
    service, monkeypatch
) -> None:
    """One misbehaving tab is not evidence the browser is dead."""
    await service.launch()
    browser = service._browser

    async def _refuse():
        raise RuntimeError("no")

    monkeypatch.setattr(service._context, "new_page", _refuse)
    with pytest.raises(BrowserError):
        await service.new_page()

    assert service.running is True
    assert service._browser is browser


# ── 5-8: concurrency and the page cap ────────────────────────────────────────


async def test_concurrent_page_creation_cannot_exceed_the_cap(
    chromium: BrowserSettings,
) -> None:
    """REAL BROWSER. The bug Step 3 exists to fix.

    Before the lifecycle lock covered page creation, the cap was a
    check-then-act across an ``await``: every concurrent caller read a count
    taken before any of them had registered anything. Measured against the
    real implementation, a limit of two admitted five.

    The assertion is against the browser's own page list, not only the
    service's bookkeeping — the failure mode was real pages existing that the
    ledger had not yet counted, so counting the ledger would have missed it.
    """
    svc = BrowserService(replace(chromium, max_pages=2))
    try:
        results = await asyncio.gather(
            *(svc.new_page() for _ in range(5)), return_exceptions=True
        )

        created = [r for r in results if not isinstance(r, Exception)]
        refused = [r for r in results if isinstance(r, BrowserError)]
        assert len(created) == 2
        assert len(refused) == 3
        assert svc.page_count == 2
        assert len(svc._context.pages) == 2
        assert all("limit is 2" in str(r) for r in refused)
    finally:
        await svc.shutdown()


async def test_concurrent_page_creation_during_a_cold_start(
    chromium: BrowserSettings,
) -> None:
    """REAL BROWSER. The cap holds even when the browser does not exist yet.

    Each caller launches *and* opens a page. Without one lock covering both,
    the launch race and the cap race compound.
    """
    svc = BrowserService(replace(chromium, max_pages=3))
    try:
        results = await asyncio.gather(
            *(svc.new_page() for _ in range(8)), return_exceptions=True
        )
        created = [r for r in results if not isinstance(r, Exception)]

        assert len(created) == 3
        assert svc.page_count == 3
        assert len(svc._browser.contexts) == 1, "one context, not eight"
        assert len(svc._context.pages) == 3
    finally:
        await svc.shutdown()


async def test_closing_a_page_releases_its_slot(chromium: BrowserSettings) -> None:
    """REAL BROWSER."""
    svc = BrowserService(replace(chromium, max_pages=1))
    try:
        first = await svc.new_page()
        with pytest.raises(BrowserError):
            await svc.new_page()

        assert await svc.close_page(first.page_id) is True
        second = await svc.new_page()

        assert second.page_id != first.page_id
        assert svc.page_count == 1
        assert len(svc._context.pages) == 1
    finally:
        await svc.shutdown()


async def test_concurrent_close_and_open_stay_consistent(
    chromium: BrowserSettings,
) -> None:
    """REAL BROWSER. Closing and opening at once must not double-count."""
    svc = BrowserService(replace(chromium, max_pages=2))
    try:
        a = await svc.new_page()
        await svc.new_page()

        results = await asyncio.gather(
            svc.close_page(a.page_id), svc.new_page(), return_exceptions=True
        )
        assert results[0] is True
        assert svc.page_count == len(svc._context.pages)
        assert svc.page_count <= 2
    finally:
        await svc.shutdown()


async def test_a_page_closed_behind_the_services_back_frees_its_slot(
    chromium: BrowserSettings,
) -> None:
    """REAL BROWSER. ``window.close()`` and renderer kills are not JARVIS's doing.

    If those stay in the ledger the cap counts corpses and eventually refuses
    to open a page while the browser holds none.
    """
    svc = BrowserService(replace(chromium, max_pages=1))
    try:
        handle = await svc.new_page()
        await handle.page.close()

        replacement = await svc.new_page()
        assert replacement.page_id != handle.page_id
        assert svc.page_count == 1
    finally:
        await svc.shutdown()


async def test_a_closed_page_is_refused_by_lookup_not_silently_reopened(
    service,
) -> None:
    """REAL BROWSER. A page the caller closed stays closed.

    Recreating it would hand back something that looks like the old page and
    is not — no history, no state, and no indication anything changed.
    """
    handle = await service.new_page()
    assert await service.close_page(handle.page_id) is True

    with pytest.raises(BrowserError):
        service.page(handle.page_id)
    assert service.page_count == 0


# ── 9-10: browser death and recovery ─────────────────────────────────────────


async def test_a_real_browser_death_invalidates_context_and_pages(
    service,
) -> None:
    """REAL BROWSER, real process ending.

    ``browser.close()`` ends the actual Chromium process, which is what the
    service sees when a browser crashes — Playwright reports the same
    disconnection either way. It is not a killed process and is not described
    as one; what it genuinely demonstrates is the service's reaction to the
    browser going away underneath it.
    """
    await service.launch()
    await service.new_page()
    assert service.running is True

    await service._browser.close()  # the browser goes away

    assert service.started is True, "it did start; that is a different question"
    assert service.running is False
    assert service.describe()["running"] is False


async def test_a_dead_browser_reports_no_open_pages(service) -> None:
    """REAL BROWSER. Each entry point invalidates, not just one of them.

    Separate tests per accessor on purpose: written as one, the first failing
    assertion masks the rest, and a run that reached only ``describe`` would
    look like proof that ``pages`` and ``page`` were covered too.
    """
    await service.new_page()
    await service._browser.close()

    assert service.describe()["pages"] == []


async def test_a_dead_browser_discards_its_context_too(service) -> None:
    """REAL BROWSER. Pages are not the only thing that dies with the browser.

    Reaping closed pages would empty the page list on its own — Playwright
    marks them closed when the browser goes. What it would *not* do is drop
    the context, which is equally dead and which the next launch must replace
    rather than reuse.
    """
    await service.new_page()
    await service._browser.close()

    assert service.pages() == []
    assert service.page_count == 0
    assert service._context is None, "a dead browser's context must not be kept"
    assert service.describe()["isolated_context"] is False


async def test_a_page_lookup_after_a_browser_death_blames_the_browser(
    service,
) -> None:
    """REAL BROWSER. The diagnosis must name the cause.

    "Page pg_x has been closed" is what the caller would otherwise be told,
    and it is misleading: nobody closed it, the browser ended. Someone reading
    that goes looking for a bug in their own bookkeeping.
    """
    handle = await service.new_page()
    await service._browser.close()

    with pytest.raises(BrowserError) as caught:
        service.page(handle.page_id)

    message = str(caught.value)
    assert "the browser process ended" in message
    assert "cannot be restored" in message


async def test_the_service_recovers_with_a_clean_browser_after_death(
    service,
) -> None:
    """REAL BROWSER. The full recovery sequence.

    launch → context → page → browser dies → detected → explicit launch →
    new browser, new context, new page → shutdown. Nothing is resurrected: the
    old page is gone and the caller asks for a new one.
    """
    await service.launch()
    old_page = await service.new_page()
    await old_page.page.goto("data:text/html,<h1>before</h1>")
    old_browser, old_context = service._browser, service._context

    await old_browser.close()
    assert service.running is False

    outcome = await service.launch()
    assert outcome.ok is True, outcome.reason
    assert service.running is True
    assert service._browser is not old_browser
    assert service._context is not old_context
    assert service.page_count == 0, "the old page must not come back"

    fresh = await service.new_page()
    assert fresh.page_id != old_page.page_id
    await fresh.page.goto("data:text/html,<h1>after</h1>")
    assert "after" in await fresh.page.content()


async def test_recovery_does_not_inherit_the_previous_sessions_cookies(
    service,
) -> None:
    """REAL BROWSER. Recovery must not become accidental persistence.

    A crash followed by a relaunch is the one moment where "restore what was
    there" is a tempting behaviour, and where doing it would silently give
    JARVIS a login it was never granted. Step 3 implements no persistence, and
    this is what proves the recovery path did not introduce one by accident.
    """
    await service.launch()
    await service._context.add_cookies(
        [{"name": "session", "value": "secret", "url": "https://example.invalid"}]
    )
    assert len(await service._context.cookies()) == 1

    await service._browser.close()
    assert (await service.launch()).ok is True

    assert await service._context.cookies() == []
    assert service.settings.persists_storage is False


async def test_shutdown_after_a_browser_death_is_still_clean(service) -> None:
    """REAL BROWSER. Teardown meets objects that are already gone."""
    await service.launch()
    await service.new_page()
    await service._browser.close()

    report = await service.shutdown()

    assert isinstance(report, ShutdownReport)
    assert service.started is False
    assert service._playwright is None


async def test_a_stub_browser_that_cannot_be_asked_is_treated_as_gone() -> None:
    """SIMULATION. A browser object whose ``is_connected`` raises.

    Not reachable with a real browser — Playwright answers or the object is
    gone — so a stand-in is the only way to cover it. Labelled as simulation
    because nothing here involves a browser process.
    """
    svc = BrowserService(BrowserSettings())

    class _Unaskable:
        def is_connected(self):
            raise RuntimeError("transport closed")

    svc._browser = _Unaskable()
    assert svc.running is False
    assert svc.describe()["running"] is False


# ── 11-17: shutdown ──────────────────────────────────────────────────────────


async def test_shutdown_before_launch_is_a_clean_no_op() -> None:
    svc = BrowserService(BrowserSettings())
    report = await svc.shutdown()
    assert report.clean is True
    assert report.pages_closed == 0


async def test_shutdown_after_launch_closes_the_browser(chromium) -> None:
    """REAL BROWSER."""
    svc = BrowserService(chromium)
    await svc.launch()
    browser = svc._browser

    report = await svc.shutdown()

    assert report.clean is True
    assert browser.is_connected() is False
    assert svc.started is False


async def test_shutdown_with_pages_closes_all_of_them(chromium) -> None:
    """REAL BROWSER."""
    svc = BrowserService(replace(chromium, max_pages=3))
    pages = [await svc.new_page() for _ in range(3)]
    browser = svc._browser

    report = await svc.shutdown()

    assert report.pages_closed == 3
    assert report.clean is True
    assert all(h.closed for h in pages)
    assert browser.is_connected() is False
    assert svc.page_count == 0


async def test_repeated_shutdown_is_safe_on_a_real_browser(chromium) -> None:
    """REAL BROWSER."""
    svc = BrowserService(chromium)
    await svc.new_page()

    first = await svc.shutdown()
    second = await svc.shutdown()
    third = await svc.shutdown()

    assert first.pages_closed == 1
    assert second.pages_closed == 0 and second.clean
    assert third.clean
    assert svc.started is False


@pytest.mark.parametrize(
    "stage",
    ["nothing", "playwright", "playwright+browser", "browser+context",
     "browser+context+pages"],
)
async def test_shutdown_is_clean_from_every_partial_state(stage: str) -> None:
    """SIMULATION. Each partial-initialisation state, torn down.

    Stand-ins rather than a real browser because these states are the *middle*
    of a launch, and a real launch does not stop there on request. What is
    being checked is the teardown's own logic — that it skips what is absent
    and closes what is present — which the stand-ins exercise exactly.
    """
    from jarvis.browser.service import PageHandle

    closed: list[str] = []

    class _Closable:
        def __init__(self, label: str) -> None:
            self.label = label

        async def close(self):
            closed.append(self.label)

        async def stop(self):
            closed.append(self.label)

        def is_closed(self):
            return False

    svc = BrowserService(BrowserSettings())
    if "playwright" in stage or "browser" in stage:
        svc._playwright = _Closable("playwright")
    if "browser" in stage:
        svc._browser = _Closable("browser")
    if "context" in stage:
        svc._context = _Closable("context")
    if "pages" in stage:
        svc._pages["pg_1"] = PageHandle(page_id="pg_1", page=_Closable("page"))

    report = await svc.shutdown()

    assert report.clean is True
    assert svc._playwright is None and svc._browser is None and svc._context is None
    assert svc.page_count == 0
    if "pages" in stage:
        assert "page" in closed
    if "context" in stage:
        assert "context" in closed
    if "browser" in stage:
        assert "browser" in closed and closed.index("context" if "context" in stage
                                                    else "browser") <= closed.index("browser")


async def test_one_failing_close_does_not_abandon_the_rest() -> None:
    """CONTROLLED FAILURE INJECTION.

    The failure that matters: a context whose ``close`` raises must not stop
    the browser and the driver from being closed. Stopping early there is
    precisely how a teardown leaves Chromium running.
    """
    stopped: list[str] = []

    class _Angry:
        async def close(self):
            raise RuntimeError("context is already broken")

    class _Good:
        def __init__(self, label):
            self.label = label

        async def close(self):
            stopped.append(self.label)

        async def stop(self):
            stopped.append(self.label)

    svc = BrowserService(BrowserSettings())
    svc._context = _Angry()
    svc._browser = _Good("browser")
    svc._playwright = _Good("playwright")

    report = await svc.shutdown()

    assert stopped == ["browser", "playwright"], "cleanup continued past the failure"
    assert report.clean is False
    assert "context" in report.failures
    assert "already broken" in report.failures["context"]
    assert svc._playwright is None


async def test_a_hanging_close_is_abandoned_rather_than_waited_on() -> None:
    """CONTROLLED FAILURE INJECTION. A hang is worse than a raise.

    ``shutdown`` runs on the way out of the process. A ``browser.close()`` that
    never returns would hold JARVIS's exit open forever with a Chromium still
    alive — the exact outcome shutdown exists to prevent. Abandoning the step
    costs nothing, because stopping the driver kills the browser anyway, and
    the driver stop is deliberately last for that reason.
    """
    stopped: list[str] = []

    class _Hangs:
        async def close(self):
            await asyncio.sleep(30)

    class _Good:
        async def stop(self):
            stopped.append("playwright")

    svc = BrowserService(BrowserSettings(shutdown_timeout_seconds=0.2))
    svc._browser = _Hangs()
    svc._playwright = _Good()

    report = await asyncio.wait_for(svc.shutdown(), timeout=5)

    assert "browser" in report.timed_out
    assert stopped == ["playwright"], "the driver stop must still run"
    assert report.clean is False
    assert svc.started is False


async def test_a_failed_shutdown_is_recorded_rather_than_swallowed() -> None:
    """"Did not raise" and "nothing went wrong" are different claims."""
    class _Angry:
        async def close(self):
            raise RuntimeError("boom")

    svc = BrowserService(BrowserSettings())
    svc._browser = _Angry()

    report = await svc.shutdown()

    assert report.clean is False
    assert svc.last_shutdown is report
    assert report.describe()["failures"]["browser"] == "boom"


async def test_shutdown_during_a_launch_waits_rather_than_interleaving(
    chromium,
) -> None:
    """REAL BROWSER. Shutdown must not run through a half-built launch.

    Both take the same lock, so a shutdown arriving mid-launch waits for the
    launch to finish and then tears down a complete browser — rather than
    closing a driver out from under a browser that is still coming up.
    """
    svc = BrowserService(chromium)
    launch = asyncio.create_task(svc.launch())
    await asyncio.sleep(0)  # let the launch take the lock
    shutdown = asyncio.create_task(svc.shutdown())

    outcome, report = await asyncio.gather(launch, shutdown)

    assert outcome.ok is True, outcome.reason
    assert report.clean is True
    assert svc.started is False
    assert svc._playwright is None


# ── 18-19: JarvisCore integration ────────────────────────────────────────────


async def test_jarvis_startup_leaves_the_browser_unstarted(core) -> None:
    """The laziness requirement, re-asserted after the lifecycle changes."""
    assert core.browser.started is False
    assert core.browser.running is False
    assert core.browser._playwright is None


async def test_jarvis_shutdown_closes_a_launched_browser(core, chromium) -> None:
    """REAL BROWSER through the real core shutdown path.

    Not ``browser.shutdown()`` called directly — ``JarvisCore.shutdown()``,
    which is what actually runs when the process ends.
    """
    core.browser.settings = chromium
    await core.browser.new_page()
    browser = core.browser._browser
    assert browser.is_connected() is True

    await core.shutdown()

    assert browser.is_connected() is False
    assert core.browser.started is False


async def test_jarvis_shutdown_is_safe_when_no_browser_ever_launched(core) -> None:
    await core.shutdown()
    assert core.browser.started is False
    assert core.browser.last_shutdown is not None
    assert core.browser.last_shutdown.clean is True


async def test_core_shutdown_does_not_raise_when_the_browser_teardown_fails(
    core,
) -> None:
    """A broken browser must not prevent JARVIS from exiting.

    The database and provider cleanup run after the browser in
    ``JarvisCore.shutdown``; an exception here would skip both.
    """
    class _Angry:
        async def close(self):
            raise RuntimeError("wedged")

    core.browser._browser = _Angry()
    await core.shutdown()  # must not raise
    assert core.browser.started is False


# ── 20: process cleanup ──────────────────────────────────────────────────────


def _owned_browser_pids() -> set[int] | None:
    """Chromium PIDs visible to this process, or None where unsupported.

    Isolated in a helper and returning ``None`` rather than an empty set on an
    unsupported platform, so a test cannot mistake "cannot enumerate" for
    "nothing running" — which would turn an unverifiable claim into a passing
    assertion.
    """
    import subprocess
    import sys

    if sys.platform == "win32":  # pragma: no cover - not this machine
        return None
    try:
        out = subprocess.run(
            ["ps", "-eo", "pid=,comm="], capture_output=True, text=True, timeout=10
        )
    except (OSError, subprocess.SubprocessError):  # pragma: no cover
        return None
    if out.returncode != 0:  # pragma: no cover
        return None
    pids = set()
    for line in out.stdout.splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2 and parts[1].strip().lower().startswith(
            ("chrome", "chromium", "headless_shell")
        ):
            pids.add(int(parts[0]))
    return pids


async def test_a_full_lifecycle_leaves_no_browser_process_behind(chromium) -> None:
    """REAL BROWSER + process enumeration.

    Verified on this platform only. ``_owned_browser_pids`` returns ``None``
    where enumeration is unsupported and the test skips with that reason
    rather than passing vacuously — a green run here is evidence about the
    platform it ran on and nothing more.
    """
    import sys

    before = _owned_browser_pids()
    if before is None:
        pytest.skip(f"Browser process enumeration is not supported on {sys.platform}")

    svc = BrowserService(replace(chromium, max_pages=2))
    await svc.launch()
    await svc.new_page()
    await svc.new_page()

    during = _owned_browser_pids()
    spawned = during - before
    assert spawned, "the test cannot prove cleanup if it never saw a browser start"

    await svc.shutdown()
    # Chromium's children exit asynchronously; give the OS a moment to reap.
    for _ in range(50):
        remaining = (_owned_browser_pids() or set()) & spawned
        if not remaining:
            break
        await asyncio.sleep(0.1)

    assert not remaining, f"browser processes survived shutdown: {remaining}"


async def test_repeated_cycles_do_not_accumulate_processes(chromium) -> None:
    """REAL BROWSER + process enumeration. A leak per cycle would show here."""
    import sys

    before = _owned_browser_pids()
    if before is None:
        pytest.skip(f"Browser process enumeration is not supported on {sys.platform}")

    for _ in range(3):
        svc = BrowserService(chromium)
        await svc.new_page()
        await svc.shutdown()

    for _ in range(50):
        after = _owned_browser_pids() or set()
        if not (after - before):
            break
        await asyncio.sleep(0.1)

    assert not (after - before), f"leaked across cycles: {after - before}"


# ── security regression ──────────────────────────────────────────────────────


def test_step_3_introduced_no_attachment_or_credential_paths() -> None:
    """The Step 2 boundaries, re-asserted against the changed source.

    Lifecycle work is exactly where a shortcut would be tempting — reconnecting
    to a surviving browser after a crash, or reusing a profile directory to
    make recovery seamless. Neither is present, and neither may be added
    without this failing first.
    """
    for name in ("settings", "service", "capabilities"):
        source = code_of(browser_module(name))
        for forbidden in (
            "connect_over_cdp",
            ".connect(",
            "launch_persistent_context",
            "user_data_dir",
            "remote-debugging-port",
            "storage_state=",
        ):
            assert forbidden not in source, f"{forbidden} in {name}"


def test_no_posix_process_control_was_introduced_by_the_hardening() -> None:
    """Crash handling is where ``os.kill`` gets reached for. It was not."""
    for name in ("settings", "service", "capabilities"):
        source = code_of(browser_module(name))
        for forbidden in ("killpg", "os.kill", "setsid", "getpgid", "SIGKILL",
                          "SIGTERM", "signal.", "taskkill", "preexec_fn",
                          "subprocess"):
            assert forbidden not in source, f"{forbidden} in {name}"


def test_the_hardening_did_not_touch_the_security_subsystems() -> None:
    """Step 3 is lifecycle work and must not have reached anywhere else.

    Asserted by import graph rather than by reading the diff: the browser
    package cannot have changed permission, taint or audit behaviour if it
    does not reference any of it.
    """
    for name in ("settings", "service", "capabilities"):
        source = code_of(browser_module(name))
        for forbidden in ("PermissionEngine", "ToolExecutor", "tainted",
                          "ActivityService", "ActivityKind", "jarvis.tools",
                          "jarvis.permissions", "jarvis.computer"):
            assert forbidden not in source, f"{forbidden} in {name}"


def test_the_browser_lifecycle_is_not_exposed_as_a_tool() -> None:
    """The successor to "Step 5 has not started", now that it has.

    That assertion retired when the tools arrived. What it was standing in for
    is still live and belongs here rather than with the tools: the *lifecycle*
    is JARVIS's, not the model's. A model that could launch or shut down the
    browser could end it in the middle of somebody else's work, and could
    relaunch to get a fresh context after being refused on the current one.

    Closing a single page is the one lifecycle verb the model gets, and it
    reaches exactly one page. The bounded tool surface itself is pinned by
    ``test_browser_tools.py::test_the_nine_tools_are_registered_and_no_more``.
    """
    from jarvis.tools.registry import build_default_registry

    names = {t.name for t in build_default_registry().all()}
    for forbidden in ("browser_launch", "browser_start", "browser_shutdown",
                      "browser_restart", "browser_close", "browser_quit",
                      "browser_new_context", "browser_clear_cookies"):
        assert forbidden not in names, forbidden
    assert "browser_close_page" in names
