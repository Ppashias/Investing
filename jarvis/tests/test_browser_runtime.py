"""The Phase 4 browser runtime (Step 2).

Lifecycle, capability detection and isolation. No navigation, no policy, no
tools — those are later steps and nothing here anticipates them.

## Real browsers, and what happens when there isn't one

Tests that need a browser use the real thing: a Chromium process, a real
context, real pages. Mocks are used only where the point *is* the failure —
a missing package, a launch that raises — because a mock cannot demonstrate
that a browser shuts down.

If Chromium cannot be resolved on this machine the browser-requiring tests skip
with the resolver's own reason rather than a generic one, so a skipped run says
*why* rather than merely that it happened. The failure-path and no-launch tests
never skip: they are the ones that must hold on a machine with no browser at
all, which is precisely the machine most likely to be running them.
"""

from __future__ import annotations

import inspect
from dataclasses import replace
from pathlib import Path

import pytest

from jarvis.browser import (
    BrowserAvailability,
    BrowserService,
    BrowserSettings,
    BrowserUnavailable,
    detect,
)
from jarvis.browser.capabilities import BrowserError


def code_of(module) -> str:
    """The module's executable source, with docstrings and comments removed.

    The absence-assertions below are about what the code *does*, and prose is
    not code. Scanning raw source makes a docstring that explains why
    ``connect_over_cdp`` is forbidden trip the test forbidding it — which would
    train the next person to delete the explanation rather than keep the rule.
    """
    import ast

    tree = ast.parse(inspect.getsource(module))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)) and ast.get_docstring(node):
            node.body = node.body[1:] or [ast.Pass()]
    return ast.unparse(tree)


BROWSER_MODULES = ("settings", "service", "capabilities")


def browser_module(name: str):
    import importlib

    return importlib.import_module(f"jarvis.browser.{name}")


def _environment_chromium() -> Path | None:
    """A Chromium this machine has, found from the environment, not hard-coded.

    Playwright's bundled build can be a version behind or ahead of the
    installed package — this container has exactly that mismatch. Rather than
    skip every real-browser test on such a machine, the fixture looks under
    ``PLAYWRIGHT_BROWSERS_PATH`` for a build that is actually present. The path
    comes from the environment, so nothing machine-specific is written down.
    """
    import os

    root = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if not root or not Path(root).is_dir():
        return None
    for candidate in sorted(Path(root).glob("chromium-*/chrome-linux/chrome")):
        if candidate.is_file():
            return candidate
    for candidate in sorted(Path(root).glob("chromium-*/chrome-win/chrome.exe")):
        if candidate.is_file():
            return candidate
    return None


_UNRESOLVED = object()
_resolved: object = _UNRESOLVED


async def resolve_chromium() -> tuple[BrowserSettings | None, str]:
    """Work out once how to launch a real browser here, and remember it.

    Cached across the whole test session because ``detect`` starts a Playwright
    driver process to ask Playwright where its Chromium is. Roughly thirty
    tests want a browser, and paying a node process launch per test is pure
    subprocess churn on a machine that is simultaneously starting Chromiums and
    an Xvfb.

    ``--no-sandbox`` and ``--disable-dev-shm-usage`` are not defaults in
    :class:`BrowserSettings` and must not become them. They are set here
    because CI containers run unprivileged and can have a small ``/dev/shm`` —
    facts about the test machine, not about JARVIS.
    """
    global _resolved
    if _resolved is not _UNRESOLVED:
        return _resolved  # type: ignore[return-value]

    args = ("--no-sandbox", "--disable-dev-shm-usage")
    report = await detect(BrowserSettings())
    if report.available:
        _resolved = (BrowserSettings(headless=True, launch_args=args), report.reason)
    else:
        fallback = _environment_chromium()
        if fallback is None:
            _resolved = (
                None,
                f"{report.reason} (and none found under PLAYWRIGHT_BROWSERS_PATH)",
            )
        else:
            _resolved = (
                BrowserSettings(
                    headless=True, executable_path=fallback, launch_args=args
                ),
                report.reason,
            )
    return _resolved  # type: ignore[return-value]


@pytest.fixture
async def chromium() -> BrowserSettings:
    """Settings for a real browser, or an explicit skip saying why not."""
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


# ── settings ─────────────────────────────────────────────────────────────────


def test_settings_default_to_the_conservative_end_of_everything() -> None:
    s = BrowserSettings()
    assert s.enabled is True
    assert s.headless is True
    assert s.executable_path is None
    assert s.storage_dir is None
    assert s.persists_storage is False
    assert s.launch_args == ()
    assert s.max_pages > 0


def test_settings_load_from_the_existing_configuration_system() -> None:
    """One configuration system, not two.

    The browser reads :class:`jarvis.config.Settings` like every other
    subsystem — same ``JARVIS_`` prefix, same ``.env`` handling, same
    validation — rather than introducing a parallel mechanism.
    """
    from jarvis.config import Settings

    configured = Settings(
        browser_enabled=False,
        browser_headless=False,
        browser_max_pages=2,
        browser_executable_path=Path("/somewhere/chrome"),
        browser_storage_dir=Path("/somewhere/state"),
    )
    assert configured.browser_enabled is False
    assert configured.browser_headless is False
    assert configured.browser_max_pages == 2
    assert configured.browser_executable_path == Path("/somewhere/chrome")

    summary = configured.public_dict()["browser"]
    assert summary["enabled"] is False
    assert summary["persists_storage"] is True
    assert summary["executable_configured"] is True
    # The path itself is not in the summary: it goes over the API, and a
    # directory layout is the operator's business.
    assert "/somewhere" not in str(summary)


def test_settings_paths_are_not_machine_specific() -> None:
    """No path discovered during reconnaissance is baked into the source.

    ``/opt/pw-browsers`` is where this container happens to keep browsers. A
    default naming it would work here and nowhere else, and would silently
    stop being true the moment the image changed.
    """
    for name in BROWSER_MODULES:
        source = code_of(browser_module(name))
        assert "/opt/pw-browsers" not in source, name
        assert "C:\\Program Files" not in source, name
        assert "/usr/bin/chromium" not in source, name


# ── laziness ─────────────────────────────────────────────────────────────────


def test_constructing_the_service_starts_nothing() -> None:
    svc = BrowserService(BrowserSettings())
    assert svc.started is False
    assert svc.running is False
    assert svc.page_count == 0
    assert svc.capabilities.state is BrowserAvailability.UNPROBED


def test_an_unprobed_service_does_not_claim_to_be_available() -> None:
    """UNPROBED exists so "we have not looked" is not reported as "yes".

    Detection starts a driver process, so it is deliberately lazy; the cost of
    laziness is a state that must not be rounded up to AVAILABLE.
    """
    svc = BrowserService(BrowserSettings())
    assert svc.capabilities.available is False
    with pytest.raises(BrowserUnavailable):
        svc.capabilities.require()


def test_describe_never_launches_a_browser() -> None:
    svc = BrowserService(BrowserSettings())
    snapshot = svc.describe()
    assert snapshot["started"] is False
    assert snapshot["running"] is False
    assert snapshot["isolated_context"] is False
    assert snapshot["pages"] == []
    assert svc.started is False


async def test_jarvis_startup_does_not_launch_a_browser(core) -> None:
    """The whole point of lazy launch, asserted against the real core.

    ``core`` has completed ``JarvisCore.startup``. A Chromium process at this
    moment would mean every JARVIS start pays hundreds of megabytes for a
    capability most turns never use.
    """
    assert core.browser.started is False
    assert core.browser.running is False
    assert core.browser.capabilities.state is BrowserAvailability.UNPROBED


async def test_core_shutdown_is_safe_when_the_browser_never_launched(core) -> None:
    """Shutdown reaches the browser whether or not there is one."""
    await core.browser.shutdown()
    assert core.browser.started is False


# ── capability detection ─────────────────────────────────────────────────────


async def test_detection_reports_disabled_separately_from_unavailable() -> None:
    """Two different answers, because they have two different fixes.

    "You switched it off" is undone in a config file. "There is no Chromium" is
    not. Collapsing them into "off" makes the remedy unguessable.
    """
    report = await detect(BrowserSettings(enabled=False))
    assert report.state is BrowserAvailability.DISABLED
    assert report.available is False
    assert "switched off" in report.reason
    assert report.state is not BrowserAvailability.UNAVAILABLE


async def test_detection_reports_a_missing_playwright_package() -> None:
    """The package genuinely absent, simulated by breaking its import."""
    import builtins

    real_import = builtins.__import__

    def _no_playwright(name, *args, **kwargs):
        if name == "playwright" or name.startswith("playwright."):
            raise ImportError("No module named 'playwright'")
        return real_import(name, *args, **kwargs)

    builtins.__import__ = _no_playwright
    try:
        report = await detect(BrowserSettings())
    finally:
        builtins.__import__ = real_import

    assert report.state is BrowserAvailability.UNAVAILABLE
    assert report.playwright_installed is False
    assert "playwright package is not installed" in report.reason
    assert "pip install playwright" in report.reason


async def test_a_configured_executable_that_does_not_exist_is_reported_as_such(
    tmp_path: Path,
) -> None:
    """Naming a binary overrules everything, including the fallback.

    Falling through to Playwright's own Chromium here would launch a browser
    the operator did not ask for, and the misconfiguration would never
    surface — the worst combination.
    """
    missing = tmp_path / "nowhere" / "chrome.exe"
    report = await detect(BrowserSettings(executable_path=missing))

    assert report.state is BrowserAvailability.UNAVAILABLE
    assert "does not exist" in report.reason
    assert str(missing) in report.reason
    assert report.executable_source is None


async def test_a_configured_executable_that_exists_is_used(tmp_path: Path) -> None:
    """Resolution order step 1, asserted without launching it."""
    fake = tmp_path / "chrome"
    fake.write_text("#!/bin/sh\n", encoding="utf-8")

    report = await detect(BrowserSettings(executable_path=fake))
    assert report.state is BrowserAvailability.AVAILABLE
    assert report.executable_source == "configured"
    assert report.executable_path == str(fake)


async def test_detection_does_not_claim_availability_from_the_import_alone() -> None:
    """Playwright importing proves a package, not a browser.

    Whatever this machine reports, an AVAILABLE answer must be backed by an
    executable that exists on disk — never by ``import playwright`` succeeding.
    """
    report = await detect(BrowserSettings())
    if report.available:
        assert report.executable_path is not None
        assert Path(report.executable_path).is_file()
        assert report.executable_source in {"configured", "playwright"}
    else:
        assert report.reason.startswith("Browser unavailable")


async def test_detection_never_reports_a_browser_as_verified() -> None:
    """``available`` and ``verified`` are different claims.

    Detection can say "nothing rules this out". Only a launch can say "it
    worked", and conflating the two is how a subsystem reports success it
    never observed.
    """
    report = await detect(BrowserSettings())
    assert report.verified is False


async def test_the_report_serialises_for_the_api() -> None:
    payload = (await detect(BrowserSettings(enabled=False))).to_dict()
    assert payload["state"] == "DISABLED"
    assert payload["available"] is False
    assert payload["verified"] is False
    assert "reason" in payload and payload["reason"]
    assert set(payload) >= {"os", "playwright", "executable", "notes"}


async def test_detection_is_cached_until_refreshed(monkeypatch) -> None:
    """The probe starts a process; the status endpoint must not pay for it."""
    from jarvis.browser import service as service_module

    calls = 0

    async def counting_detect(settings):
        nonlocal calls
        calls += 1
        from jarvis.browser.capabilities import BrowserCapabilityReport

        report = BrowserCapabilityReport()
        report.state = BrowserAvailability.UNAVAILABLE
        report.reason = "Browser unavailable — stub."
        return report

    monkeypatch.setattr(service_module, "detect", counting_detect)
    svc = BrowserService(BrowserSettings())

    await svc.detect()
    await svc.detect()
    assert calls == 1

    await svc.detect(refresh=True)
    assert calls == 2


# ── platform ─────────────────────────────────────────────────────────────────


async def test_windows_is_not_reported_unavailable_for_being_windows(
    monkeypatch,
) -> None:
    """The whole reason Phase 4 exists, asserted directly.

    Phase 3 correctly reports no desktop control on Windows, because X11
    automation cannot drive a Windows desktop. Browser control has no such
    limitation — Chromium runs there identically — and the two must not be
    conflated. This machine's Chromium determines the answer; the platform
    string must not.
    """
    import platform

    monkeypatch.setattr(platform, "system", lambda: "Windows")
    monkeypatch.setattr(platform, "release", lambda: "11")

    windows = await detect(BrowserSettings())
    monkeypatch.undo()
    here = await detect(BrowserSettings())

    assert windows.os_name == "Windows"
    assert windows.state is here.state
    assert "Windows" not in windows.reason


def test_the_browser_subsystem_does_not_import_the_desktop_backend() -> None:
    """Browser control is independent of Phase 3, by construction.

    A single import of ``jarvis.computer`` would make browser control depend on
    a subsystem that cannot work on the target machine — asserted against the
    source rather than trusted, because an import added later would be silent.
    """
    for name in BROWSER_MODULES:
        source = code_of(browser_module(name))
        assert "jarvis.computer" not in source, name
        assert "Xvfb" not in source, name
        assert "DISPLAY" not in source, name
        assert "Xlib" not in source, name


def test_no_posix_only_process_management_is_used() -> None:
    """Playwright owns the browser process on every platform.

    ``killpg``, ``setsid`` and ``SIGKILL`` do not exist on Windows, and reaching
    for them would make teardown work on the development machine and fail on
    the target one.
    """
    for name in BROWSER_MODULES:
        source = code_of(browser_module(name))
        for forbidden in ("killpg", "setsid", "getpgid", "SIGKILL", "SIGTERM",
                          "preexec_fn", "os.fork"):
            assert forbidden not in source, f"{forbidden} in {name}"


# ── isolation ────────────────────────────────────────────────────────────────


def test_the_service_cannot_attach_to_an_existing_browser() -> None:
    """The security property that motivates the whole subsystem.

    Attaching to the user's browser hands over every logged-in session at once
    and does so invisibly. The absence of these calls is the enforcement, so it
    is asserted rather than assumed — an added ``connect_over_cdp`` would
    otherwise be an ordinary-looking three-word diff.
    """
    source = code_of(browser_module("service"))
    for forbidden in (
        "connect_over_cdp",
        ".connect(",
        "launch_persistent_context",
        "remote-debugging-port",
        "user_data_dir",
        "userDataDir",
    ):
        assert forbidden not in source, f"{forbidden} must not appear"
    assert "chromium.launch(" in source


def test_storage_is_ephemeral_unless_explicitly_configured() -> None:
    """Staying logged in between sessions must be a decision, not a default."""
    assert BrowserSettings().persists_storage is False
    assert BrowserSettings(storage_dir=Path("/tmp/x")).persists_storage is True


def test_no_credential_handling_exists_in_the_runtime() -> None:
    """Nothing here can type a password, because nothing here types anything.

    Step 2 has no input capability at all. The assertion is cheap now and
    becomes the thing that notices if a later step adds one to the wrong layer.

    ``fill(`` is matched with its leading dot as of Step 12, because the
    navigation guard calls ``route.fulfill()`` and the bare token matched it as
    a substring. The rule is unchanged — ``locator.fill(...)`` is still banned
    in every module here, which is what "nothing types anything" means.
    """
    for name in BROWSER_MODULES:
        source = code_of(browser_module(name)).lower()
        for forbidden in ("password", "credential(", "def login", "keyring",
                          "enter_password", ".fill(", "type("):
            assert forbidden not in source, f"{forbidden} in {name}"


# ── launch failure ───────────────────────────────────────────────────────────


async def test_an_unavailable_browser_returns_an_outcome_rather_than_raising() -> None:
    """JARVIS must keep running on a machine with no browser.

    A machine without Chromium is not a broken JARVIS; it is a JARVIS that
    cannot browse and should say so. Raising here would make an ordinary,
    expected condition look like a crash.
    """
    svc = BrowserService(BrowserSettings(enabled=False))
    outcome = await svc.launch()

    assert outcome.ok is False
    assert outcome.reason and "switched off" in outcome.reason
    assert svc.started is False
    with pytest.raises(BrowserUnavailable):
        outcome.require()


async def test_a_launch_that_raises_leaves_no_partial_state(monkeypatch) -> None:
    """Half a browser is worse than none.

    A Playwright driver with no browser, or a browser with no context, would
    leave the service holding resources it cannot use and cannot describe. The
    launch path unwinds whatever came up before returning the failure.
    """
    svc = BrowserService(BrowserSettings())
    monkeypatch.setattr(
        svc, "detect", _stub_report_factory(BrowserAvailability.AVAILABLE)
    )

    async def _explode(report):
        svc._playwright = object()  # something did come up…
        raise RuntimeError("chromium refused to start")

    monkeypatch.setattr(svc, "_start_locked", _explode)

    outcome = await svc.launch()
    assert outcome.ok is False
    assert "chromium refused to start" in outcome.reason
    assert svc.started is False
    assert svc.running is False
    assert svc._playwright is None
    assert svc.capabilities.verified is False


async def test_a_failed_launch_does_not_break_jarvis_shutdown(monkeypatch) -> None:
    svc = BrowserService(BrowserSettings())
    monkeypatch.setattr(
        svc, "detect", _stub_report_factory(BrowserAvailability.AVAILABLE)
    )

    async def _explode(report):
        raise RuntimeError("nope")

    monkeypatch.setattr(svc, "_start_locked", _explode)
    await svc.launch()
    await svc.shutdown()  # must not raise
    assert svc.started is False


def _stub_report_factory(state: BrowserAvailability):
    from jarvis.browser.capabilities import BrowserCapabilityReport

    async def _detect(refresh: bool = False) -> BrowserCapabilityReport:
        report = BrowserCapabilityReport()
        report.state = state
        report.reason = "stub"
        report.executable_source = "playwright"
        return report

    return _detect


# ── crash handling ───────────────────────────────────────────────────────────


async def test_a_dead_browser_is_not_reported_as_running(monkeypatch) -> None:
    """``started`` and ``running`` answer different questions.

    A crashed browser has still been started. Answering "may I use it?" from
    ``started`` is how a service ends up handing out pages on a browser that
    no longer exists.
    """
    svc = BrowserService(BrowserSettings())

    class _DeadBrowser:
        def is_connected(self):
            return False

    svc._browser = _DeadBrowser()
    assert svc.started is True
    assert svc.running is False


async def test_a_browser_that_cannot_answer_is_treated_as_gone() -> None:
    svc = BrowserService(BrowserSettings())

    class _Unaskable:
        def is_connected(self):
            raise RuntimeError("connection closed")

    svc._browser = _Unaskable()
    assert svc.running is False


# ── shutdown ─────────────────────────────────────────────────────────────────


async def test_shutdown_is_safe_before_anything_started() -> None:
    svc = BrowserService(BrowserSettings())
    await svc.shutdown()
    await svc.shutdown()
    assert svc.started is False


async def test_shutdown_survives_a_partially_initialised_service() -> None:
    """Teardown runs on the way out of a failure, so it meets broken objects.

    Each resource is closed independently: a context whose close raises must
    not prevent the browser from closing, or a JARVIS that failed mid-launch
    leaves Chromium running.
    """
    svc = BrowserService(BrowserSettings())

    class _Angry:
        async def close(self):
            raise RuntimeError("already gone")

        async def stop(self):
            raise RuntimeError("already gone")

    svc._context = _Angry()
    svc._browser = _Angry()
    svc._playwright = _Angry()

    await svc.shutdown()

    assert svc._context is None
    assert svc._browser is None
    assert svc._playwright is None
    assert svc.started is False


async def test_shutdown_clears_page_bookkeeping() -> None:
    from jarvis.browser.service import PageHandle

    svc = BrowserService(BrowserSettings())

    class _Page:
        def is_closed(self):
            return False

        async def close(self):
            return None

    svc._pages["pg_1"] = PageHandle(page_id="pg_1", page=_Page())
    await svc.shutdown()
    assert svc.page_count == 0


# ── real browser ─────────────────────────────────────────────────────────────


async def test_a_real_browser_launches_and_reports_verified(service) -> None:
    outcome = await service.launch()

    assert outcome.ok is True, outcome.reason
    assert service.started is True
    assert service.running is True
    assert service.capabilities.verified is True
    assert service.describe()["isolated_context"] is True


async def test_launch_is_idempotent(service) -> None:
    first = await service.launch()
    browser = service._browser
    second = await service.launch()

    assert first.ok and second.ok
    assert service._browser is browser, "a second launch must not spawn a second browser"


async def test_concurrent_first_use_launches_one_browser(service) -> None:
    """Two callers arriving at once must not leak a browser.

    Without the lock both see ``running is False``, both launch, and one of the
    two Chromium processes ends up with nothing referencing it — the exact
    orphan the lifecycle is supposed to prevent.
    """
    import asyncio

    outcomes = await asyncio.gather(*(service.launch() for _ in range(4)))
    assert all(o.ok for o in outcomes)
    assert service.running is True


async def test_pages_open_inside_the_isolated_context(service) -> None:
    handle = await service.new_page()

    assert handle.page_id.startswith("pg_")
    assert handle.closed is False
    assert service.page_count == 1
    # The page belongs to the context the service created, not to some other
    # context or a default profile.
    assert handle.page.context is service._context


async def test_the_context_is_isolated_from_a_second_context(service) -> None:
    """Cookie isolation, demonstrated rather than asserted from the API name.

    A cookie set in JARVIS's context must not be visible in another context of
    the same browser — which is the property that makes "what is JARVIS logged
    into?" a question with an answer.
    """
    await service.launch()
    page = (await service.new_page()).page
    await page.goto("data:text/html,<html><body>ok</body></html>")
    await service._context.add_cookies(
        [{"name": "jarvis", "value": "1", "url": "https://example.invalid"}]
    )

    other = await service._browser.new_context()
    try:
        assert await other.cookies() == []
        assert len(await service._context.cookies()) == 1
    finally:
        await other.close()


async def test_the_context_starts_with_no_cookies_at_all(service) -> None:
    """No inherited profile: the context begins empty, every time."""
    await service.launch()
    assert await service._context.cookies() == []
    assert service.settings.persists_storage is False


async def test_pages_can_be_closed_and_the_slot_reused(service) -> None:
    handle = await service.new_page()
    assert await service.close_page(handle.page_id) is True
    assert service.page_count == 0
    assert await service.close_page(handle.page_id) is False

    again = await service.new_page()
    assert again.page_id != handle.page_id


async def test_a_closed_page_cannot_be_looked_up(service) -> None:
    handle = await service.new_page()
    await handle.page.close()

    with pytest.raises(BrowserError) as caught:
        service.page(handle.page_id)
    assert "closed" in str(caught.value)


async def test_an_unknown_page_id_is_refused(service) -> None:
    await service.launch()
    with pytest.raises(BrowserError):
        service.page("pg_fabricated")


async def test_the_page_cap_is_enforced(chromium: BrowserSettings) -> None:
    # ``replace`` rather than a fresh BrowserSettings: the fixture may carry an
    # executable_path this machine needs, and rebuilding from scratch drops it.
    svc = BrowserService(replace(chromium, max_pages=2))
    try:
        await svc.new_page()
        await svc.new_page()
        with pytest.raises(BrowserError) as caught:
            await svc.new_page()
        assert "limit is 2" in str(caught.value)
        assert svc.page_count == 2
    finally:
        await svc.shutdown()


async def test_a_page_closed_by_the_page_itself_frees_its_slot(
    chromium: BrowserSettings,
) -> None:
    """The cap must count live pages, not remembered ones.

    A page can close without JARVIS asking — ``window.close()``, a renderer
    crash. If those stay in the ledger the service eventually refuses to open a
    page while holding none.
    """
    svc = BrowserService(replace(chromium, max_pages=1))
    try:
        handle = await svc.new_page()
        await handle.page.close()  # closed behind the service's back
        replacement = await svc.new_page()
        assert replacement.page_id != handle.page_id
        assert svc.page_count == 1
    finally:
        await svc.shutdown()


async def test_a_real_browser_shuts_down_completely(chromium: BrowserSettings) -> None:
    """The acceptance criterion: no Chromium left behind."""
    svc = BrowserService(chromium)
    await svc.new_page()
    browser = svc._browser
    assert browser.is_connected() is True

    await svc.shutdown()

    assert browser.is_connected() is False
    assert svc.started is False
    assert svc.running is False
    assert svc.page_count == 0
    assert svc.capabilities.verified is False


async def test_shutdown_is_idempotent_on_a_real_browser(chromium: BrowserSettings) -> None:
    svc = BrowserService(chromium)
    await svc.launch()
    await svc.shutdown()
    await svc.shutdown()
    await svc.shutdown()
    assert svc.started is False


async def test_the_service_relaunches_after_the_browser_dies(
    chromium: BrowserSettings,
) -> None:
    """A crash must be recoverable, not terminal.

    Closing the browser underneath the service is what a crash looks like from
    here. The next launch has to notice, clear the corpse, and produce a
    working browser rather than either reusing the dead one or leaking it.
    """
    svc = BrowserService(chromium)
    try:
        await svc.launch()
        dead = svc._browser
        await dead.close()  # the crash
        assert svc.running is False

        outcome = await svc.launch()
        assert outcome.ok is True
        assert svc.running is True
        assert svc._browser is not dead
    finally:
        await svc.shutdown()


async def test_repeated_startup_and_shutdown_cycles_leave_nothing_running(
    chromium: BrowserSettings,
) -> None:
    svc = BrowserService(chromium)
    browsers = []
    for _ in range(3):
        assert (await svc.launch()).ok is True
        await svc.new_page()
        browsers.append(svc._browser)
        await svc.shutdown()

    assert all(b.is_connected() is False for b in browsers)
    assert svc.started is False


async def test_an_explicitly_configured_executable_is_the_one_launched() -> None:
    """Resolution order step 1, end to end against a real browser.

    Uses whichever Chromium this machine actually has — Playwright's own if it
    resolves, otherwise the one found under ``PLAYWRIGHT_BROWSERS_PATH`` — and
    points ``executable_path`` at it explicitly. What is being proved is that
    the configured binary wins and launches, not which binary it happens to be.
    """
    report = await detect(BrowserSettings())
    binary = (
        Path(report.executable_path)
        if report.available and report.executable_source == "playwright"
        else _environment_chromium()
    )
    if binary is None:
        pytest.skip(f"No Chromium on this machine to point at: {report.reason}")

    svc = BrowserService(
        BrowserSettings(
            headless=True, executable_path=binary, launch_args=("--no-sandbox",)
        )
    )
    try:
        outcome = await svc.launch()
        assert outcome.ok is True, outcome.reason
        assert svc.capabilities.executable_source == "configured"
        assert svc.capabilities.executable_path == str(binary)
        assert svc.running is True
        # Actually the browser at that path, not a coincidentally working one.
        assert svc._browser.version
    finally:
        await svc.shutdown()
