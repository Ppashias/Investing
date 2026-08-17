"""The browser control plane (Phase 4, Step 4B–4E).

URL policy, origin permissions, element references and credential refusal —
all built before any browser tool exists, so the tools in Step 5 are written
against boundaries that already refuse rather than having refusals retrofitted
around them.

Most of this is deliberately testable without a browser: the URL decision is a
pure function of a string and DNS, the permission decision is a pure function
of grants, and the credential rule is a pure function of DOM attributes. Where
a real browser adds evidence a stub cannot — that a page navigating really does
invalidate references, that a real ``<input type=password>`` is really refused —
it is used, and those tests say so.
"""

from __future__ import annotations

import socket

import pytest

from jarvis.browser.elements import (
    ElementRef,
    ElementRegistry,
    StaleElement,
    UnknownElement,
    WrongPage,
)
from jarvis.browser.policy import (
    BrowserOperation,
    BrowserPolicy,
    CredentialRefused,
    FieldInspection,
    credential_reason,
    inspection_from_dom,
    refuse_if_credential,
    resource_for,
)
from jarvis.browser.urls import UrlPolicy, UrlVerdict
from jarvis.core import JarvisCore
from jarvis.db.models import Capability, PermissionGrant, PermissionMode

from .test_browser_runtime import browser_module, code_of


def fake_dns(mapping: dict[str, list[str]]):
    """A resolver that answers from a table, for destinations we cannot own.

    Real DNS would make these tests depend on the internet and on somebody
    else's zone file. The mapping is the *only* thing stubbed; the address
    classification underneath is the real code.
    """

    def _resolve(host, _port, **_kwargs):
        try:
            answers = mapping[host]
        except KeyError as exc:
            raise socket.gaierror(f"no fixture entry for {host}") from exc
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, 0)) for a in answers]

    return _resolve


# ── 4B: schemes ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/page",
        "https://example.com:8443/page?q=1#frag",
        "http://example.com/",
    ],
)
def test_ordinary_web_urls_are_permitted(url: str) -> None:
    policy = UrlPolicy(resolver=fake_dns({"example.com": ["93.184.216.34"]}))
    decision = policy.check(url)
    assert decision.verdict is UrlVerdict.ALLOWED, decision.reason
    assert decision.allowed is True


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "file://C:/Windows/System32/config/SAM",
        "data:text/html,<script>alert(1)</script>",
        "javascript:alert(document.cookie)",
        "ftp://example.com/x",
        "chrome://settings",
        "view-source:https://example.com",
        "about:blank",
        "blob:https://example.com/uuid",
        "ws://example.com/socket",
    ],
)
def test_non_web_schemes_are_refused(url: str) -> None:
    """``file:`` matters most of the four.

    It would turn the browser into a filesystem reader that answers to nobody —
    Phase 3's ``read_file`` is bounded by configured roots, and a browser that
    can open ``file:///`` walks straight around them.
    """
    decision = UrlPolicy().check(url)
    assert decision.verdict is UrlVerdict.UNSUPPORTED_SCHEME
    assert "only opens http and https" in decision.reason


@pytest.mark.parametrize(
    "url", ["", "   ", "not a url", "https://", "http://", "://example.com",
            "example.com/path", "https://exa mple.com", "http://[::1"]
)
def test_malformed_urls_are_reported_as_malformed(url: str) -> None:
    """A different category from a refusal, because it has a different cause.

    "You gave me something that is not a URL" is the model's mistake to fix.
    "That destination is off-limits" is not, and telling the model to correct
    its syntax when the syntax was fine sends it round a loop.
    """
    decision = UrlPolicy().check(url)
    assert decision.verdict is UrlVerdict.INVALID
    assert decision.allowed is False


def test_a_url_with_a_nonsense_port_is_malformed() -> None:
    assert UrlPolicy().check("https://example.com:notaport/").verdict is (
        UrlVerdict.INVALID
    )


# ── 4B: destinations ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "url,expected_in_reason",
    [
        ("http://localhost:3000/", "this machine"),
        ("http://127.0.0.1/", "loopback"),
        ("http://127.1/", "loopback"),
        ("http://[::1]/", "loopback"),
        ("http://0.0.0.0/", "unspecified"),
        ("http://10.0.0.5/", "private"),
        ("http://172.16.4.4/", "private"),
        ("http://192.168.1.1/", "private"),
        ("http://169.254.169.254/latest/meta-data/", "link-local"),
        ("http://[fe80::1]/", "link-local"),
        ("http://[fc00::1]/", "private"),
        ("http://[::ffff:10.0.0.1]/", "private"),
    ],
)
def test_local_and_private_destinations_are_refused(url: str, expected_in_reason: str) -> None:
    """The SSRF boundary. ``169.254.169.254`` is the one to notice.

    It is the cloud metadata endpoint: link-local, unauthenticated, and the
    single highest-value thing a browsing agent can be pointed at. It is
    refused by the same rule as every other link-local address rather than by
    a special case, because a special case only covers the address someone
    thought of.
    """
    decision = UrlPolicy().check(url)
    assert decision.verdict is UrlVerdict.FORBIDDEN_DESTINATION, decision.reason
    assert expected_in_reason in decision.reason


@pytest.mark.parametrize("url", ["http://2130706433/", "http://0x7f.0.0.1/",
                                 "http://017700000001/"])
def test_obfuscated_spellings_of_loopback_are_refused(url: str) -> None:
    """Why resolution beats string matching, demonstrated.

    ``2130706433`` is ``127.0.0.1`` in decimal. No amount of comparing the host
    against "127." or "localhost" catches it; resolving it does, and the reason
    that comes back names loopback rather than "unparseable".
    """
    decision = UrlPolicy().check(url)
    assert decision.verdict is UrlVerdict.FORBIDDEN_DESTINATION
    assert "loopback" in decision.reason


def test_a_public_name_that_resolves_privately_is_refused() -> None:
    """The attack string matching cannot see at all.

    ``intranet.example.com`` looks like any other public hostname. Only
    resolution reveals it points at ``10.1.2.3``.
    """
    policy = UrlPolicy(resolver=fake_dns({"intranet.example.com": ["10.1.2.3"]}))
    decision = policy.check("https://intranet.example.com/admin")
    assert decision.verdict is UrlVerdict.FORBIDDEN_DESTINATION
    assert "10.1.2.3" in decision.reason


def test_a_name_resolving_to_both_public_and_private_is_refused() -> None:
    """Which address the browser connects to is the browser's choice.

    Allowing the URL because *one* answer was public would leave the actual
    destination up to resolution order.
    """
    policy = UrlPolicy(
        resolver=fake_dns({"split.example.com": ["93.184.216.34", "10.0.0.9"]})
    )
    decision = policy.check("https://split.example.com/")
    assert decision.verdict is UrlVerdict.FORBIDDEN_DESTINATION
    assert "10.0.0.9" in decision.reason


def test_an_unresolvable_host_is_refused_rather_than_allowed() -> None:
    """Fail closed. An unknown destination is not a safe one."""
    policy = UrlPolicy(resolver=fake_dns({}))
    decision = policy.check("https://nowhere.invalid/")
    assert decision.verdict is UrlVerdict.FORBIDDEN_DESTINATION
    assert "could not be resolved" in decision.reason


def test_localhost_can_be_opened_deliberately() -> None:
    """The escape hatch exists and is separate from the private-network one.

    An operator pointing JARVIS at a dev server on localhost should not have to
    open their whole LAN to do it.
    """
    permissive = UrlPolicy(allow_localhost=True)
    assert permissive.check("http://localhost:3000/").allowed is True
    assert permissive.check("http://127.0.0.1:3000/").allowed is True
    # …and it does not quietly widen to the rest of the private space.
    assert permissive.check("http://10.0.0.5/").allowed is False


def test_private_networks_can_be_opened_separately() -> None:
    permissive = UrlPolicy(allow_private_networks=True)
    assert permissive.check("http://10.0.0.5/").allowed is True
    assert permissive.check("http://localhost/").allowed is False, (
        "the localhost switch is a different switch"
    )


# ── 4B: redirects ────────────────────────────────────────────────────────────


def test_a_redirect_into_a_private_address_is_a_redirect_violation() -> None:
    """The same attack with one extra hop, and its own verdict.

    A refused navigation is a request that should not have been made. A refused
    redirect is a reasonable request and a site that sent it somewhere it may
    not go — only the second is evidence about the site, so they are not the
    same category.
    """
    policy = UrlPolicy(resolver=fake_dns({"public.example.com": ["93.184.216.34"]}))
    assert policy.check("https://public.example.com/").allowed is True

    decision = policy.check_redirect(
        "http://169.254.169.254/latest/meta-data/",
        from_url="https://public.example.com/",
    )
    assert decision.verdict is UrlVerdict.REDIRECT_VIOLATION
    assert "redirected to" in decision.reason
    assert "link-local" in decision.reason


def test_a_redirect_to_a_forbidden_scheme_is_refused() -> None:
    decision = UrlPolicy().check_redirect(
        "file:///etc/passwd", from_url="https://example.com/"
    )
    assert decision.verdict is UrlVerdict.REDIRECT_VIOLATION
    assert "file:" in decision.reason


def test_a_redirect_to_another_public_page_is_fine() -> None:
    policy = UrlPolicy(resolver=fake_dns({"elsewhere.example.com": ["93.184.216.34"]}))
    assert policy.check_redirect("https://elsewhere.example.com/x").allowed is True


def test_a_malformed_redirect_target_stays_malformed() -> None:
    """Category preserved: a broken Location header is not a policy violation."""
    assert UrlPolicy().check_redirect("://nonsense").verdict is UrlVerdict.INVALID


# ── 4B: origins ──────────────────────────────────────────────────────────────


def test_the_origin_always_carries_scheme_and_an_explicit_port() -> None:
    """Two spellings of one destination must produce one string.

    Without an explicit port, ``https://github.com`` and
    ``https://github.com:443`` are two resources and a grant covers one of them.
    """
    policy = UrlPolicy(resolver=fake_dns({"github.com": ["140.82.121.4"]}))
    assert policy.check("https://github.com/a/b").origin == "https://github.com:443"
    assert policy.check("https://github.com:443/x").origin == "https://github.com:443"
    assert policy.check("http://github.com/").origin == "http://github.com:80"
    assert policy.check("https://github.com:8443/").origin == "https://github.com:8443"


def test_an_ipv6_origin_is_bracketed() -> None:
    """``http://::1:80`` cannot be parsed back, and origins become resources."""
    policy = UrlPolicy(allow_localhost=True)
    assert policy.check("http://[::1]:8080/").origin == "http://[::1]:8080"


def test_the_origin_is_case_and_trailing_dot_normalised() -> None:
    policy = UrlPolicy(resolver=fake_dns({"github.com": ["140.82.121.4"]}))
    assert policy.check("https://GitHub.COM./x").origin == "https://github.com:443"


# ── 4C: origin permissions ───────────────────────────────────────────────────


async def _grant(core, *, capability, scope, mode=PermissionMode.ALLOW):
    async with core.database.session_factory() as session:
        user = await JarvisCore.ensure_default_user(session)
        session.add(
            PermissionGrant(
                user_id=user.id, capability=capability,
                resource_scope=scope, mode=mode, note="test",
            )
        )
        await session.commit()
        return user.id


async def _authorize(core, origin, operation, *, tainted=False):
    async with core.database.session_factory() as session:
        user = await JarvisCore.ensure_default_user(session)
        return await BrowserPolicy(session).authorize(
            operation, origin=origin, user_id=user.id, tainted=tainted
        )


def test_the_resource_form_is_defined_in_exactly_one_place() -> None:
    assert resource_for("https://github.com:443") == "browser:https://github.com:443"


async def test_a_granted_origin_matches_its_grant(core) -> None:
    """The grant is found — and the irreversibility floor still applies.

    Worth spelling out, because it surprised this test when it was written:
    an INTERACT is declared irreversible (JARVIS cannot un-click a button), so
    Phase 1's floor turns even an explicit ALLOW into ASK. That is the engine
    behaving as designed and is not weakened here; what the grant changes is
    which rules fire, so that is what is asserted.
    """
    await _grant(
        core,
        capability=Capability.EXTERNAL_ACTION,
        scope="browser:https://github.com:443",
    )
    auth = await _authorize(core, "https://github.com:443", BrowserOperation.INTERACT)

    assert auth.origin == "https://github.com:443"
    assert auth.decision.matched_grant_id is not None
    assert any("matched_grant" in rule for rule in auth.decision.applied_rules)
    assert auth.mode is PermissionMode.ASK
    assert "irreversible_floor" in auth.decision.applied_rules


async def test_a_grant_for_one_origin_does_not_cover_another(core) -> None:
    """The property the whole scheme exists for."""
    await _grant(
        core,
        capability=Capability.EXTERNAL_ACTION,
        scope="browser:https://github.com:443",
    )
    auth = await _authorize(core, "https://evil.com:443", BrowserOperation.INTERACT)
    # Asserted on the grant rather than the mode: both origins end at ASK
    # because of the irreversibility floor, so a mode comparison would pass
    # even if the grant *had* leaked across.
    assert auth.decision.matched_grant_id is None


async def test_a_grant_for_https_does_not_cover_http(core) -> None:
    """Why the scheme is in the resource.

    A grant to interact with a site over TLS is not a grant to do it in
    plaintext, and an origin form that omitted the scheme would silently make
    it one.
    """
    await _grant(
        core,
        capability=Capability.EXTERNAL_ACTION,
        scope="browser:https://github.com:443",
    )
    auth = await _authorize(core, "http://github.com:80", BrowserOperation.INTERACT)
    assert auth.decision.matched_grant_id is None


async def test_a_grant_for_one_port_does_not_cover_another(core) -> None:
    await _grant(
        core,
        capability=Capability.EXTERNAL_ACTION,
        scope="browser:https://internal.example.com:443",
    )
    auth = await _authorize(
        core, "https://internal.example.com:8443", BrowserOperation.INTERACT
    )
    assert auth.decision.matched_grant_id is None


async def test_a_lookalike_subdomain_does_not_inherit_an_exact_grant(core) -> None:
    """``github.com.evil.com`` must not match a grant for ``github.com``.

    Exact scopes are safe against this. A trailing-wildcard scope is not —
    :func:`fnmatch` lets ``*`` cross dots — which is a property of the existing
    engine rather than of this module, and is recorded in the Step 4 report as
    a documented sharp edge rather than silently assumed away.
    """
    await _grant(
        core,
        capability=Capability.EXTERNAL_ACTION,
        scope="browser:https://github.com:443",
    )
    auth = await _authorize(
        core, "https://github.com.evil.com:443", BrowserOperation.INTERACT
    )
    assert auth.decision.matched_grant_id is None


async def test_an_explicit_deny_stays_denied_however_broad_the_allow(core) -> None:
    await _grant(core, capability=Capability.EXTERNAL_ACTION, scope="browser:*")
    await _grant(
        core,
        capability=Capability.EXTERNAL_ACTION,
        scope="browser:https://evil.com:443",
        mode=PermissionMode.DENY,
    )
    assert (
        await _authorize(core, "https://evil.com:443", BrowserOperation.INTERACT)
    ).mode is PermissionMode.DENY
    # DENY is absolute; ALLOW still meets the irreversibility floor, so the
    # broad grant produces ASK rather than ALLOW. Both are the engine's
    # existing semantics, unchanged by this module.
    assert (
        await _authorize(core, "https://good.com:443", BrowserOperation.INTERACT)
    ).mode is PermissionMode.ASK


async def test_an_ungranted_origin_asks_rather_than_allowing(core) -> None:
    auth = await _authorize(core, "https://unknown.com:443", BrowserOperation.INTERACT)
    assert auth.mode is PermissionMode.ASK


async def test_reading_a_public_page_is_allowed_by_the_default_read_grant(core) -> None:
    """Reading is a READ capability, which the seeded grants permit.

    Which is the point of separating READ from INTERACT: fetching a page is not
    the same act as clicking a button on it, and treating them the same means
    either browsing needs approval for every page or clicking does not need it
    at all.
    """
    auth = await _authorize(core, "https://example.com:443", BrowserOperation.READ)
    assert auth.mode is PermissionMode.ALLOW


async def test_an_interaction_always_meets_a_human(core) -> None:
    """The actual Phase 4 guarantee, and it is stronger than taint escalation.

    An INTERACT is declared irreversible, so Phase 1's floor turns it to ASK
    before the taint rule is even reached — with no grant, with an origin
    grant, and with the broadest grant expressible. JARVIS cannot un-click a
    button, and whether the far side can undo it is the far side's business.

    A consequence worth stating rather than discovering: because the floor
    fires first, ``taint_escalation`` does not appear in the applied rules for
    an interaction. The outcome is identical, but an audit reader looking for
    evidence that taint mattered will not find it here — it is masked, not
    absent. ``test_taint_escalation_reaches_browser_resources`` pins the
    mechanism separately so relaxing the floor later cannot silently remove it.
    """
    ungranted = await _authorize(core, "https://a.com:443", BrowserOperation.INTERACT)
    assert ungranted.mode is PermissionMode.ASK

    await _grant(core, capability=Capability.EXTERNAL_ACTION, scope="browser:*")
    granted = await _authorize(core, "https://a.com:443", BrowserOperation.INTERACT)
    tainted = await _authorize(
        core, "https://a.com:443", BrowserOperation.INTERACT, tainted=True
    )

    assert granted.mode is PermissionMode.ASK
    assert "irreversible_floor" in granted.decision.applied_rules
    assert tainted.mode is PermissionMode.ASK


async def test_taint_escalation_reaches_browser_resources(core) -> None:
    """The browser resource family is not exempt from taint escalation.

    Exercised with ``reversible=True`` so the irreversibility floor does not
    mask the rule — the point is that a ``browser:`` resource participates in
    escalation exactly like ``tool:`` and ``obsidian:`` do. If a future browser
    operation is genuinely reversible, this is the property it will rely on,
    and this test fails first if it stops holding.
    """
    from jarvis.browser.policy import resource_for
    from jarvis.db.models import RiskLevel
    from jarvis.permissions.engine import PermissionEngine, PermissionRequest

    await _grant(core, capability=Capability.EXTERNAL_ACTION, scope="browser:*")

    async with core.database.session_factory() as session:
        user = await JarvisCore.ensure_default_user(session)
        engine = PermissionEngine(session)

        def _request(tainted: bool) -> PermissionRequest:
            return PermissionRequest(
                user_id=user.id,
                capability=Capability.EXTERNAL_ACTION,
                resource=resource_for("https://github.com:443"),
                risk_level=RiskLevel.LOW,
                reversible=True,
                tainted=tainted,
            )

        clean = await engine.evaluate(_request(False))
        tainted = await engine.evaluate(_request(True))

    assert clean.mode is PermissionMode.ALLOW
    assert "taint_escalation" not in clean.applied_rules
    assert tainted.mode is PermissionMode.ASK
    assert "taint_escalation" in tainted.applied_rules
    assert tainted.reason == "untrusted_context"


async def test_tainted_content_does_not_escalate_a_read(core) -> None:
    """Reads are exempt by the engine's existing rule, and should stay so.

    Escalating reads would mean a confirmation to look at the next page of
    something JARVIS is already reading, which trains the user to approve
    without looking — the failure mode that makes every later confirmation
    worthless.
    """
    auth = await _authorize(
        core, "https://example.com:443", BrowserOperation.READ, tainted=True
    )
    assert auth.mode is PermissionMode.ALLOW


async def test_an_interaction_is_never_auto_allowed_without_a_grant(core) -> None:
    """The irreversibility floor applies: JARVIS cannot un-click a button."""
    from jarvis.browser.policy import OPERATIONS

    _, _, reversible = OPERATIONS[BrowserOperation.INTERACT]
    assert reversible is False

    await _grant(core, capability=Capability.EXTERNAL_ACTION, scope="browser:*")
    auth = await _authorize(core, "https://x.com:443", BrowserOperation.INTERACT)
    # A broad grant can allow it; what it cannot do is bypass the engine.
    assert auth.decision.applied_rules


def test_the_browser_policy_creates_no_second_permission_system() -> None:
    """One engine. Asserted against the source, since a second one would start
    as a small convenience helper and never look like a decision."""
    source = code_of(browser_module("policy"))
    assert "PermissionEngine(" in source
    for forbidden in ("class PermissionEngine", "DEFAULT_MODES", "PermissionGrant",
                      "fnmatch", "_specificity"):
        assert forbidden not in source, forbidden


# ── 4D: element references ───────────────────────────────────────────────────


def test_a_reference_resolves_on_its_own_page() -> None:
    registry = ElementRegistry()
    ref = registry.register(page_id="pg_1", locator="LOC", description="Save button")
    entry = registry.resolve(ref, page_id="pg_1")
    assert entry.locator == "LOC"
    assert entry.description == "Save button"


def test_a_reference_is_refused_on_another_page() -> None:
    """The same reference resolving on two pages would mean acting on a page
    nobody looked at."""
    registry = ElementRegistry()
    ref = registry.register(page_id="pg_1", locator="LOC", description="Save")
    registry.register(page_id="pg_2", locator="OTHER", description="Delete")

    with pytest.raises(WrongPage) as caught:
        registry.resolve(ref, page_id="pg_2")
    assert "belongs to page pg_1" in str(caught.value)


def test_a_fabricated_reference_does_not_resolve() -> None:
    """Validation is by lookup, never by parsing.

    A model can compose a plausible id; it cannot make the registry have issued
    one. This is why the token's shape carries no authority.
    """
    registry = ElementRegistry()
    registry.register(page_id="pg_1", locator="LOC", description="Save")

    invented = ElementRef(element_id="el_whatever", page_id="pg_1", generation=0)
    with pytest.raises(UnknownElement):
        registry.resolve(invented, page_id="pg_1")


def test_a_fabricated_reference_cannot_be_aimed_at_another_page() -> None:
    """Rewriting the page id in a reference must not make it work there."""
    registry = ElementRegistry()
    real = registry.register(page_id="pg_1", locator="LOC", description="Save")
    registry.register(page_id="pg_2", locator="OTHER", description="Delete")

    forged = ElementRef(
        element_id=real.element_id, page_id="pg_2", generation=real.generation
    )
    with pytest.raises(UnknownElement):
        registry.resolve(forged, page_id="pg_2")


def test_navigation_makes_existing_references_stale() -> None:
    """The case a selector cannot express.

    ``#submit`` still resolves after a navigation — to a different button on a
    different page. A generation stamp is what turns that from a silent wrong
    action into a refusal.
    """
    registry = ElementRegistry()
    ref = registry.register(page_id="pg_1", locator="LOC", description="Save")

    registry.page_navigated("pg_1")

    with pytest.raises(StaleElement) as caught:
        registry.resolve(ref, page_id="pg_1")
    assert "navigated since" in str(caught.value)


def test_navigation_drops_the_entries_it_invalidates() -> None:
    """Not just marked stale — released, because entries hold locators."""
    registry = ElementRegistry()
    registry.register(page_id="pg_1", locator="LOC", description="Save")
    assert registry.count("pg_1") == 1

    registry.page_navigated("pg_1")
    assert registry.count("pg_1") == 0


def test_references_issued_after_navigation_work() -> None:
    registry = ElementRegistry()
    old = registry.register(page_id="pg_1", locator="OLD", description="Save")
    registry.page_navigated("pg_1")
    new = registry.register(page_id="pg_1", locator="NEW", description="Submit")

    assert new.generation == old.generation + 1
    assert registry.resolve(new, page_id="pg_1").locator == "NEW"
    with pytest.raises(StaleElement):
        registry.resolve(old, page_id="pg_1")


def test_forgetting_a_page_invalidates_its_references() -> None:
    registry = ElementRegistry()
    ref = registry.register(page_id="pg_1", locator="LOC", description="Save")
    registry.forget_page("pg_1")

    with pytest.raises(UnknownElement):
        registry.resolve(ref, page_id="pg_1")


def test_clearing_the_registry_invalidates_everything() -> None:
    registry = ElementRegistry()
    a = registry.register(page_id="pg_1", locator="A", description="a")
    b = registry.register(page_id="pg_2", locator="B", description="b")
    registry.clear()

    for ref, page in ((a, "pg_1"), (b, "pg_2")):
        with pytest.raises(UnknownElement):
            registry.resolve(ref, page_id=page)


def test_the_registry_is_bounded_per_page() -> None:
    """Entries hold locators, locators hold pages. Unbounded growth is a leak."""
    registry = ElementRegistry(max_elements=3)
    refs = [
        registry.register(page_id="pg_1", locator=f"L{i}", description=f"e{i}")
        for i in range(10)
    ]

    assert registry.count("pg_1") == 3
    # The most recent survive; the oldest are evicted.
    assert registry.resolve(refs[-1], page_id="pg_1").locator == "L9"
    with pytest.raises(UnknownElement):
        registry.resolve(refs[0], page_id="pg_1")


def test_the_registry_describes_itself_without_leaking_locators() -> None:
    registry = ElementRegistry()
    registry.register(page_id="pg_1", locator="SECRET-LOCATOR", description="Save")
    payload = registry.describe()

    assert payload["pages"]["pg_1"] == {"generation": 0, "elements": 1}
    assert "SECRET-LOCATOR" not in str(payload)


def test_element_references_carry_no_coordinates() -> None:
    """The decision document's constraint, asserted against the source.

    A coordinate anywhere in the reference model would be the beginning of a
    fallback path — "use the locator, or the last known position if it fails" —
    and that fallback is the fake-success failure the whole design avoids.
    """
    source = code_of(browser_module("elements")).lower()
    for forbidden in ("coordinate", "mouse", "click_at", "bounding_box",
                      "screen_x", "screen_y", "offset_x", "offset_y", "pixel"):
        assert forbidden not in source, forbidden

    # Nothing on the reference itself positions anything.
    from jarvis.browser.elements import ElementEntry, ElementRef

    fields = set(ElementRef.__dataclass_fields__) | set(
        ElementEntry.__dataclass_fields__
    )
    assert not fields & {"x", "y", "width", "height", "box", "position"}


def test_nothing_in_the_subsystem_navigates_yet() -> None:
    """A guard rail for Step 5, placed before Step 5 exists.

    The URL policy only protects what goes through it. A tool that reaches for
    ``page.goto`` directly would be correct-looking, would work, and would skip
    the entire SSRF boundary — so the absence of any navigation call is pinned
    now, and adding one has to come with a deliberate decision about where the
    policy check goes.

    Step 12 made exactly that decision for ``route(``, and the exception is
    narrow rather than a deletion: ``service`` may register request routing,
    and ``test_the_only_routing_in_the_subsystem_is_the_navigation_guard``
    below pins what it is allowed to register. Everything else — and every
    other module — is unchanged.
    """
    for name in ("settings", "service", "capabilities", "policy", "elements", "urls"):
        source = code_of(browser_module(name))
        forbidden = [".goto(", "page.goto", "set_content(", "request.get(",
                     "expose_function("]
        if name != "service":
            forbidden.append("route(")
        for token in forbidden:
            assert token not in source, f"{token} in {name}"


def test_the_only_routing_in_the_subsystem_is_the_navigation_guard() -> None:
    """What ``service`` is allowed to do with the exception it was granted.

    A route handler decides whether every request in the context is dispatched,
    so a second one — or the same one pointed at a different function — is the
    whole SSRF boundary re-decided somewhere nobody is looking. Pinned by
    source, because "there is only one" is not something a runtime test can
    observe.
    """
    import re

    source = code_of(browser_module("service"))
    registrations = re.findall(r"\.route\(([^)]*)\)", source)
    assert registrations == ["'**/*', self._guard_navigation"], registrations
    # And the ways a routed request can end are countable. Two aborts (the
    # single refusal path, and the fail-closed one), one pass-through for
    # sub-resources, one fetch-and-fulfill for documents.
    assert source.count("route.abort(") == 2
    assert source.count("route.continue_(") == 1
    assert source.count("route.fetch(") == 1
    assert source.count("route.fulfill(") == 1


# ── 4E: credentials ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "field_info,expected",
    [
        (FieldInspection(type="password"), "password field"),
        (FieldInspection(type="PASSWORD"), "password field"),
        (FieldInspection(type="text", autocomplete="current-password"), "autocomplete"),
        (FieldInspection(type="text", autocomplete="new-password"), "autocomplete"),
        (FieldInspection(type="text", autocomplete="one-time-code"), "autocomplete"),
        (FieldInspection(type="text", autocomplete="cc-csc"), "autocomplete"),
        (FieldInspection(type="text", name="user_password"), "credential field"),
        (FieldInspection(type="text", element_id="loginPasswd"), "credential field"),
        (FieldInspection(type="text", aria_label="One-time code"), "credential field"),
        (FieldInspection(type="text", placeholder="Enter your PIN"), "credential field"),
        (FieldInspection(type="text", label="API key"), "credential field"),
        (FieldInspection(type="text", name="cvv"), "credential field"),
        (FieldInspection(type="text", name="seed-phrase"), "credential field"),
        (FieldInspection(type="text", name="mfa_token"), "credential field"),
    ],
)
def test_credential_fields_are_identified(field_info, expected: str) -> None:
    reason = credential_reason(field_info)
    assert reason is not None, "this field should have been refused"
    assert expected in reason


@pytest.mark.parametrize(
    "field_info",
    [
        FieldInspection(type="text", name="q", aria_label="Search"),
        FieldInspection(type="email", name="email", label="Email address"),
        FieldInspection(type="text", name="street", label="Street address"),
        FieldInspection(type="search", placeholder="Search the docs"),
        FieldInspection(type="textarea", name="comment", label="Your comment"),
    ],
)
def test_ordinary_fields_are_not_refused(field_info) -> None:
    """The rule must not refuse everything, or it stops being a rule."""
    assert credential_reason(field_info) is None


def test_refusal_raises_with_something_the_user_can_act_on() -> None:
    with pytest.raises(CredentialRefused) as caught:
        refuse_if_credential(FieldInspection(type="password"), target="the login form")

    assert "the login form" in str(caught.value)
    assert "Enter it yourself" in caught.value.user_message
    assert caught.value.code == "browser_credential_field_refused"


def test_an_uninspectable_field_is_treated_as_a_credential_field() -> None:
    """Fail closed. The only reason the DOM payload is missing is that the
    element could not be read, and an element JARVIS cannot see is not one it
    should type a secret into."""
    assert credential_reason(inspection_from_dom(None)) is not None
    assert credential_reason(inspection_from_dom({})) is not None


def test_the_dom_payload_maps_onto_the_rule() -> None:
    field_info = inspection_from_dom(
        {"tag": "input", "type": "password", "name": "pw", "element_id": "pw",
         "autocomplete": "current-password", "aria_label": "", "placeholder": "",
         "label": "Password"}
    )
    assert field_info.type == "password"
    assert credential_reason(field_info) is not None


def test_no_credential_storage_or_retrieval_exists_anywhere_in_the_subsystem() -> None:
    """Refusal is the whole feature. There is nothing to type *from*.

    A store would make the refusal a policy that could be relaxed; its absence
    makes it a property of the build.
    """
    for name in ("settings", "service", "capabilities", "policy", "elements", "urls"):
        source = code_of(browser_module(name)).lower()
        for forbidden in ("keyring", "get_password", "saved_password",
                          "credential_store", "login(", "authenticate("):
            assert forbidden not in source, f"{forbidden} in {name}"


# ── real browser: the parts a stub cannot demonstrate ────────────────────────


@pytest.fixture
async def chromium():
    from .test_browser_runtime import resolve_chromium

    settings, reason = await resolve_chromium()
    if settings is None:
        pytest.skip(f"No usable Chromium on this machine: {reason}")
    return settings


@pytest.fixture
async def service(chromium):
    from jarvis.browser import BrowserService

    svc = BrowserService(chromium)
    try:
        yield svc
    finally:
        await svc.shutdown()


FORM = """
<html><body>
  <form>
    <label for="u">Username</label><input id="u" name="username" type="text">
    <label for="p">Password</label><input id="p" name="password" type="password">
    <input id="q" name="q" type="search" placeholder="Search">
    <input id="otp" name="otpValue" type="text" autocomplete="one-time-code">
    <input id="addr" name="shipping_address" type="text">
    <button id="go">Sign in</button>
  </form>
</body></html>
"""


async def test_a_real_navigation_invalidates_real_references(service) -> None:
    """REAL BROWSER. The generation bump is wired to Playwright, not just to a
    method somebody remembers to call.

    Subscribed to ``framenavigated`` at page creation, so a navigation JARVIS
    did not initiate — a redirect, a meta refresh, a script — invalidates
    references too. Those are exactly the navigations an attacker controls.
    """
    handle = await service.new_page()
    await handle.page.goto("data:text/html,<h1>first</h1>")

    ref = service.elements.register(
        page_id=handle.page_id, locator="LOC", description="heading"
    )
    assert service.elements.resolve(ref, page_id=handle.page_id).locator == "LOC"

    await handle.page.goto("data:text/html,<h1>second</h1>")

    with pytest.raises(StaleElement):
        service.elements.resolve(ref, page_id=handle.page_id)


async def test_closing_a_real_page_forgets_its_references(service) -> None:
    """REAL BROWSER."""
    handle = await service.new_page()
    await handle.page.goto("data:text/html,<h1>x</h1>")
    ref = service.elements.register(
        page_id=handle.page_id, locator="LOC", description="heading"
    )

    await service.close_page(handle.page_id)

    with pytest.raises(UnknownElement):
        service.elements.resolve(ref, page_id=handle.page_id)


async def test_a_browser_restart_invalidates_every_reference(service) -> None:
    """REAL BROWSER. The references must not outlive the browser they point into."""
    handle = await service.new_page()
    await handle.page.goto("data:text/html,<h1>x</h1>")
    ref = service.elements.register(
        page_id=handle.page_id, locator="LOC", description="heading"
    )

    await service._browser.close()          # the browser goes away
    service.describe()                      # …which the service notices
    assert (await service.launch()).ok is True

    with pytest.raises(UnknownElement):
        service.elements.resolve(ref, page_id=handle.page_id)
    assert service.elements.describe()["pages"] == {}


async def test_references_do_not_survive_shutdown(service) -> None:
    """REAL BROWSER. Entries hold locators, locators hold pages."""
    handle = await service.new_page()
    await handle.page.goto("data:text/html,<h1>x</h1>")
    ref = service.elements.register(
        page_id=handle.page_id, locator="LOC", description="heading"
    )

    await service.shutdown()

    assert service.elements.describe()["pages"] == {}
    with pytest.raises(UnknownElement):
        service.elements.resolve(ref, page_id=handle.page_id)


async def test_a_real_password_field_is_refused(service) -> None:
    """REAL BROWSER, real DOM. The credential boundary end to end.

    The inspection JS runs against a real ``<input type="password">`` in a real
    page, and the refusal comes from reading what the DOM says — not from a
    prompt asking the model to behave.
    """
    from jarvis.browser.policy import FIELD_INSPECTION_JS

    handle = await service.new_page()
    await handle.page.set_content(FORM)

    payload = await handle.page.locator("#p").evaluate(FIELD_INSPECTION_JS)
    field_info = inspection_from_dom(payload)

    assert field_info.type == "password"
    assert field_info.label == "Password"
    with pytest.raises(CredentialRefused):
        refuse_if_credential(field_info, target="the sign-in form")


@pytest.mark.parametrize(
    "selector,refused",
    [("#p", True), ("#otp", True), ("#u", False), ("#q", False), ("#addr", False)],
)
async def test_the_credential_rule_agrees_with_a_real_dom(
    service, selector: str, refused: bool
) -> None:
    """REAL BROWSER. Both directions, against real elements.

    ``#addr`` is the one that matters as much as ``#p``: "shipping_address"
    contains "pin", and a rule that refuses it is a rule the user disables —
    taking the password check with it.
    """
    from jarvis.browser.policy import FIELD_INSPECTION_JS

    handle = await service.new_page()
    await handle.page.set_content(FORM)

    payload = await handle.page.locator(selector).evaluate(FIELD_INSPECTION_JS)
    reason = credential_reason(inspection_from_dom(payload))

    assert (reason is not None) is refused, f"{selector} → {reason}"


def test_an_authorisation_exposes_all_three_outcomes() -> None:
    """So a Step 5 tool cannot accidentally reduce the decision to a boolean.

    ``allowed`` is False for every interaction — the floor sees to that — and a
    tool branching on it alone would either never act or, once someone
    "fixed" it, act without consulting the engine at all.
    """
    from jarvis.browser.policy import BrowserAuthorisation
    from jarvis.permissions.engine import PermissionDecision

    for mode, expected in (
        (PermissionMode.ALLOW, ("allowed",)),
        (PermissionMode.ASK, ("needs_confirmation",)),
        (PermissionMode.DENY, ("denied",)),
    ):
        auth = BrowserAuthorisation(
            decision=PermissionDecision(
                mode=mode, reason="x",
                capability=Capability.EXTERNAL_ACTION, resource="browser:x",
            ),
            origin="https://x:443",
            operation=BrowserOperation.INTERACT,
        )
        flags = {
            "allowed": auth.allowed,
            "needs_confirmation": auth.needs_confirmation,
            "denied": auth.denied,
        }
        assert [k for k, v in flags.items() if v] == list(expected)


def test_replacing_the_settings_discards_the_capability_answer() -> None:
    """A capability report is a conclusion *about* a configuration.

    Keeping it across a settings change answers a question nobody asked with
    the answer to a different one. Latent until the core began probing at
    startup: before that the first probe happened lazily, after any
    reconfiguration. It surfaced as the entire browser suite failing with "the
    browser is not available" — a refusal cached against default settings that
    the tests' own configuration could no longer clear.
    """
    from dataclasses import replace

    from jarvis.browser import BrowserService, BrowserSettings
    from jarvis.browser.capabilities import BrowserAvailability

    service = BrowserService(BrowserSettings(enabled=False))
    assert service.capabilities.state is BrowserAvailability.DISABLED

    service.settings = replace(service.settings, enabled=True)

    assert service.capabilities.state is BrowserAvailability.UNPROBED, \
        "a stale refusal survived the reconfiguration"
    # UNPROBED carries its own wording — "nobody has looked yet" is a state
    # worth naming, and it must not be the previous configuration's refusal.
    assert "switched off" not in service.capabilities.reason


def test_disabling_by_settings_is_known_without_probing() -> None:
    """The other direction, which must keep working: "switched off" is knowable
    from configuration alone, and the status endpoint should say so rather than
    "not probed yet"."""
    from dataclasses import replace

    from jarvis.browser import BrowserService, BrowserSettings
    from jarvis.browser.capabilities import BrowserAvailability

    service = BrowserService(BrowserSettings(enabled=True))
    service.settings = replace(service.settings, enabled=False)

    assert service.capabilities.state is BrowserAvailability.DISABLED
    assert "switched off" in service.capabilities.reason
