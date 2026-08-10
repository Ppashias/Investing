"""Memory vocabulary — the single definition of what a memory *is*.

Deliberately free of database and service imports. Everything else in JARVIS
imports its memory vocabulary from here, including :mod:`jarvis.db.models`,
which is why this module must stay a leaf: the brief (§9) asks that memory
types not be hard-coded across the application, and the enforcement mechanism
is that there is exactly one place they can come from.

Three scales appear repeatedly and are easy to confuse, so they are named:

* **Confidence** — how sure JARVIS is that the memory is *true*. Explicit user
  instruction is certain; something inferred from one ambiguous sentence is
  not. Drives whether the model says "I know" or "I believe" (§17).
* **Importance** — how much the memory *matters* if true. Independent of
  confidence: "the user's cat is called Mia" can be certain and unimportant.
  Drives retrieval ranking and what survives pruning (§18).
* **Relevance** — how well a memory matches *this* request. Computed per query
  at retrieval time, never stored.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass


class MemoryType(str, enum.Enum):
    """What kind of thing is remembered.

    The split that matters is personal (``USER_*``) versus project-scoped
    (``PROJECT_*``) versus derived-from-documents (``KNOWLEDGE``/``REFERENCE``).
    The brief (§8) asks that semantic knowledge stay separate from personal
    memory, and this is where that separation starts.
    """

    USER_FACT = "USER_FACT"
    USER_PREFERENCE = "USER_PREFERENCE"
    USER_GOAL = "USER_GOAL"
    USER_ROUTINE = "USER_ROUTINE"
    PROJECT_FACT = "PROJECT_FACT"
    PROJECT_DECISION = "PROJECT_DECISION"
    PROJECT_STATE = "PROJECT_STATE"
    IMPORTANT_EVENT = "IMPORTANT_EVENT"
    LESSON_LEARNED = "LESSON_LEARNED"
    WORKFLOW = "WORKFLOW"
    SYSTEM_PREFERENCE = "SYSTEM_PREFERENCE"
    REFERENCE = "REFERENCE"
    KNOWLEDGE = "KNOWLEDGE"

    @property
    def is_project_scoped(self) -> bool:
        return self in _PROJECT_SCOPED

    @property
    def is_episodic(self) -> bool:
        """True for memories about *events*, which are never superseded.

        A newer event does not make an older one wrong — "the build failed in
        March" stays true after it succeeds in April. Contradiction handling
        (§16) must not treat these as conflicting.
        """
        return self in _EPISODIC


_PROJECT_SCOPED = frozenset(
    {
        MemoryType.PROJECT_FACT,
        MemoryType.PROJECT_DECISION,
        MemoryType.PROJECT_STATE,
    }
)

_EPISODIC = frozenset({MemoryType.IMPORTANT_EVENT, MemoryType.LESSON_LEARNED})


class MemorySource(str, enum.Enum):
    """Where a memory came from.

    ``OBSIDIAN`` exists now although no connector does (§10). A source type is
    a schema commitment, not an implementation claim: adding it later would
    mean migrating every existing row's provenance, whereas adding it now costs
    one enum member. Nothing produces it in Phase 2 and a test asserts that.
    """

    USER = "USER"
    CONVERSATION = "CONVERSATION"
    PROJECT = "PROJECT"
    DOCUMENT = "DOCUMENT"
    OBSIDIAN = "OBSIDIAN"
    WEB = "WEB"
    AGENT = "AGENT"
    SYSTEM = "SYSTEM"

    @property
    def is_external(self) -> bool:
        """True for sources whose content JARVIS did not author or receive
        directly from the user.

        External content is data, never instructions (§42). Memories from these
        sources are stored tainted, which makes the permission engine escalate
        anything they subsequently influence.
        """
        return self in {MemorySource.DOCUMENT, MemorySource.OBSIDIAN, MemorySource.WEB}


class MemoryStatus(str, enum.Enum):
    """Lifecycle state.

    The brief's lifecycle (§11) mixes states with events: DISCOVERED and
    ARCHIVED are states a memory sits in, but RETRIEVED and CONFIRMED are
    things that *happen* to it. Modelling the events as states would mean a
    memory retrieved once could never be "stored" again. So states live here
    and events live in ``memory_revisions``, which is also what gives the user
    a history to inspect when correcting JARVIS.
    """

    #: Evaluated as worth keeping but awaiting the user's yes/no (§14).
    PROPOSED = "PROPOSED"
    #: Live. The only status retrieval will surface.
    ACTIVE = "ACTIVE"
    #: Replaced by a newer memory after a detected contradiction. Kept, because
    #: "what did I used to think?" is a real question and because a wrong
    #: supersession must be reversible.
    SUPERSEDED = "SUPERSEDED"
    #: User said no at the confirmation prompt. Kept briefly so JARVIS does not
    #: immediately re-propose the same thing.
    REJECTED = "REJECTED"
    #: Soft-deleted. Invisible to retrieval, visible in the UI, restorable.
    ARCHIVED = "ARCHIVED"
    #: Hard-deleted tombstone: id and deletion time only, content gone.
    DELETED = "DELETED"

    @property
    def is_retrievable(self) -> bool:
        return self is MemoryStatus.ACTIVE


class MemoryRelation(str, enum.Enum):
    """Typed edges between memories."""

    SUPERSEDES = "SUPERSEDES"
    CONTRADICTS = "CONTRADICTS"
    DUPLICATE_OF = "DUPLICATE_OF"
    RELATED_TO = "RELATED_TO"
    DERIVED_FROM = "DERIVED_FROM"


class RevisionKind(str, enum.Enum):
    """Lifecycle events recorded against a memory (§11)."""

    CREATED = "CREATED"
    UPDATED = "UPDATED"
    CONFIRMED = "CONFIRMED"
    CORRECTED = "CORRECTED"
    MERGED = "MERGED"
    SUPERSEDED = "SUPERSEDED"
    ARCHIVED = "ARCHIVED"
    RESTORED = "RESTORED"
    DELETED = "DELETED"
    ACCESSED = "ACCESSED"


# ── confidence and importance ────────────────────────────────────────────────
#
# Stored as floats so ranking arithmetic is straightforward, presented as bands
# so the UI and the model never have to interpret "0.63".


class ConfidenceBand(str, enum.Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CERTAIN = "CERTAIN"


class ImportanceBand(str, enum.Enum):
    TRIVIAL = "TRIVIAL"
    LOW = "LOW"
    NORMAL = "NORMAL"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


#: What the user said in so many words. Nothing inferred ever reaches this.
CONFIDENCE_EXPLICIT = 1.0
#: A preference observed several times.
CONFIDENCE_OBSERVED = 0.75
#: A single unambiguous statement that was not addressed to memory.
CONFIDENCE_INFERRED = 0.55
#: One ambiguous statement. Retrievable, but always hedged.
CONFIDENCE_WEAK = 0.35


def confidence_band(value: float) -> ConfidenceBand:
    if value >= 0.95:
        return ConfidenceBand.CERTAIN
    if value >= 0.7:
        return ConfidenceBand.HIGH
    if value >= 0.45:
        return ConfidenceBand.MEDIUM
    return ConfidenceBand.LOW


def importance_band(value: float) -> ImportanceBand:
    if value >= 0.85:
        return ImportanceBand.CRITICAL
    if value >= 0.65:
        return ImportanceBand.HIGH
    if value >= 0.4:
        return ImportanceBand.NORMAL
    if value >= 0.2:
        return ImportanceBand.LOW
    return ImportanceBand.TRIVIAL


def hedge_for(value: float) -> str:
    """How the model should introduce a memory of this confidence (§17).

    Low-confidence inferences must never be presented as unquestionable fact,
    and the cheapest reliable way to enforce that is to hand the model the
    wording rather than hope it calibrates.
    """
    band = confidence_band(value)
    return {
        ConfidenceBand.CERTAIN: "you told me",
        ConfidenceBand.HIGH: "I remember",
        ConfidenceBand.MEDIUM: "I believe",
        ConfidenceBand.LOW: "I am not sure, but I think",
    }[band]


#: Default importance per type, applied when nothing better is known. A
#: decision is worth more than a passing fact by default; the evaluator can
#: override, and the user always can.
DEFAULT_IMPORTANCE: dict[MemoryType, float] = {
    MemoryType.USER_FACT: 0.5,
    MemoryType.USER_PREFERENCE: 0.65,
    MemoryType.USER_GOAL: 0.8,
    MemoryType.USER_ROUTINE: 0.6,
    MemoryType.PROJECT_FACT: 0.55,
    MemoryType.PROJECT_DECISION: 0.8,
    MemoryType.PROJECT_STATE: 0.7,
    MemoryType.IMPORTANT_EVENT: 0.6,
    MemoryType.LESSON_LEARNED: 0.75,
    MemoryType.WORKFLOW: 0.7,
    MemoryType.SYSTEM_PREFERENCE: 0.6,
    MemoryType.REFERENCE: 0.35,
    MemoryType.KNOWLEDGE: 0.4,
}


@dataclass(frozen=True, slots=True)
class MemoryScore:
    """Why a memory was retrieved. Returned alongside results so the ranking is
    inspectable rather than a black box."""

    semantic: float = 0.0
    keyword: float = 0.0
    structured: float = 0.0
    importance: float = 0.0
    recency: float = 0.0
    confidence: float = 0.0
    total: float = 0.0

    def describe(self) -> dict[str, float]:
        return {
            "semantic": round(self.semantic, 4),
            "keyword": round(self.keyword, 4),
            "structured": round(self.structured, 4),
            "importance": round(self.importance, 4),
            "recency": round(self.recency, 4),
            "confidence": round(self.confidence, 4),
            "total": round(self.total, 4),
        }
