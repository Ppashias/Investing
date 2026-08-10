"""Browser control (Phase 4).

The runtime only. Navigation policy, origin permissions, element references,
taint propagation and the agent-facing tools are later steps and are not here.

Independent of :mod:`jarvis.computer` by construction: nothing in this package
imports it, and browser control works on Windows where the X11 desktop backend
cannot exist at all.
"""

from jarvis.browser.capabilities import (
    BrowserAvailability,
    BrowserCapabilityReport,
    BrowserError,
    BrowserUnavailable,
    detect,
)
from jarvis.browser.service import BrowserService, LaunchOutcome, PageHandle
from jarvis.browser.settings import BrowserSettings

__all__ = [
    "BrowserAvailability",
    "BrowserCapabilityReport",
    "BrowserError",
    "BrowserService",
    "BrowserSettings",
    "BrowserUnavailable",
    "LaunchOutcome",
    "PageHandle",
    "detect",
]
