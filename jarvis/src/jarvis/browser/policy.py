"""Who may do what, where (Phase 4, Steps 4C and 4E).

Two boundaries live here, both of which have to exist before a single browser
tool does, because a tool written against a missing boundary tends to grow its
own.

## Origins are the resource, and the same engine decides

The permission model is (capability, resource_scope, mode), and this adds one
resource family: ``browser:<origin>``. Nothing else is new. The decision is
made by :class:`~jarvis.permissions.engine.PermissionEngine` — the same engine
Phase 1 built, the same grants table, the same specificity rules, the same
taint escalation, the same irreversibility floor. This module derives the
resource and calls it, exactly as
:meth:`ObsidianService.authorize` and :meth:`ComputerPolicyEngine._core_decision`
already do.

The reason a second layer exists at all is mechanical rather than
philosophical: :attr:`Tool.resource` is ``f"tool:{name}"``, a fixed property of
the tool. A browser action's resource depends on its *arguments* — which site —
and the executor decides before it can know that. So the executor authorises
"may this tool run", and this authorises "against this origin", and both go
through one engine.

## Why the origin carries scheme and port

``browser:github.com`` looks tidier and is wrong twice over. It makes
``http://github.com`` and ``https://github.com`` the same resource, so a grant
for the secure one silently covers the plaintext one. And it leaves the port
implicit, so ``github.com`` and ``github.com:8443`` — which need not be the same
service at all — collapse together.

The canonical form is therefore ``browser:https://github.com:443``, with the
port always written out even when it is the default. Always-explicit is what
makes two spellings of one destination produce one string; a form that omits
default ports produces two, and a grant covers one of them.

Grants are matched with :func:`fnmatch`, so ``*`` matches anything including
dots and slashes. That is the existing engine's behaviour and this module does
not change it — but it means ``browser:https://github.com*`` also matches
``browser:https://github.com.evil.com:443``. The canonical form cannot prevent
a careless glob; what it can do is make the *exact* form safe, which is why the
seeded and UI-generated scopes should be exact. There is a test for the
suffix-confusion case so the property is recorded rather than assumed.

## Credentials

JARVIS does not type passwords. Not "is instructed not to" — cannot, because
the check happens against the live DOM before any text is entered, and a field
that looks like a credential field is refused whatever the model intended.

Prompt instructions are the wrong layer for this. A page is untrusted content
and can argue with an instruction; it cannot argue with a function that reads
``input[type=password]`` and returns a refusal. The identification is
deliberately generous — type, autocomplete, name, id, and the accessible label
— because a false refusal costs the user one manual entry, and a false accept
costs them a credential.
"""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass, field
from typing import Any

from jarvis.browser.capabilities import BrowserError
from jarvis.db.models import Capability, PermissionMode, RiskLevel
from jarvis.logging import get_logger
from jarvis.permissions.engine import PermissionDecision, PermissionEngine, PermissionRequest

log = get_logger(__name__)


class BrowserOperation(str, enum.Enum):
    """What a browser action does, in permission terms.

    Reading a page and clicking a button are different capabilities even though
    both are "using the browser". A click submits forms, spends money and sends
    email; a read does not, and the engine's taint escalation only fires on
    non-read capabilities, so the distinction is what makes a poisoned page
    unable to authorise its own follow-up.
    """

    #: Open a page, navigate, inspect, extract. Content comes back; nothing
    #: changes on the far side.
    READ = "read"
    #: Click, fill, select, submit. Changes state somewhere JARVIS does not own.
    INTERACT = "interact"


#: (capability, risk, reversible) per operation.
#:
#: INTERACT is not reversible. JARVIS cannot un-click a button: the request
#: reached the server, and whether it can be undone is the far side's business,
#: not something this process can promise. Declaring it irreversible engages the
#: engine's floor, which turns ALLOW into ASK — that is the intended effect, and
#: it is why the flag is set here rather than argued about per tool.
OPERATIONS: dict[BrowserOperation, tuple[Capability, RiskLevel, bool]] = {
    BrowserOperation.READ: (Capability.READ, RiskLevel.LOW, True),
    BrowserOperation.INTERACT: (Capability.EXTERNAL_ACTION, RiskLevel.MEDIUM, False),
}


def resource_for(origin: str) -> str:
    """The permission resource for an origin. One place, so it cannot drift."""
    return f"browser:{origin}"


@dataclass(slots=True)
class BrowserAuthorisation:
    """A decision plus the origin it was made about."""

    decision: PermissionDecision
    origin: str
    operation: BrowserOperation

    @property
    def mode(self) -> PermissionMode:
        return self.decision.mode

    @property
    def allowed(self) -> bool:
        """Proceed without asking. **False for every INTERACT**, by design.

        Worth reading before using: an interaction is irreversible, so the
        engine's floor makes it ASK even with an explicit grant. A Step 5 tool
        written as ``if auth.allowed: click()`` would therefore never click,
        and the obvious repair — dropping the check — would skip the engine
        entirely. Branch on all three outcomes instead; the vocabulary below
        mirrors :class:`PermissionDecision` so there is no reason not to.
        """
        return self.decision.mode is PermissionMode.ALLOW

    @property
    def denied(self) -> bool:
        return self.decision.mode is PermissionMode.DENY

    @property
    def needs_confirmation(self) -> bool:
        """Ask the user, then proceed on their approval — never on your own."""
        return self.decision.mode is PermissionMode.ASK

    def describe(self) -> dict[str, Any]:
        return {
            "origin": self.origin,
            "operation": self.operation.value,
            **self.decision.describe(),
        }


class BrowserPolicy:
    """Authorises browser actions against origins.

    Deliberately not an engine. It builds a request and hands it to the one
    that exists; there is no mode ceiling here, no second grants table, and no
    rule that could disagree with Phase 1.
    """

    def __init__(self, session: Any) -> None:
        self.session = session

    async def authorize(
        self,
        operation: BrowserOperation,
        *,
        origin: str,
        user_id: str,
        tainted: bool = False,
    ) -> BrowserAuthorisation:
        """Decide, before the action happens.

        ``tainted`` is threaded from :attr:`ToolContext.tainted`, which by
        Step 4A includes taint a browser page itself contributed earlier in the
        turn. That is the loop that matters: a page JARVIS read makes the turn
        tainted, and the engine escalates the click that page suggested.
        """
        capability, risk, reversible = OPERATIONS[operation]
        decision = await PermissionEngine(self.session).evaluate(
            PermissionRequest(
                user_id=user_id,
                capability=capability,
                resource=resource_for(origin),
                risk_level=risk,
                reversible=reversible,
                tainted=tainted,
            )
        )
        log.info(
            "browser_permission_decision",
            origin=origin,
            operation=operation.value,
            mode=decision.mode.value,
            tainted=tainted,
        )
        return BrowserAuthorisation(
            decision=decision, origin=origin, operation=operation
        )


# ── credentials (4E) ─────────────────────────────────────────────────────────

#: Long, unambiguous markers, matched as substrings of the field's identifiers
#: with every separator removed. Collapsing separators is what makes one entry
#: cover "API key", "api-key", "api_key" and "apiKey" — a list that spelled
#: each variant out would be a list with a gap in it.
CREDENTIAL_SUBSTRINGS: tuple[str, ...] = (
    "password", "passwd", "passphrase",
    "apikey", "credential", "privatekey", "secretkey", "accesskey",
    "seedphrase", "mnemonic", "recoveryphrase", "recoverycode",
    "onetimecode", "onetimepassword", "verificationcode", "securitycode",
    "authcode", "authenticationcode", "cardsecurity", "securityanswer",
)

#: Short markers that are real words or appear inside innocent ones, matched on
#: word boundaries instead.
#:
#: ``pin`` is why this tier exists: as a substring it fires on "shipping". A
#: rule that refuses the shipping address is a rule someone turns off, and then
#: the password check goes with it.
CREDENTIAL_WORDS: tuple[str, ...] = (
    "pwd", "pin", "cvv", "cvc", "csc", "otp", "totp", "mfa", "2fa",
    "token", "secret", "seed",
)

#: ``autocomplete`` values the HTML spec defines for credential fields. Exact
#: matches, since these are a closed vocabulary rather than free text.
CREDENTIAL_AUTOCOMPLETE = frozenset({
    "current-password", "new-password", "one-time-code",
    "cc-csc", "cc-number",
})

_SUBSTRING_RE = re.compile(
    "|".join(re.escape(h) for h in CREDENTIAL_SUBSTRINGS), re.IGNORECASE
)
_WORD_RE = re.compile(
    r"\b(" + "|".join(re.escape(w) for w in CREDENTIAL_WORDS) + r")\b",
    re.IGNORECASE,
)
_SEPARATORS_RE = re.compile(r"[^0-9a-z]+")


def _squashed(value: str) -> str:
    """Lowercase, separators removed. ``"API key"`` and ``"api_key"`` converge."""
    return _SEPARATORS_RE.sub("", value.lower())


def _worded(value: str) -> str:
    """Lowercase, separators normalised to spaces, camelCase split.

    Both transforms matter for the short-token tier: ``otpValue`` has no
    separator to normalise, and without the case split ``\\botp\\b`` misses it.
    """
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", value)
    return _SEPARATORS_RE.sub(" ", spaced.lower()).strip()


def _hint_in(value: str) -> str | None:
    """The credential marker in this string, or ``None``."""
    if not value:
        return None
    match = _SUBSTRING_RE.search(_squashed(value))
    if match:
        return match.group(0)
    match = _WORD_RE.search(_worded(value))
    return match.group(0) if match else None


@dataclass(slots=True)
class FieldInspection:
    """What the DOM says about an input, reduced to what the decision needs.

    A plain dataclass rather than a Playwright handle so the rule is a pure
    function of observable attributes — testable without a browser, and
    identical whether the attributes came from a real page or a fixture.
    """

    tag: str = "input"
    type: str = "text"
    name: str = ""
    element_id: str = ""
    autocomplete: str = ""
    aria_label: str = ""
    placeholder: str = ""
    label: str = ""

    def signals(self) -> list[str]:
        """Everything worth matching a hint against."""
        return [self.name, self.element_id, self.aria_label, self.placeholder,
                self.label]


class CredentialRefused(BrowserError):
    """The field is one JARVIS will not type into."""

    code = "browser_credential_field_refused"


def credential_reason(field_info: FieldInspection) -> str | None:
    """Why JARVIS will not type here, or ``None`` if it will.

    Checked in order of confidence, so the reason names the strongest signal
    rather than whichever matched first alphabetically.
    """
    if (field_info.type or "").strip().lower() == "password":
        return "it is a password field (<input type=\"password\">)"

    autocomplete = (field_info.autocomplete or "").strip().lower()
    if autocomplete in CREDENTIAL_AUTOCOMPLETE:
        return f'its autocomplete is "{autocomplete}", which marks it a credential field'

    for signal in field_info.signals():
        hint = _hint_in(signal)
        if hint is not None:
            return (
                f'it looks like a credential field ("{hint}" appears in its '
                "name, id or label)"
            )
    return None


def refuse_if_credential(field_info: FieldInspection, *, target: str = "") -> None:
    """Raise unless the field is safe to type into.

    The refusal is the product's behaviour, not advice to the model: it happens
    below the tool layer, against the live DOM, and no argument in a page or a
    prompt reaches it.
    """
    reason = credential_reason(field_info)
    if reason is None:
        return
    where = f" on {target}" if target else ""
    raise CredentialRefused(
        f"Refusing to type into a credential field{where}: {reason}.",
        "That field asks for a password or security code, and JARVIS never "
        "types those. Enter it yourself in the browser window, then tell me to "
        "carry on.",
    )


#: Playwright evaluation that reduces an element to a :class:`FieldInspection`.
#: Kept here beside the rule it feeds so the two cannot drift apart, and used
#: by the browser tools in a later step — nothing calls it yet.
FIELD_INSPECTION_JS = """
(el) => {
  const attr = (n) => (el.getAttribute(n) || '');
  let label = '';
  if (el.labels && el.labels.length) label = el.labels[0].textContent || '';
  if (!label && el.id) {
    const l = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
    if (l) label = l.textContent || '';
  }
  return {
    tag: el.tagName.toLowerCase(),
    type: (el.type || attr('type') || 'text').toLowerCase(),
    name: attr('name'),
    element_id: el.id || '',
    autocomplete: attr('autocomplete'),
    aria_label: attr('aria-label'),
    placeholder: attr('placeholder'),
    label: label.trim(),
  };
}
"""


def inspection_from_dom(payload: dict[str, Any] | None) -> FieldInspection:
    """Build the inspection from :data:`FIELD_INSPECTION_JS`'s output.

    A missing or malformed payload produces a field that *looks* like a
    password field rather than a benign one. Failing closed matters here: the
    only reason the payload would be missing is that the element could not be
    inspected, and an element JARVIS cannot see is not one it should type a
    secret into.
    """
    if not payload:
        return FieldInspection(type="password", name="uninspectable")
    return FieldInspection(
        tag=str(payload.get("tag") or "input"),
        type=str(payload.get("type") or "text"),
        name=str(payload.get("name") or ""),
        element_id=str(payload.get("element_id") or ""),
        autocomplete=str(payload.get("autocomplete") or ""),
        aria_label=str(payload.get("aria_label") or ""),
        placeholder=str(payload.get("placeholder") or ""),
        label=str(payload.get("label") or ""),
    )
