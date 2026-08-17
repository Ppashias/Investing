"""What an action *does to the world*, as one word (Phase D, item 4).

Capability answers "which domain does this touch". Risk answers "how bad if it
goes wrong". Neither answers the question a person actually asks when a dialog
appears, which is *"can I take this back?"*

`vierisid/jarvis` separates these, and it is right to: its ``IMPACT_MAP`` turns
thirteen action categories into four impacts, and its dashboard renders the
impact rather than the category. The reasoning is visible in its voice code —
destructive impacts never resolve by a spoken "yes", because a single misheard
syllable could trigger a payment. That rule needs a word for "destructive", and
capability does not supply one: ``EXTERNAL_ACTION`` covers both *read a public
page* and *send an email to your employer*.

## Derived, never declared

Ours is computed from ``(capability, reversible, risk_level)`` — three fields
every tool already carries — rather than added as a fourth thing each tool
declares. A declared impact is a field somebody forgets to set, and the failure
mode of forgetting is a destructive action rendered as a routine one.

The derivation is deliberately pessimistic at every junction. Where two
readings are defensible, this picks the one that makes the dialog louder.
"""

from __future__ import annotations

import enum

from jarvis.db.models import Capability, RiskLevel


class Impact(str, enum.Enum):
    """How far an action reaches, and whether it can be undone."""

    #: Observes. No side effects anywhere.
    READ = "read"
    #: Changes something JARVIS owns — a note, a task, a memory. Reversible.
    WRITE = "write"
    #: Leaves the machine. A page fetch, a form submission.
    EXTERNAL = "external"
    #: Cannot be undone, costs money, or reaches someone else. The category
    #: that must never be approved by a mishearing.
    DESTRUCTIVE = "destructive"

    @property
    def is_destructive(self) -> bool:
        return self is Impact.DESTRUCTIVE


#: Sentence fragments for the confirmation body. Written for the person
#: reading them at speed, which is when the difference has to be obvious.
_PHRASING = {
    Impact.READ: "This only reads.",
    Impact.WRITE: "This changes something JARVIS keeps, and can be undone.",
    Impact.EXTERNAL: "This reaches outside this machine.",
    Impact.DESTRUCTIVE: "This cannot be undone.",
}


def impact_of(
    capability: Capability,
    *,
    reversible: bool = True,
    risk_level: RiskLevel = RiskLevel.NONE,
) -> Impact:
    """Classify one action.

    Order matters and is pessimistic:

    1. **Irreversible is destructive**, whatever the capability. This is the
       same rule the permission engine already applies as its reversibility
       floor, expressed for a human instead of for a decision.
    2. **CRITICAL risk is destructive**, because a tool rated critical and
       rendered as "changes something" is a dialog that misinforms.
    3. Otherwise the capability decides, and ``SENSITIVE_ACTION`` is
       destructive by definition — it is the class the engine denies outright.

    A read is the only thing that can come back READ, and only when it is both
    reversible and not critical. Everything else has to earn its way down.
    """
    if not reversible:
        return Impact.DESTRUCTIVE
    if risk_level is RiskLevel.CRITICAL:
        return Impact.DESTRUCTIVE
    if capability is Capability.SENSITIVE_ACTION:
        return Impact.DESTRUCTIVE
    if capability is Capability.EXECUTE:
        # Running a command is not reversible in any sense we can verify, even
        # when the tool claims it is. HIGH-risk execution is the shell.
        return (
            Impact.DESTRUCTIVE
            if risk_level in {RiskLevel.HIGH, RiskLevel.CRITICAL}
            else Impact.EXTERNAL
        )
    if capability is Capability.EXTERNAL_ACTION:
        return Impact.EXTERNAL
    if capability is Capability.WRITE:
        return Impact.WRITE
    return Impact.READ


def describe_impact(impact: Impact) -> str:
    """One sentence, for the human reading the dialog."""
    return _PHRASING[impact]


def impact_of_tool(tool: object) -> Impact:
    """Convenience for the executor, which holds a whole ``Tool``."""
    return impact_of(
        getattr(tool, "capability", Capability.READ),
        reversible=getattr(tool, "reversible", True),
        risk_level=getattr(tool, "risk_level", RiskLevel.NONE),
    )
