"""Where JARVIS's browser is allowed to go (Phase 4, Step 4B).

A browser that will fetch any URL a model produces is a request-forgery engine
pointed at the inside of the user's network. It runs on their machine, behind
their firewall, with their routing table, so ``http://192.168.1.1/`` or
``http://169.254.169.254/latest/meta-data/`` reaches things nothing on the
public internet can. The model does not have to be malicious for this to
happen; a page it read only has to suggest a link.

This module is the boundary. It exists before any navigation tool does, so the
tools are written against a decision that already refuses rather than having a
refusal retrofitted around them.

## Two checks, because one is not enough

**The scheme** is a string question and string matching answers it. Only
``http`` and ``https`` are permitted. ``file:`` would turn the browser into a
filesystem reader that bypasses Phase 3's roots entirely; ``data:`` and
``javascript:`` are script-execution primitives wearing a URL's clothes.

**The destination** is not a string question, and treating it as one is the
classic way to get this wrong. ``localhost`` is a name, ``127.0.0.1`` is one
spelling of a number, ``127.1`` and ``0x7f.1`` are others, and
``internal.example.com`` may resolve to ``10.0.0.5`` while looking entirely
public. So the host is resolved and every address it resolves to is checked
with :mod:`ipaddress` — private, loopback, link-local, reserved, multicast and
unspecified ranges are all refused, in v4 and v6. A name that resolves to one
public and one private address is refused: the browser picks, not JARVIS.

## Redirects

A permitted URL that redirects into a refused one is the same attack with an
extra hop, and it is the hop the scheme check cannot see. :meth:`UrlPolicy.
check_redirect` applies the identical rules to the destination, so the answer
does not depend on how many times the server bounced the request.

## What this is not

Not a general SSRF framework. There is no allow-list service, no per-request
proxy, no header filtering. The threat model is "the model, possibly influenced
by a page, proposes a URL", and the smallest boundary that answers it is the
one that is going to stay correct.

**Known limitation, stated rather than papered over:** resolution happens at
check time and the browser resolves again when it connects. A name that answers
differently between the two — DNS rebinding — is not defeated by this module,
and defeating it needs connection-level control Playwright does not expose.
Recorded in the Step 4 report as a residual gap rather than implied to be
covered.
"""

from __future__ import annotations

import enum
import ipaddress
import re
import socket
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from jarvis.logging import get_logger

log = get_logger(__name__)

#: The only schemes a browser JARVIS drives may load.
ALLOWED_SCHEMES = frozenset({"http", "https"})

#: Default ports, so an origin's port is always explicit. Two spellings of the
#: same destination must produce one resource string or a grant covers one of
#: them and silently not the other.
DEFAULT_PORTS = {"http": 80, "https": 443}

#: Names that mean "this machine" without needing to resolve to prove it.
#: Belt and braces: resolution catches these anyway, but refusing them by name
#: keeps the reason readable and works when resolution is unavailable.
LOCAL_NAMES = frozenset({"localhost", "localhost.localdomain", "ip6-localhost",
                         "ip6-loopback"})

#: What a host may be made of: DNS labels, or an IP literal. Deliberately
#: permissive about label content — internationalised names arrive here already
#: punycoded by :func:`urlsplit` — and deliberately strict about whitespace and
#: the characters that make a URL mean two things.
_HOST_RE = re.compile(r"^[0-9a-z_\-.]+$|^[0-9a-f:.]+$", re.IGNORECASE)


class UrlVerdict(str, enum.Enum):
    """Why a URL was refused. Four categories, kept apart deliberately.

    They have different causes and different fixes, and collapsing them into
    "bad URL" loses the only information the reader needs. A malformed string
    is the model's mistake; an unsupported scheme is a capability JARVIS does
    not have; a forbidden destination is a security decision; a redirect
    violation is a security decision that arrived one hop late and says
    something about the *site* rather than about the request.
    """

    ALLOWED = "ALLOWED"
    INVALID = "INVALID"
    UNSUPPORTED_SCHEME = "UNSUPPORTED_SCHEME"
    FORBIDDEN_DESTINATION = "FORBIDDEN_DESTINATION"
    REDIRECT_VIOLATION = "REDIRECT_VIOLATION"


@dataclass(slots=True)
class UrlDecision:
    """The answer, with everything a caller or an audit record needs."""

    verdict: UrlVerdict
    url: str
    reason: str
    #: ``scheme://host:port`` with the port always present. The permission
    #: resource is derived from this; see :mod:`jarvis.browser.policy`.
    origin: str | None = None
    host: str | None = None
    port: int | None = None
    scheme: str | None = None
    #: Addresses the host resolved to, for the audit trail. Empty when the
    #: refusal happened before resolution was needed.
    addresses: list[str] = field(default_factory=list)

    @property
    def allowed(self) -> bool:
        return self.verdict is UrlVerdict.ALLOWED

    def describe(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict.value,
            "allowed": self.allowed,
            "url": self.url,
            "reason": self.reason,
            "origin": self.origin,
            "addresses": list(self.addresses),
        }


def _authority(host: str) -> str:
    """Host as it appears in an origin, bracketing IPv6 literals.

    ``http://::1:80`` cannot be parsed back — the colons are ambiguous — and
    the origin string becomes a permission resource, so an ambiguous one means
    a grant that does not mean what it looks like.
    """
    try:
        ipaddress.IPv6Address(host)
    except ValueError:
        return host
    return f"[{host}]"


def _forbidden_address(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str | None:
    """Why this address is off-limits, or ``None`` if it is fine.

    Ordered most-specific-first so the reason names the actual property rather
    than the broadest one that happens to match — ``is_private`` is true of a
    loopback address too, and "private range" is a worse explanation of
    ``127.0.0.1`` than "loopback".
    """
    if ip.is_unspecified:
        return "the unspecified address"
    if ip.is_loopback:
        return "a loopback address"
    if ip.is_link_local:
        # 169.254.0.0/16 and fe80::/10. The cloud metadata endpoint lives here
        # and is the single most valuable target a browsing agent can be
        # pointed at.
        return "a link-local address"
    if ip.is_multicast:
        return "a multicast address"
    if ip.is_reserved:
        return "a reserved address"
    if ip.is_private:
        # Covers RFC1918 in v4 and fc00::/7 unique-local in v6.
        return "a private network address"
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            # ::ffff:127.0.0.1 is a loopback address written in v6. Judge it as
            # what it reaches, not as what it looks like.
            return _forbidden_address(ip.ipv4_mapped)
        if ip.sixtofour is not None:
            return _forbidden_address(ip.sixtofour)
    return None


@dataclass(slots=True)
class UrlPolicy:
    """The decision, as an object so it can be configured and tested.

    Both escape hatches default to off and are separate: an operator who wants
    JARVIS to reach a development server on ``localhost:3000`` should not have
    to open the whole private network to get it.
    """

    allow_localhost: bool = False
    allow_private_networks: bool = False
    #: Injectable so the resolution behaviour can be tested without DNS. Not a
    #: configuration knob — nothing outside tests passes it.
    resolver: Any = None

    def check(self, url: str) -> UrlDecision:
        """Evaluate a URL the model or a caller supplied."""
        return self._check(url, UrlVerdict.FORBIDDEN_DESTINATION)

    def check_redirect(self, url: str, *, from_url: str = "") -> UrlDecision:
        """Evaluate where a request ended up after the server redirected it.

        Same rules, different verdict on refusal. The distinction is worth
        keeping: a refused navigation is a request that should not have been
        made, while a refused redirect is a request that was reasonable and a
        site that sent it somewhere it may not go. Only the second one is
        evidence about the site.
        """
        decision = self._check(url, UrlVerdict.REDIRECT_VIOLATION)
        if not decision.allowed:
            decision.verdict = (
                UrlVerdict.REDIRECT_VIOLATION
                if decision.verdict is not UrlVerdict.INVALID
                else UrlVerdict.INVALID
            )
            if from_url:
                decision.reason = (
                    f"{from_url} redirected to {url}, which is refused: "
                    f"{decision.reason}"
                )
        return decision

    # ── internals ────────────────────────────────────────────────────────────

    def _check(self, url: str, destination_verdict: UrlVerdict) -> UrlDecision:
        raw = (url or "").strip()
        if not raw:
            return UrlDecision(UrlVerdict.INVALID, url, "The URL is empty.")

        try:
            parts = urlsplit(raw)
        except ValueError as exc:
            return UrlDecision(UrlVerdict.INVALID, url, f"Not a usable URL: {exc}")

        scheme = (parts.scheme or "").lower()
        if not scheme:
            return UrlDecision(
                UrlVerdict.INVALID,
                url,
                "The URL has no scheme. Give a full address starting with "
                "https:// — JARVIS does not guess one.",
            )
        if scheme not in ALLOWED_SCHEMES:
            return UrlDecision(
                UrlVerdict.UNSUPPORTED_SCHEME,
                url,
                f"JARVIS's browser only opens http and https URLs, not "
                f"{scheme}:. Local files, inline data and javascript: are not "
                "reachable through it.",
                scheme=scheme,
            )

        try:
            host = parts.hostname
        except ValueError as exc:  # malformed IPv6 literal, bad percent-encoding
            return UrlDecision(UrlVerdict.INVALID, url, f"Not a usable URL: {exc}")
        if not host:
            return UrlDecision(
                UrlVerdict.INVALID, url, "The URL has no host."
            )
        host = host.lower().rstrip(".")
        if not _HOST_RE.fullmatch(host):
            # Categorised as malformed rather than left to fail resolution.
            # Both refuse, but "that is not a hostname" is the model's mistake
            # to correct, and reporting it as a forbidden destination sends it
            # looking for a permission problem it does not have.
            return UrlDecision(
                UrlVerdict.INVALID,
                url,
                f"{host!r} is not a valid hostname or IP address.",
            )

        try:
            port = parts.port
        except ValueError:
            return UrlDecision(
                UrlVerdict.INVALID, url, "The URL's port is not a number."
            )
        port = port or DEFAULT_PORTS[scheme]
        origin = f"{scheme}://{_authority(host)}:{port}"

        def refuse(reason: str, addresses: list[str] | None = None) -> UrlDecision:
            return UrlDecision(
                destination_verdict, url, reason, origin=origin, host=host,
                port=port, scheme=scheme, addresses=addresses or [],
            )

        if host in LOCAL_NAMES and not self.allow_localhost:
            return refuse(
                f"{host} is this machine. JARVIS's browser does not open local "
                "services; enable it deliberately if that is what you want."
            )

        addresses, error = self._resolve(host)
        if error is not None:
            return refuse(error)

        # Every address, not the first: a name that answers with one public
        # and one private address is refused, because which one the browser
        # connects to is the browser's choice and not JARVIS's.
        for address in addresses:
            ip = ipaddress.ip_address(address)
            problem = _forbidden_address(ip)
            if problem is None:
                continue
            if ip.is_loopback and self.allow_localhost:
                continue
            if self.allow_private_networks and (ip.is_private or ip.is_link_local):
                continue
            return refuse(
                f"{host} resolves to {address}, which is {problem}. JARVIS's "
                "browser does not reach the local machine or private networks, "
                "so it cannot be used to probe them.",
                addresses,
            )

        return UrlDecision(
            UrlVerdict.ALLOWED, url, "Permitted destination.", origin=origin,
            host=host, port=port, scheme=scheme, addresses=addresses,
        )

    def _resolve(self, host: str) -> tuple[list[str], str | None]:
        """Every address the host maps to, or a refusal reason.

        A literal address is used as written — resolving it would be a round
        trip to learn what the string already says. A name is resolved, and a
        name that cannot be resolved is refused rather than allowed: an
        unknown destination is not a safe one, and the browser would only fail
        on it anyway.
        """
        try:
            return [str(ipaddress.ip_address(host))], None
        except ValueError:
            pass

        resolver = self.resolver or socket.getaddrinfo
        try:
            infos = resolver(host, None, proto=socket.IPPROTO_TCP)
        except (socket.gaierror, OSError, UnicodeError) as exc:
            return [], (
                f"{host} could not be resolved ({exc}). JARVIS does not open a "
                "destination it cannot identify."
            )

        addresses: list[str] = []
        for info in infos:
            candidate = info[4][0] if isinstance(info, tuple) else str(info)
            candidate = str(candidate).split("%", 1)[0]  # strip a zone id
            try:
                ipaddress.ip_address(candidate)
            except ValueError:
                continue
            if candidate not in addresses:
                addresses.append(candidate)

        if not addresses:
            return [], (
                f"{host} resolved to no usable address. JARVIS does not open a "
                "destination it cannot identify."
            )
        return addresses, None
