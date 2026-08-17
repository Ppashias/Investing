"""Browser control (Phase 4).

The runtime and its control plane: lifecycle, URL policy, origin permissions,
element references and the credential boundary. The agent-facing tools are
Step 5 and are not here — every boundary exists first, so the tools are written
against decisions that already refuse.

Independent of :mod:`jarvis.computer` by construction: nothing in this package
imports it, and browser control works on Windows where the X11 desktop backend
cannot exist at all.
"""

from jarvis.browser.elements import (
    ElementEntry,
    ElementRef,
    ElementRegistry,
    ElementReferenceError,
    StaleElement,
    UnknownElement,
    WrongPage,
)
from jarvis.browser.capabilities import (
    BrowserAvailability,
    BrowserCapabilityReport,
    BrowserError,
    BrowserUnavailable,
    detect,
)
from jarvis.browser.service import (
    BrowserService,
    LaunchOutcome,
    PageHandle,
    ShutdownReport,
)
from jarvis.browser.policy import (
    BrowserAuthorisation,
    BrowserOperation,
    BrowserPolicy,
    CredentialRefused,
    FieldInspection,
    credential_reason,
    refuse_if_credential,
    resource_for,
)
from jarvis.browser.settings import BrowserSettings
from jarvis.browser.urls import UrlDecision, UrlPolicy, UrlVerdict

__all__ = [
    "BrowserAuthorisation",
    "BrowserAvailability",
    "BrowserCapabilityReport",
    "BrowserError",
    "BrowserOperation",
    "BrowserPolicy",
    "BrowserService",
    "BrowserSettings",
    "BrowserUnavailable",
    "CredentialRefused",
    "ElementEntry",
    "ElementRef",
    "ElementReferenceError",
    "ElementRegistry",
    "FieldInspection",
    "LaunchOutcome",
    "PageHandle",
    "ShutdownReport",
    "StaleElement",
    "UnknownElement",
    "UrlDecision",
    "UrlPolicy",
    "UrlVerdict",
    "WrongPage",
    "credential_reason",
    "detect",
    "refuse_if_credential",
    "resource_for",
]
