"""Runtime configuration.

Settings hold *non-secret* configuration only. Credentials are resolved
separately through :mod:`jarvis.secrets` and are never attributes of this
object, so dumping settings — into a log, an error, or the ``/system/status``
endpoint — cannot leak a key.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from jarvis.secrets import ChainSecretsProvider, Secret, default_secrets_provider

REPO_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="JARVIS_",
        env_file=os.environ.get("JARVIS_ENV_FILE", str(REPO_ROOT / ".env")),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── identity / deployment ────────────────────────────────────────────────
    environment: Literal["development", "production", "test"] = "development"
    data_dir: Path = Field(default=REPO_ROOT / "data")

    # ── server ───────────────────────────────────────────────────────────────
    host: str = "127.0.0.1"
    port: int = 8787
    #: Browser origins permitted to call the API. Loopback only by default —
    #: JARVIS binds to localhost and is not intended to be internet-facing.
    cors_origins: list[str] = Field(
        default_factory=lambda: [
            "http://127.0.0.1:8787",
            "http://localhost:8787",
            "http://127.0.0.1:5173",
            "http://localhost:5173",
        ]
    )

    # ── database ─────────────────────────────────────────────────────────────
    #: Left unset, derived from ``data_dir`` so there is one place to relocate
    #: state. Async driver because the whole request path is async.
    database_url: str | None = None

    # ── logging ──────────────────────────────────────────────────────────────
    log_level: str = "INFO"
    log_format: Literal["console", "json"] = "console"

    # ── providers ────────────────────────────────────────────────────────────
    default_provider: str = "anthropic"
    #: Model assignments per task class. The router reads these; see
    #: :mod:`jarvis.providers.router`. Overridable without code changes.
    model_reasoning: str = "claude-opus-5"
    model_conversation: str = "claude-sonnet-5"
    model_fast: str = "claude-haiku-4-5"

    #: Environment-variable / keychain *names* — not values.
    anthropic_api_key_name: str = "ANTHROPIC_API_KEY"
    openai_api_key_name: str = "OPENAI_API_KEY"

    #: Base URL for the OpenAI-compatible provider. Any of Ollama, llama.cpp,
    #: LM Studio, or vLLM works here; only the URL changes.
    openai_base_url: str | None = None
    openai_compat_models: list[str] = Field(default_factory=list)

    provider_timeout_seconds: float = 120.0
    provider_max_retries: int = 3

    # ── API auth ─────────────────────────────────────────────────────────────
    #: When set, every non-health request must present this token. JARVIS binds
    #: to loopback, but loopback is shared with every other process on the
    #: machine, so a token is still the difference between "my agent" and "any
    #: local program that can reach port 8787".
    api_token_name: str = "JARVIS_API_TOKEN"
    require_auth: bool = True

    # ── execution limits ─────────────────────────────────────────────────────
    tool_timeout_seconds: float = 30.0
    max_agent_iterations: int = 8
    confirmation_ttl_seconds: int = 900

    @field_validator("data_dir", mode="after")
    @classmethod
    def _resolve_data_dir(cls, v: Path) -> Path:
        return v.resolve()

    @property
    def resolved_database_url(self) -> str:
        if self.database_url:
            return self.database_url
        return f"sqlite+aiosqlite:///{self.data_dir / 'jarvis.db'}"

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)

    def public_dict(self) -> dict[str, object]:
        """Safe to return over the API — contains no credentials by construction."""
        return {
            "environment": self.environment,
            "host": self.host,
            "port": self.port,
            "default_provider": self.default_provider,
            "models": {
                "reasoning": self.model_reasoning,
                "conversation": self.model_conversation,
                "fast": self.model_fast,
            },
            "auth_required": self.require_auth,
            "tool_timeout_seconds": self.tool_timeout_seconds,
            "max_agent_iterations": self.max_agent_iterations,
        }


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    settings = Settings()
    settings.ensure_dirs()
    return settings


@lru_cache(maxsize=1)
def get_secrets() -> ChainSecretsProvider:
    return default_secrets_provider()


def get_secret(key: str) -> Secret | None:
    return get_secrets().get(key)


def reset_config_caches() -> None:
    """Test hook — settings and secrets are cached for the process lifetime."""
    get_settings.cache_clear()
    get_secrets.cache_clear()
