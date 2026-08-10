"""Retry policy for provider calls.

Kept out of the provider implementations so every provider gets identical
behaviour and so retry is testable without a network. Only errors flagged
``retryable`` in the taxonomy are retried; a bad request or an auth failure
fails immediately rather than burning three attempts on a certainty.

``asyncio.CancelledError`` is re-raised untouched — a cancelled request must
not be retried into life.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TypeVar

from jarvis.errors import JarvisError, ProviderRateLimitError
from jarvis.logging import get_logger

log = get_logger(__name__)

T = TypeVar("T")


@dataclass(slots=True)
class RetryPolicy:
    max_attempts: int = 3
    base_delay: float = 0.5
    max_delay: float = 20.0
    #: Full jitter. Without it, concurrent failures retry in lockstep and
    #: re-collide — which is exactly the situation rate limiting creates.
    jitter: bool = True

    def delay_for(self, attempt: int, retry_after: float | None = None) -> float:
        if retry_after is not None:
            return min(retry_after, self.max_delay)
        delay = min(self.base_delay * (2 ** (attempt - 1)), self.max_delay)
        if self.jitter:
            delay = random.uniform(0, delay)
        return delay

    async def run(
        self,
        operation: Callable[[], Awaitable[T]],
        *,
        description: str = "provider_call",
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> T:
        sleeper = sleep or asyncio.sleep
        last_error: BaseException | None = None

        for attempt in range(1, self.max_attempts + 1):
            try:
                return await operation()
            except asyncio.CancelledError:
                raise
            except JarvisError as exc:
                last_error = exc
                if not exc.retryable or attempt == self.max_attempts:
                    raise
                retry_after = getattr(exc, "retry_after", None)
                delay = self.delay_for(attempt, retry_after)
                log.warning(
                    "provider_retry",
                    operation=description,
                    attempt=attempt,
                    max_attempts=self.max_attempts,
                    error_code=exc.code,
                    delay_seconds=round(delay, 3),
                )
                await sleeper(delay)
            except Exception as exc:  # unexpected — do not mask, do not retry
                last_error = exc
                raise

        assert last_error is not None  # pragma: no cover - unreachable
        raise last_error


DEFAULT_RETRY_POLICY = RetryPolicy()


def rate_limit_from_headers(headers: dict[str, str] | None) -> float | None:
    """Extract ``retry-after`` seconds from a response header map."""
    if not headers:
        return None
    for key in ("retry-after", "Retry-After", "x-ratelimit-reset-after"):
        raw = headers.get(key)
        if raw:
            try:
                return float(raw)
            except (TypeError, ValueError):
                continue
    return None


__all__ = [
    "RetryPolicy",
    "DEFAULT_RETRY_POLICY",
    "ProviderRateLimitError",
    "rate_limit_from_headers",
]
