"""Secret detection for memory writes (§34).

Secrets must never become ordinary memories. A memory is retrieved into model
context automatically, may be exported, and is displayed in a UI — so a
credential that lands here is a credential that leaks by three separate routes
without anyone doing anything wrong.

This runs on **every** write path: the explicit "remember this" tool, the
automatic evaluator, and the REST API. Placing it in the service rather than at
the call sites is deliberate; a check that each caller must remember to make is
a check that a future caller will forget.

## What this can and cannot do

It catches credential *shapes* (recognisable key formats, high-entropy strings
in credential-like contexts, private key armour) and credential *statements*
("my password is …"). It is a pattern matcher, so it will not catch a password
that happens to look like an ordinary word, and no pattern matcher would.

The design follows from that limit. The check is deliberately biased toward
false positives, and refusal is cheap to recover from — JARVIS says it will not
store that and the user rephrases — whereas a missed credential is permanent
and silent. Where the two error modes are that asymmetric, tuning for precision
is the wrong instinct.

The user can override for a specific memory via the API's ``allow_sensitive``
flag, which is recorded in the revision history. That exists because a blanket
prohibition someone cannot override is a prohibition they will work around by
disabling the feature.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class GuardVerdict:
    blocked: bool
    #: Short machine-readable reason: ``api_key``, ``private_key``, ...
    reason: str = ""
    #: Explanation safe to show the user. Never quotes the matched text — a
    #: refusal message that echoes the secret defeats the refusal.
    detail: str = ""

    @property
    def allowed(self) -> bool:
        return not self.blocked


#: Credential formats specific enough to be near-certain when they match.
_SHAPES: list[tuple[str, re.Pattern[str]]] = [
    ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("anthropic_key", re.compile(r"sk-ant-[A-Za-z0-9_\-]{16,}")),
    ("openai_key", re.compile(r"\bsk-[A-Za-z0-9]{32,}")),
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}")),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("slack_token", re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}")),
    ("stripe_key", re.compile(r"\b[rs]k_(?:live|test)_[A-Za-z0-9]{16,}")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}")),
    ("bearer_token", re.compile(r"\bBearer\s+[A-Za-z0-9._\-]{20,}", re.IGNORECASE)),
    ("connection_string", re.compile(r"\b[a-z][a-z0-9+.\-]*://[^\s:@/]+:[^\s@/]+@", re.IGNORECASE)),
    ("card_number", re.compile(r"\b(?:\d[ -]?){13,19}\b")),
    ("iban", re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b")),
]

#: "<credential word> is/=/: <value>" — the statement form. The value has to be
#: long enough and unspaced enough to plausibly be a credential, which is what
#: keeps "my password is hard to remember" from matching.
_STATEMENT = re.compile(
    r"\b(?:password|passwd|passphrase|api[\s_-]?key|secret[\s_-]?key|access[\s_-]?token"
    r"|auth[\s_-]?token|client[\s_-]?secret|private[\s_-]?key|credential|pin[\s_-]?code"
    r"|security[\s_-]?code|cvv|otp[\s_-]?secret|recovery[\s_-]?code|seed[\s_-]?phrase)\b"
    r"\s*(?:for\s+\S+\s*)?(?:is|are|=|:)\s*[\"'`]?(?P<value>\S{6,})",
    re.IGNORECASE,
)

#: Credential word adjacent to a long unspaced high-entropy token, without the
#: connecting verb — "api_key abc123def456…" in pasted config.
_ADJACENT = re.compile(
    r"\b(?:password|api[\s_-]?key|secret|token|credential)\b[^\n]{0,20}?"
    r"(?P<value>[A-Za-z0-9_\-+/=]{24,})",
    re.IGNORECASE,
)

#: Words that make a long token benign despite the context: URLs, file paths,
#: and prose. Checked against the *matched value*, not the whole text.
_BENIGN_VALUE = re.compile(r"^(?:https?://|/|\./|~/|[a-z]+\.[a-z]{2,4}$)", re.IGNORECASE)


def shannon_entropy(text: str) -> float:
    """Bits per character. Random-looking strings score high; English scores
    around 3–4, and base64 key material around 5–6."""
    if not text:
        return 0.0
    counts: dict[str, int] = {}
    for char in text:
        counts[char] = counts.get(char, 0) + 1
    length = len(text)
    return -sum(
        (c / length) * math.log2(c / length) for c in counts.values()
    )


def _looks_random(value: str) -> bool:
    """High entropy *and* mixed character classes.

    Entropy alone flags long ordinary words in a way that gets irritating fast;
    requiring a mix of classes is what separates "correcthorsebatterystaple"
    from "kJ8x_2Qm-vT4".
    """
    if len(value) < 12:
        return False
    if _BENIGN_VALUE.match(value):
        return False
    classes = sum(
        bool(re.search(pattern, value))
        for pattern in (r"[a-z]", r"[A-Z]", r"\d", r"[^A-Za-z0-9]")
    )
    return shannon_entropy(value) >= 3.4 and classes >= 3


def _luhn_valid(digits: str) -> bool:
    """Card numbers pass Luhn; a 16-digit order reference usually does not.

    Without this, every long number — an ISBN, a build id, a phone number with
    separators — would be treated as a payment card.
    """
    numbers = [int(d) for d in digits if d.isdigit()]
    if not 13 <= len(numbers) <= 19:
        return False
    checksum = 0
    parity = len(numbers) % 2
    for index, digit in enumerate(numbers):
        if index % 2 == parity:
            digit *= 2
            if digit > 9:
                digit -= 9
        checksum += digit
    return checksum % 10 == 0


def inspect(text: str) -> GuardVerdict:
    """Decide whether ``text`` may be stored as a memory."""
    if not text:
        return GuardVerdict(blocked=False)

    for reason, pattern in _SHAPES:
        match = pattern.search(text)
        if not match:
            continue
        if reason == "card_number" and not _luhn_valid(match.group(0)):
            continue
        return GuardVerdict(
            blocked=True,
            reason=reason,
            detail=(
                "That looks like it contains a credential or payment detail "
                f"({reason.replace('_', ' ')}). I will not store it as a memory. "
                "Secrets belong in your keychain or .env file."
            ),
        )

    for reason, pattern in (("credential_statement", _STATEMENT),
                            ("credential_adjacent", _ADJACENT)):
        match = pattern.search(text)
        if match and _looks_random(match.group("value")):
            return GuardVerdict(
                blocked=True,
                reason=reason,
                detail=(
                    "That reads like a password or key being stated outright. "
                    "I will not store it as a memory. Tell me *that* a "
                    "credential exists and where it lives, not what it is."
                ),
            )

    return GuardVerdict(blocked=False)
