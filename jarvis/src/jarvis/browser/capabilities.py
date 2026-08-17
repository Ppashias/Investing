"""Can this machine run JARVIS's browser? (Phase 4, §5)

The same instruction Phase 3's :mod:`jarvis.computer.capabilities` follows —
*inspect the environment rather than assume it* — applied to a different
question. What differs is the answer's shape, and the difference is the point:

Phase 3 asks "is there a display?" and a Windows desktop answers **no**, because
X11 automation cannot drive one. Phase 4 asks "is there a browser?" and a
Windows desktop answers **yes**, because Chromium runs there identically to
everywhere else. Browser control is not the desktop backend with a different
name, and this module is where that stops being a claim and becomes code: it
imports nothing from :mod:`jarvis.computer`, and there is no path through it
that consults a display.

## Four states, because "unknown" is a real one

``detect`` starts a Playwright driver process to ask Playwright where its own
Chromium lives. That is cheap next to launching a browser but it is not free,
and JARVIS should not pay it at startup for a user who never browses. So
probing is lazy, and :data:`BrowserAvailability.UNPROBED` is what the report
says before anyone has asked. Reporting ``AVAILABLE`` for "we have not looked"
would be the overclaim this whole module exists to prevent.

## What "available" is allowed to mean

Only that the executable resolved and exists on disk. It is deliberately not
"a browser launched successfully" — that would mean launching one, and this is
the check that decides whether to. :attr:`BrowserCapabilityReport.verified`
records the stronger fact once a launch has actually happened, so the two are
never confused.
"""

from __future__ import annotations

import enum
import platform
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jarvis.browser.settings import BrowserSettings
from jarvis.errors import JarvisError
from jarvis.logging import get_logger

log = get_logger(__name__)


class BrowserUnavailable(JarvisError):
    """The browser cannot be used here, and the reason is in the message."""

    code = "browser_unavailable"
    http_status = 501
    retryable = False

    def __init__(self, message: str, user_message: str | None = None) -> None:
        super().__init__(message, user_message=user_message or message)


class BrowserError(JarvisError):
    """A browser operation failed for a reason that is not availability."""

    code = "browser_error"
    http_status = 400
    retryable = False

    def __init__(self, message: str, user_message: str | None = None) -> None:
        super().__init__(message, user_message=user_message or message)


class BrowserAvailability(str, enum.Enum):
    """Why the browser is or is not usable.

    ``DISABLED`` and ``UNAVAILABLE`` are kept apart on purpose. One is a
    decision the operator made and can unmake in a config file; the other is a
    fact about the machine that a config file will not change. Collapsing them
    into "off" would make the fix unguessable.
    """

    #: Nobody has looked yet. The honest state before the first probe.
    UNPROBED = "UNPROBED"
    #: Playwright and a Chromium executable are both present.
    AVAILABLE = "AVAILABLE"
    #: Something is missing. ``reason`` says what.
    UNAVAILABLE = "UNAVAILABLE"
    #: Switched off in configuration.
    DISABLED = "DISABLED"


@dataclass(slots=True)
class BrowserCapabilityReport:
    """What the browser subsystem can do on this machine."""

    state: BrowserAvailability = BrowserAvailability.UNPROBED
    reason: str = "The browser subsystem has not been probed yet."

    # Looked up through the module at call time rather than bound as
    # ``default_factory=platform.system``: the latter captures the function
    # object when the class is defined, so the value is fixed before any test
    # or caller could influence it.
    os_name: str = field(default_factory=lambda: platform.system())
    os_release: str = field(default_factory=lambda: platform.release())

    #: True when the ``playwright`` package imports.
    playwright_installed: bool = False
    playwright_version: str | None = None

    #: The Chromium binary that would be launched, once resolved.
    executable_path: str | None = None
    #: How it was found — ``configured``, ``playwright``, or ``None``.
    executable_source: str | None = None

    #: True only after a browser has actually launched. ``AVAILABLE`` means
    #: "nothing rules it out"; this means "it worked".
    verified: bool = False

    notes: list[str] = field(default_factory=list)

    @property
    def available(self) -> bool:
        return self.state is BrowserAvailability.AVAILABLE

    def require(self) -> None:
        """Raise with the reason, for callers that cannot proceed without it."""
        if not self.available:
            raise BrowserUnavailable(self.reason)

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "available": self.available,
            "reason": self.reason,
            "verified": self.verified,
            "os": {"name": self.os_name, "release": self.os_release},
            "playwright": {
                "installed": self.playwright_installed,
                "version": self.playwright_version,
            },
            "executable": {
                # The path is operator-facing diagnostics and is a machine
                # path, not a personal one — unlike a vault location, which is
                # deliberately withheld from the API elsewhere.
                "path": self.executable_path,
                "source": self.executable_source,
            },
            "notes": list(self.notes),
        }


def _playwright_version() -> str | None:
    try:
        from importlib.metadata import PackageNotFoundError, version

        return version("playwright")
    except (ImportError, PackageNotFoundError):  # pragma: no cover - trivial
        return None


async def detect(settings: BrowserSettings | None = None) -> BrowserCapabilityReport:
    """Probe for a usable browser. Never raises, never launches one.

    Resolution order is §5's, and the order matters more than any single step:

    1. **The configured executable.** An operator who named a binary has
       overruled everything else, and if that binary is missing the answer is
       "the one you configured is not there" — not a silent fall-through to a
       different browser than the one they asked for.
    2. **Playwright's own resolution.** The normal path, and the one that works
       on Windows after ``playwright install chromium``. Asked *of Playwright*
       rather than reconstructed: the bundled-browser layout is Playwright's
       to know, and a reimplementation of it here would be a hard-coded path
       wearing a function's clothes.
    3. **Unavailable, with the reason.**
    """
    settings = settings or BrowserSettings()
    report = BrowserCapabilityReport()

    if not settings.enabled:
        report.state = BrowserAvailability.DISABLED
        report.reason = (
            "Browser control is switched off (JARVIS_BROWSER_ENABLED=false)."
        )
        return report

    try:
        import playwright  # noqa: F401
        from playwright.async_api import async_playwright
    except ImportError as exc:
        report.state = BrowserAvailability.UNAVAILABLE
        report.reason = (
            "Browser unavailable — the playwright package is not installed. "
            "Install it with 'pip install playwright' and fetch a browser with "
            "'playwright install chromium'."
        )
        report.notes.append(str(exc))
        return report

    report.playwright_installed = True
    report.playwright_version = _playwright_version()

    # 1. Explicitly configured.
    if settings.executable_path is not None:
        configured = Path(settings.executable_path)
        if configured.is_file():
            report.state = BrowserAvailability.AVAILABLE
            report.executable_path = str(configured)
            report.executable_source = "configured"
            report.reason = (
                f"Browser available — using the configured Chromium at "
                f"{configured}."
            )
            return report
        report.state = BrowserAvailability.UNAVAILABLE
        report.reason = (
            f"Browser unavailable — the configured Chromium executable "
            f"{configured} does not exist. Correct "
            "JARVIS_BROWSER_EXECUTABLE_PATH or unset it to use Playwright's "
            "own browser."
        )
        return report

    # 2. Playwright's own. Starting the driver is a small node process, not a
    #    browser; it is what makes this a real check rather than an import.
    try:
        async with async_playwright() as pw:
            resolved = pw.chromium.executable_path
    except Exception as exc:  # driver missing, incompatible, or unrunnable
        report.state = BrowserAvailability.UNAVAILABLE
        report.reason = (
            "Browser unavailable — the Playwright driver could not be started, "
            "so Chromium could not be resolved."
        )
        report.notes.append(str(exc))
        return report

    if resolved and Path(resolved).is_file():
        report.state = BrowserAvailability.AVAILABLE
        report.executable_path = resolved
        report.executable_source = "playwright"
        report.reason = "Browser available — Playwright's Chromium is installed."
        return report

    report.state = BrowserAvailability.UNAVAILABLE
    report.executable_path = resolved or None
    report.reason = (
        "Browser unavailable — Playwright is installed but its Chromium "
        "executable could not be resolved. Run 'playwright install chromium'."
    )
    if resolved:
        report.notes.append(f"Playwright expected a browser at {resolved}.")
    if shutil.which("chromium") or shutil.which("google-chrome"):
        # Worth saying, and worth *not* using. A system Chromium may well be a
        # working browser; it is not the one Playwright was built against, and
        # silently substituting it would trade a clear error for a subtle one.
        report.notes.append(
            "A system Chromium is on PATH. JARVIS does not use it "
            "automatically — set JARVIS_BROWSER_EXECUTABLE_PATH to choose it "
            "deliberately."
        )
    return report
