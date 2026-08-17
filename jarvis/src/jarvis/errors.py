"""Error taxonomy for JARVIS.

Every failure mode the core can produce has a type here. The point is that
callers — and especially the orchestrator's error stage — can branch on
*category* rather than on string matching, and that the API layer can map any
error to a status code without knowing what raised it.

Two properties travel with every error:

``retryable``    whether re-attempting the identical operation could plausibly
                 succeed. Drives the provider retry loop and, later, task
                 re-execution.
``user_message`` what JARVIS should say. Never contains internals, never
                 contains anything drawn from a secret.
"""

from __future__ import annotations

from typing import Any


class JarvisError(Exception):
    """Base class. Carries an operator-facing message and a user-facing one."""

    code: str = "jarvis_error"
    http_status: int = 500
    retryable: bool = False
    default_user_message = "Something went wrong on my side."

    def __init__(
        self,
        message: str,
        *,
        user_message: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.user_message = user_message or self.default_user_message
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        """Safe for the API surface — deliberately omits ``message``.

        ``message`` is for logs. It can contain paths, model names, and other
        internals we do not want to hand to a client.
        """
        return {
            "code": self.code,
            "message": self.user_message,
            "retryable": self.retryable,
            "details": self.details,
        }


# ── configuration and startup ────────────────────────────────────────────────


class ConfigurationError(JarvisError):
    code = "configuration_error"
    http_status = 500
    default_user_message = "I am not configured correctly. Check the server configuration."


class ProviderNotConfiguredError(ConfigurationError):
    code = "provider_not_configured"
    default_user_message = (
        "No AI provider is configured. Set an API key and restart me."
    )


# ── request handling ─────────────────────────────────────────────────────────


class ValidationError(JarvisError):
    code = "validation_error"
    http_status = 422
    default_user_message = "That request was not something I could read."


class NotFoundError(JarvisError):
    code = "not_found"
    http_status = 404
    default_user_message = "I could not find that."


class AuthenticationError(JarvisError):
    code = "authentication_error"
    http_status = 401
    default_user_message = "Authentication failed."


# ── permissions and confirmation ─────────────────────────────────────────────


class PermissionDeniedError(JarvisError):
    code = "permission_denied"
    http_status = 403
    default_user_message = "I am not permitted to do that."

    def __init__(
        self,
        message: str,
        *,
        capability: str | None = None,
        tool: str | None = None,
        reason: str | None = None,
        **kwargs: Any,
    ) -> None:
        details = kwargs.pop("details", {}) or {}
        details.update(
            {k: v for k, v in
             {"capability": capability, "tool": tool, "reason": reason}.items()
             if v is not None}
        )
        super().__init__(message, details=details, **kwargs)


class ConfirmationRequiredError(JarvisError):
    """Not a failure. Execution is suspended pending a human decision."""

    code = "confirmation_required"
    http_status = 202
    default_user_message = "I need you to confirm this before I continue."

    def __init__(self, message: str, *, confirmation_id: str, **kwargs: Any) -> None:
        details = kwargs.pop("details", {}) or {}
        details["confirmation_id"] = confirmation_id
        super().__init__(message, details=details, **kwargs)
        self.confirmation_id = confirmation_id


class ConfirmationDeniedError(JarvisError):
    code = "confirmation_denied"
    http_status = 403
    default_user_message = "Understood — I have not done that."


# ── tools ────────────────────────────────────────────────────────────────────


class ToolError(JarvisError):
    code = "tool_error"
    http_status = 500
    default_user_message = "A tool failed while I was working."


class ToolNotFoundError(ToolError):
    code = "tool_not_found"
    http_status = 404
    default_user_message = "I tried to use a tool that does not exist."


class ToolInputError(ToolError):
    """The model produced arguments that do not satisfy the tool's schema.

    Recoverable in the agent loop: the error text is fed back so the model can
    correct itself, which is why this is retryable.
    """

    code = "tool_input_error"
    http_status = 422
    retryable = True
    default_user_message = "I called a tool incorrectly and am retrying."


class ToolExecutionError(ToolError):
    code = "tool_execution_error"


class ToolTimeoutError(ToolError):
    code = "tool_timeout"
    http_status = 504
    retryable = True
    default_user_message = "A tool took too long and I stopped it."


# ── providers ────────────────────────────────────────────────────────────────


class ProviderError(JarvisError):
    code = "provider_error"
    http_status = 502
    default_user_message = "The AI provider had a problem."


class ProviderAuthError(ProviderError):
    code = "provider_auth_error"
    http_status = 502
    default_user_message = "My AI provider rejected the credentials."


class ProviderRateLimitError(ProviderError):
    code = "provider_rate_limit"
    http_status = 429
    retryable = True
    default_user_message = "I am being rate limited. Retrying shortly."

    def __init__(self, message: str, *, retry_after: float | None = None, **kwargs: Any) -> None:
        details = kwargs.pop("details", {}) or {}
        if retry_after is not None:
            details["retry_after"] = retry_after
        super().__init__(message, details=details, **kwargs)
        self.retry_after = retry_after


class ProviderTimeoutError(ProviderError):
    code = "provider_timeout"
    http_status = 504
    retryable = True
    default_user_message = "The AI provider timed out. Retrying."


class ProviderOverloadedError(ProviderError):
    code = "provider_overloaded"
    http_status = 503
    retryable = True
    default_user_message = "The AI provider is overloaded. Retrying."


class ProviderResponseError(ProviderError):
    """A 200 that we could not use — malformed or unusable content."""

    code = "provider_response_error"
    default_user_message = "I got a response I could not interpret."


class ProviderRefusalError(ProviderError):
    """The model declined the request on policy grounds."""

    code = "provider_refusal"
    http_status = 200
    default_user_message = "I am not able to help with that."


class NoEligibleProviderError(ProviderError):
    """Routing found no provider satisfying the task's capability requirements."""

    code = "no_eligible_provider"
    http_status = 503
    default_user_message = "No configured AI provider can handle that kind of work."


# ── execution control ────────────────────────────────────────────────────────


class CancelledError(JarvisError):
    code = "cancelled"
    http_status = 499
    default_user_message = "Stopped."


class TaskError(JarvisError):
    code = "task_error"
    default_user_message = "Something went wrong with that task."


class InvalidStateTransitionError(TaskError):
    code = "invalid_state_transition"
    http_status = 409
    default_user_message = "That task cannot move to that state from where it is."
