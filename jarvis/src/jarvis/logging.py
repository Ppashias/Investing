"""Structured logging with defence-in-depth secret redaction.

Two layers stop credentials reaching the log stream:

1. **Key-name redaction** — any event key that looks like a credential
   (``api_key``, ``token``, ``authorization``, ...) is replaced wholesale.
2. **Value redaction** — every secret the process has actually resolved is
   registered here, and its literal value is scrubbed from all rendered
   strings. This catches the case that key-name filtering misses: a key
   interpolated into an exception message or a URL.

Layer 2 is the important one. Layer 1 only protects fields we remembered to
name correctly, and the failure mode of forgetting is silent.

Request-scoped identifiers (request / conversation / task / execution) ride in
context variables so every log line inside a request carries them without the
call site passing them down.
"""

from __future__ import annotations

import logging
import re
import sys
from contextvars import ContextVar
from typing import Any, Iterator, MutableMapping

import structlog

# ── request-scoped context ───────────────────────────────────────────────────

_request_id: ContextVar[str | None] = ContextVar("request_id", default=None)
_conversation_id: ContextVar[str | None] = ContextVar("conversation_id", default=None)
_task_id: ContextVar[str | None] = ContextVar("task_id", default=None)
_execution_id: ContextVar[str | None] = ContextVar("execution_id", default=None)

_CONTEXT_VARS = {
    "request_id": _request_id,
    "conversation_id": _conversation_id,
    "task_id": _task_id,
    "execution_id": _execution_id,
}


def bind_context(**kwargs: str | None) -> dict[str, Any]:
    """Bind identifiers for the current async context. Returns reset tokens."""
    tokens: dict[str, Any] = {}
    for key, value in kwargs.items():
        var = _CONTEXT_VARS.get(key)
        if var is not None and value is not None:
            tokens[key] = var.set(value)
    return tokens


def reset_context(tokens: dict[str, Any]) -> None:
    for key, token in tokens.items():
        var = _CONTEXT_VARS.get(key)
        if var is not None:
            var.reset(token)


def current_request_id() -> str | None:
    return _request_id.get()


# ── redaction ────────────────────────────────────────────────────────────────

REDACTED = "***redacted***"

#: Substrings that mark a field as sensitive, matched case-insensitively
#: against the event key.
_SENSITIVE_KEY_PARTS = (
    "api_key",
    "apikey",
    "secret",
    "password",
    "passwd",
    "token",
    "authorization",
    "auth_header",
    "credential",
    "private_key",
    "session_key",
    "cookie",
    "bearer",
)

#: Literal secret values registered at resolution time (layer 2).
_known_secret_values: set[str] = set()

#: Shapes that look like credentials even if we never registered them —
#: a backstop for keys that arrive from somewhere we do not control.
_SECRET_PATTERNS = [
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"sk-[A-Za-z0-9]{32,}"),
    re.compile(r"Bearer\s+[A-Za-z0-9._\-]{16,}", re.IGNORECASE),
]

#: Values shorter than this are not scrubbed — redacting a 3-character string
#: would corrupt unrelated text far more often than it would protect anything.
_MIN_SCRUB_LENGTH = 8


def register_secret_value(value: str) -> None:
    """Register a literal secret so it is scrubbed wherever it appears."""
    if value and len(value) >= _MIN_SCRUB_LENGTH:
        _known_secret_values.add(value)


def clear_registered_secrets() -> None:
    _known_secret_values.clear()


def _is_sensitive_key(key: str) -> bool:
    lowered = key.lower()
    return any(part in lowered for part in _SENSITIVE_KEY_PARTS)


def scrub_text(text: str) -> str:
    """Remove any registered secret value or credential-shaped substring."""
    if not text:
        return text
    for value in _known_secret_values:
        if value in text:
            text = text.replace(value, REDACTED)
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(REDACTED, text)
    return text


def _scrub_value(value: Any, depth: int = 0) -> Any:
    if depth > 6:  # guard against pathological nesting
        return value
    if isinstance(value, str):
        return scrub_text(value)
    if isinstance(value, dict):
        return {
            k: (REDACTED if _is_sensitive_key(str(k)) else _scrub_value(v, depth + 1))
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        scrubbed = [_scrub_value(v, depth + 1) for v in value]
        return type(value)(scrubbed) if isinstance(value, tuple) else scrubbed
    return value


def redaction_processor(
    _logger: Any, _name: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    for key in list(event_dict.keys()):
        if _is_sensitive_key(str(key)):
            event_dict[key] = REDACTED
        else:
            event_dict[key] = _scrub_value(event_dict[key])
    return event_dict


def context_processor(
    _logger: Any, _name: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    for key, var in _CONTEXT_VARS.items():
        value = var.get()
        if value is not None and key not in event_dict:
            event_dict[key] = value
    return event_dict


# ── setup ────────────────────────────────────────────────────────────────────


def configure_logging(level: str = "INFO", fmt: str = "console") -> None:
    """Idempotent — safe to call from both the app factory and tests."""
    renderer: Any = (
        structlog.processors.JSONRenderer()
        if fmt == "json"
        else structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            context_processor,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            redaction_processor,  # last, so it sees fully-rendered values
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelNamesMapping().get(level.upper(), logging.INFO)
        ),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=True,
    )

    logging.basicConfig(
        format="%(message)s", stream=sys.stderr, level=level.upper(), force=True
    )
    for noisy in ("uvicorn.access", "httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str) -> structlog.BoundLogger:
    return structlog.get_logger(name)


class timed:
    """Context manager measuring wall-clock duration in milliseconds.

    Used by the pipeline and tool executor so every stage reports a duration
    without each one reimplementing the arithmetic.
    """

    __slots__ = ("_start", "duration_ms")

    def __enter__(self) -> "timed":
        import time

        self._start = time.perf_counter()
        self.duration_ms = 0.0
        return self

    def __exit__(self, *exc: object) -> None:
        import time

        self.duration_ms = (time.perf_counter() - self._start) * 1000.0


def iter_registered_secrets() -> Iterator[str]:  # pragma: no cover - test aid
    yield from _known_secret_values
