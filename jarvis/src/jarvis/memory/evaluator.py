"""Memory evaluation — deciding what is worth remembering (§12).

§12's requirement is "do not store every sentence", and the hard part is not
the filter but *who* applies it. Two mechanisms, in order of trust:

1. **Explicit instruction.** "Remember that I prefer X" is unambiguous. Handled
   by the ``remember`` tool, stored at full confidence, never subject to this
   module's judgement. The user asked; that settles it.

2. **Ambient capture.** Everything else. A turn may contain something worth
   keeping, and the only thing capable of recognising it is the model — a
   regex cannot tell that "we decided to use streamed sublevels" is a durable
   project decision while "let me check that file" is not.

## Why capture is a separate model call

Ambient extraction runs *after* the response, not inside it. Two reasons, and
both are load-bearing:

* Latency. The user waits for the answer, not for the bookkeeping.
* Contamination. A model asked to answer *and* decide what to remember does
  both worse, and its memory decisions leak into its wording.

The consequence is that extraction is a second, cheap model call, using the
fast task class. When no provider is configured it does not run at all, and
:meth:`MemoryEvaluator.available` says so — explicit memory commands still work
because they never needed a model.

## Default is to ask, not to store

``memory_capture_mode`` defaults to ``ask``: candidates are written with
``PROPOSED`` status and surfaced for a yes/no (§14). Silent accumulation of
inferences about someone is the failure mode worth avoiding, and a memory the
user confirmed is worth more than three JARVIS guessed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from jarvis.logging import get_logger
from jarvis.memory import guard
from jarvis.memory.service import MemoryDraft, MemoryService, WriteOutcome
from jarvis.memory.types import (
    CONFIDENCE_EXPLICIT,
    CONFIDENCE_INFERRED,
    CONFIDENCE_WEAK,
    MemorySource,
    MemoryStatus,
    MemoryType,
)

log = get_logger(__name__)

EXTRACTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "memories": {
            "type": "array",
            "maxItems": 5,
            "items": {
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": (
                            "The fact, written to stand alone months later. "
                            "Third person, no pronouns referring to this "
                            "conversation."
                        ),
                    },
                    "subject": {
                        "type": "string",
                        "description": (
                            "2-5 words naming what this is ABOUT, not what it "
                            "says. 'interface theme preference', not 'prefers "
                            "dark mode'. Memories on the same subject are "
                            "reconciled with each other."
                        ),
                    },
                    "type": {"type": "string", "enum": [t.value for t in MemoryType]},
                    "importance": {"type": "number", "minimum": 0, "maximum": 1},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    "reason": {"type": "string"},
                },
                "required": ["content", "subject", "type", "importance", "confidence"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["memories"],
    "additionalProperties": False,
}

EXTRACTION_PROMPT = """
You are the memory evaluator for JARVIS, a personal AI assistant. Read the
exchange below and decide whether anything in it is worth remembering long
term.

Remember something only if it will still be useful weeks or months from now:

- Stable preferences about how the user likes to work
- Long-term goals and the projects they belong to
- Decisions made about a project, and the reasoning
- Facts about the user's setup, tools, or constraints
- Lessons: an approach that failed and should not be repeated
- Significant events worth dating

Do NOT remember:

- Anything answerable by looking it up again
- The content of this request or your response to it
- Transient state — what is open right now, what is being tried this minute
- Pleasantries, acknowledgements, thinking aloud
- Anything you are merely inferring from tone or a single ambiguous remark
- Passwords, keys, tokens, card numbers, or any other credential. If the
  exchange contains one, remember nothing about it at all.

Most exchanges contain nothing worth remembering. Returning an empty list is
the correct and common answer. Do not invent something to justify the call.

Set `confidence` by how certain the fact is: 0.9+ only if the user stated it
outright, 0.5-0.7 if it is clearly implied, below 0.5 if you are guessing —
and if you are guessing, prefer not to record it.

Set `importance` by how much it would matter later: 0.8+ for goals and project
decisions, 0.5-0.7 for preferences and durable facts, below 0.4 for detail
unlikely to come up again.
""".strip()


@dataclass(slots=True)
class MemoryCandidate:
    content: str
    subject: str
    type: MemoryType
    importance: float
    confidence: float
    reason: str = ""

    def to_draft(
        self,
        *,
        project_id: str | None,
        conversation_id: str | None,
        status: MemoryStatus,
    ) -> MemoryDraft:
        return MemoryDraft(
            content=self.content,
            subject=self.subject,
            type=self.type,
            source=MemorySource.CONVERSATION,
            confidence=self.confidence,
            importance=self.importance,
            project_id=project_id,
            conversation_id=conversation_id,
            status=status,
            meta={"reason": self.reason} if self.reason else {},
        )


@dataclass(slots=True)
class EvaluationResult:
    candidates: list[MemoryCandidate] = field(default_factory=list)
    outcomes: list[WriteOutcome] = field(default_factory=list)
    proposed: list[str] = field(default_factory=list)
    stored: list[str] = field(default_factory=list)
    refused: int = 0
    skipped_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidates": len(self.candidates),
            "stored": self.stored,
            "proposed": self.proposed,
            "refused": self.refused,
            "skipped_reason": self.skipped_reason,
        }


class MemoryEvaluator:
    """Extracts memory candidates from a completed exchange."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        router: Any = None,
        embeddings: Any = None,
        capture_mode: str = "ask",
        min_importance: float = 0.45,
        duplicate_threshold: float = 0.87,
    ) -> None:
        self.session = session
        self.router = router
        self.embeddings = embeddings
        self.capture_mode = capture_mode
        self.min_importance = min_importance
        self.duplicate_threshold = duplicate_threshold

    def available(self) -> bool:
        """False when ambient capture cannot run. Reported by the API rather
        than silently doing nothing."""
        return self.capture_mode != "off" and self.router is not None

    async def evaluate_exchange(
        self,
        *,
        user_id: str,
        user_message: str,
        assistant_message: str,
        conversation_id: str | None = None,
        project_id: str | None = None,
        request_id: str | None = None,
    ) -> EvaluationResult:
        if self.capture_mode == "off":
            return EvaluationResult(skipped_reason="capture_mode=off")
        if self.router is None:
            return EvaluationResult(skipped_reason="no_provider_configured")

        # Cheap pre-filter. An exchange this short cannot contain a durable
        # fact, and skipping it avoids a model call on every "thanks".
        if len(user_message.strip()) < 12:
            return EvaluationResult(skipped_reason="exchange_too_short")

        candidates = await self._extract(user_message, assistant_message)
        if not candidates:
            return EvaluationResult(skipped_reason="nothing_worth_remembering")

        return await self._persist(
            candidates,
            user_id=user_id,
            conversation_id=conversation_id,
            project_id=project_id,
            request_id=request_id,
        )

    # ── extraction ───────────────────────────────────────────────────────────

    async def _extract(
        self, user_message: str, assistant_message: str
    ) -> list[MemoryCandidate]:
        from jarvis.providers.base import ChatMessage, CompletionRequest, TextBlock
        from jarvis.providers.router import TaskClass

        exchange = (
            f"User said:\n{user_message.strip()}\n\n"
            f"JARVIS replied:\n{assistant_message.strip()[:4000]}"
        )
        try:
            routing = self.router.route(
                TaskClass.FAST, needs_tools=False, needs_structured_output=True
            )
            result = await routing.provider.complete(
                CompletionRequest(
                    messages=[
                        ChatMessage(role="user", content=[TextBlock(text=exchange)])
                    ],
                    system=EXTRACTION_PROMPT,
                    model=routing.model,
                    max_tokens=1200,
                    output_schema=EXTRACTION_SCHEMA,
                )
            )
        except Exception as exc:
            # Never fails the turn: the answer is already delivered, and losing
            # a memory candidate is not worth surfacing an error for.
            log.warning("memory_extraction_failed", error=str(exc))
            return []

        return self._parse(result)

    def _parse(self, result: Any) -> list[MemoryCandidate]:
        from jarvis.providers.base import TextBlock

        text = "".join(
            block.text for block in result.content if isinstance(block, TextBlock)
        ).strip()
        if not text:
            return []

        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            log.warning("memory_extraction_unparseable", length=len(text))
            return []

        candidates: list[MemoryCandidate] = []
        for raw in payload.get("memories", [])[:5]:
            try:
                content = str(raw["content"]).strip()
                subject = str(raw["subject"]).strip().lower()
                if not content or not subject:
                    continue
                candidates.append(
                    MemoryCandidate(
                        content=content,
                        subject=subject,
                        type=MemoryType(raw["type"]),
                        importance=_clamp(float(raw["importance"])),
                        confidence=_clamp(float(raw["confidence"])),
                        reason=str(raw.get("reason", ""))[:300],
                    )
                )
            except (KeyError, ValueError, TypeError) as exc:
                # A malformed candidate is dropped, not guessed at. The model
                # produced it; it can produce a better one next turn.
                log.warning("memory_candidate_invalid", error=str(exc))
        return candidates

    # ── persistence ──────────────────────────────────────────────────────────

    async def _persist(
        self,
        candidates: Sequence[MemoryCandidate],
        *,
        user_id: str,
        conversation_id: str | None,
        project_id: str | None,
        request_id: str | None,
    ) -> EvaluationResult:
        result = EvaluationResult(candidates=list(candidates))
        service = MemoryService(
            self.session,
            embeddings=self.embeddings,
            duplicate_threshold=self.duplicate_threshold,
        )

        for candidate in candidates:
            if candidate.importance < self.min_importance:
                log.debug(
                    "memory_candidate_below_threshold",
                    importance=candidate.importance, subject=candidate.subject,
                )
                continue

            # Belt and braces. The service guards every write, but catching a
            # credential here means it never becomes a proposal the user is
            # shown and asked to approve.
            if guard.inspect(candidate.content).blocked:
                result.refused += 1
                log.warning("memory_candidate_refused_secret")
                continue

            # An inferred memory is capped below what an explicit instruction
            # earns, whatever the model claimed about its own certainty.
            confidence = min(candidate.confidence, CONFIDENCE_INFERRED + 0.2)
            status = (
                MemoryStatus.PROPOSED
                if self.capture_mode == "ask"
                else MemoryStatus.ACTIVE
            )
            draft = candidate.to_draft(
                project_id=project_id,
                conversation_id=conversation_id,
                status=status,
            )
            draft.confidence = confidence

            outcome = await service.create(
                user_id, draft, actor="evaluator", request_id=request_id,
                # A proposal must not silently rewrite an existing memory
                # before the user has agreed to it.
                reconcile=status is MemoryStatus.ACTIVE,
            )
            result.outcomes.append(outcome)

            if outcome.action == "refused":
                result.refused += 1
            elif outcome.memory is not None:
                if status is MemoryStatus.PROPOSED:
                    result.proposed.append(outcome.memory.id)
                else:
                    result.stored.append(outcome.memory.id)

        log.info("memory_evaluated", **result.to_dict())
        return result


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


#: Confidence for a memory the user asked for in so many words. Exported so
#: the ``remember`` tool and this module cannot drift apart.
EXPLICIT_CONFIDENCE = CONFIDENCE_EXPLICIT
WEAK_CONFIDENCE = CONFIDENCE_WEAK
