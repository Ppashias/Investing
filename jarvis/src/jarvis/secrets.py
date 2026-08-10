"""Secret resolution.

Credentials are never read directly from settings anywhere in JARVIS. They are
resolved through a :class:`SecretsProvider` so that the storage backend can
change without touching call sites — which matters because the audit's target
deployment (Windows desktop) should use the OS keychain, while development and
containers use environment variables.

Resolution order is a chain: the first backend that returns a value wins. The
default chain is ``env -> keyring``, so an explicit environment variable always
overrides a stored credential. That ordering is deliberate: it makes it
possible to run a one-off with a different key without disturbing the keychain.

Nothing here logs a secret value, and :class:`Secret` is deliberately awkward
to print.
"""

from __future__ import annotations

import os
from typing import Protocol, runtime_checkable

import structlog

log = structlog.get_logger(__name__)

_REDACTED = "***redacted***"


class Secret:
    """A string that resists being printed by accident.

    ``repr``/``str`` are redacted; the value comes out only via
    :meth:`reveal`, which makes secret use greppable in review.
    """

    __slots__ = ("_value", "_name")

    def __init__(self, value: str, *, name: str = "secret") -> None:
        self._value = value
        self._name = name

    def reveal(self) -> str:
        return self._value

    @property
    def name(self) -> str:
        return self._name

    def __bool__(self) -> bool:
        return bool(self._value)

    def __len__(self) -> int:
        # Length is safe to expose and useful for "is this plausibly a key".
        return len(self._value)

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return f"Secret({self._name}={_REDACTED})"

    __str__ = __repr__


@runtime_checkable
class SecretsProvider(Protocol):
    """Anything that can resolve a named credential."""

    name: str

    def get(self, key: str) -> Secret | None: ...


class EnvSecretsProvider:
    """Reads from process environment. Always available."""

    name = "env"

    def get(self, key: str) -> Secret | None:
        raw = os.environ.get(key)
        if raw is None:
            return None
        raw = raw.strip()
        return Secret(raw, name=key) if raw else None


class KeyringSecretsProvider:
    """Reads from the OS keychain.

    On Windows this is the Credential Manager, with the stored blob bound to
    the user account via DPAPI — which is the recommended resting place for
    live credentials on the target platform. Import and backend availability
    are both probed lazily, because headless Linux has no SecretService and we
    must degrade rather than crash.
    """

    name = "keyring"

    def __init__(self, service: str = "jarvis") -> None:
        self.service = service
        self._backend_ok: bool | None = None

    def _available(self) -> bool:
        if self._backend_ok is not None:
            return self._backend_ok
        try:
            import keyring
            from keyring.backends.fail import Keyring as FailKeyring

            self._backend_ok = not isinstance(keyring.get_keyring(), FailKeyring)
        except Exception:
            self._backend_ok = False
        if not self._backend_ok:
            log.debug("keyring_unavailable", service=self.service)
        return self._backend_ok

    def get(self, key: str) -> Secret | None:
        if not self._available():
            return None
        try:
            import keyring

            raw = keyring.get_password(self.service, key)
        except Exception as exc:  # backend can fail at call time too
            log.warning("keyring_lookup_failed", key=key, error=str(exc))
            return None
        return Secret(raw, name=key) if raw else None


class ChainSecretsProvider:
    """First backend to return a value wins."""

    name = "chain"

    def __init__(self, providers: list[SecretsProvider]) -> None:
        self.providers = providers

    def get(self, key: str) -> Secret | None:
        for provider in self.providers:
            value = provider.get(key)
            if value is not None:
                log.debug("secret_resolved", key=key, backend=provider.name)
                return value
        return None

    def describe(self) -> list[str]:
        return [p.name for p in self.providers]


def default_secrets_provider(*, service: str = "jarvis") -> ChainSecretsProvider:
    return ChainSecretsProvider([EnvSecretsProvider(), KeyringSecretsProvider(service)])
