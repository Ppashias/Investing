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
from pathlib import Path
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


class DotEnvSecretsProvider:
    """Reads from the ``.env`` file the settings are loaded from.

    Without this, a credential in ``.env`` is invisible. ``Settings`` reads the
    file through pydantic-settings, but that populates *setting fields* — it
    does not export anything into ``os.environ``, and every credential is
    fetched by name through this module rather than from a settings field. So
    ``JARVIS_API_TOKEN=…`` in ``.env`` resolved to nothing, while the README
    told people to put it there. That gap survived because the development
    environment always exported the variable in the shell.

    The file is re-read on each miss rather than cached: it is a handful of
    lines, credentials are fetched rarely, and caching would mean editing
    ``.env`` had no effect until a restart — which is precisely the confusing
    behaviour this exists to remove.
    """

    name = "dotenv"

    def __init__(self, path: "Path | str | None" = None) -> None:
        self._explicit = Path(path) if path else None

    def _path(self) -> "Path | None":
        if self._explicit is not None:
            return self._explicit
        # Imported lazily: config imports this module, so a module-level import
        # of config here would be circular.
        from jarvis.config import REPO_ROOT

        candidate = Path(os.environ.get("JARVIS_ENV_FILE", REPO_ROOT / ".env"))
        return candidate if candidate.is_file() else None

    def get(self, key: str) -> Secret | None:
        path = self._path()
        if path is None:
            return None
        try:
            # utf-8-sig, not utf-8: Windows PowerShell writes UTF-8 *with* a
            # BOM, and the BOM would otherwise become part of the first key's
            # name — making exactly the first credential in the file unreadable.
            text = path.read_text(encoding="utf-8-sig")
        except OSError:
            return None

        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, _, raw = line.partition("=")
            if name.strip() != key:
                continue
            raw = raw.strip()
            # Strip one layer of matching quotes, the way dotenv readers do.
            if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
                raw = raw[1:-1]
            return Secret(raw, name=key) if raw else None
        return None


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
    """Environment, then ``.env``, then the OS keychain.

    That order is the one the README documents: "the environment is checked
    first, so a value in .env overrides the keychain". A shell export stays the
    quickest way to override anything, ``.env`` is where a local setup puts
    things, and the keychain is the right resting place for a live credential.
    """
    return ChainSecretsProvider(
        [
            EnvSecretsProvider(),
            DotEnvSecretsProvider(),
            KeyringSecretsProvider(service),
        ]
    )
