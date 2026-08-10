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

    # ── embeddings ───────────────────────────────────────────────────────────
    #: Endpoint speaking the OpenAI ``/v1/embeddings`` format. Falls back to
    #: ``openai_base_url``. With neither set, retrieval degrades to a local
    #: lexical vectoriser — functional, but unable to match paraphrases, and
    #: reported as such by ``/api/system/status``.
    embedding_base_url: str | None = None
    embedding_api_key_name: str = "EMBEDDING_API_KEY"
    embedding_model: str = "text-embedding-3-small"
    embedding_dim: int = 1536

    # ── memory ───────────────────────────────────────────────────────────────
    #: Memory retrieval is on by default; turning it off leaves the rest of the
    #: system working, which is what makes it debuggable.
    memory_enabled: bool = True
    #: How many memories may reach the model in one request. The cap is the
    #: point — §19 forbids injecting the whole store.
    memory_max_injected: int = 8
    memory_max_chars: int = 6_000
    #: Cosine similarity below which a candidate is not worth considering.
    memory_min_similarity: float = 0.25
    #: Similarity at or above which two memories on the same subject are
    #: treated as the same memory rather than two (§15).
    memory_duplicate_threshold: float = 0.87
    #: Importance below which nothing is stored without being asked for.
    memory_autostore_min_importance: float = 0.45
    #: ``ask`` proposes and waits (§14); ``auto`` stores directly; ``off``
    #: stores only what the user explicitly asks to be remembered.
    memory_capture_mode: Literal["ask", "auto", "off"] = "ask"
    #: Lifetime of WORKING-scope memories attached to a task or session.
    working_memory_ttl_seconds: int = 86_400

    # ── knowledge ────────────────────────────────────────────────────────────
    knowledge_enabled: bool = True
    knowledge_max_injected: int = 6
    knowledge_max_chars: int = 8_000
    #: Ceiling on a single ingested file. Chunking a 500 MB log helps nobody.
    ingest_max_bytes: int = 25 * 1024 * 1024
    ingest_chunk_target_chars: int = 1_400
    ingest_chunk_overlap_chars: int = 160
    #: Directories that may be ingested from. Empty means "nothing", which is
    #: the safe default: ingestion reads arbitrary files, so the allow-list is
    #: the boundary that keeps it from reading ``~/.ssh``.
    knowledge_roots: list[Path] = Field(default_factory=list)

    # ── Obsidian (Phase 2.5) ─────────────────────────────────────────────────
    #: Optional bootstrap vault. The live configuration lives on the
    #: ``knowledge_sources`` row so it can be changed from the UI without a
    #: restart; this only seeds it on first run, which is what makes a
    #: headless or scripted deployment possible.
    obsidian_vault_path: Path | None = None
    obsidian_vault_name: str | None = None
    #: Both default off. Reading someone's notes and rewriting them are
    #: different permissions, and an integration that can write by default is
    #: one bad model turn away from an edit nobody asked for.
    obsidian_allow_writes: bool = False
    obsidian_allow_deletes: bool = False
    #: Notes pulled per sync. A ceiling rather than a target — the sync is
    #: incremental, so the usual run touches a handful.
    obsidian_sync_limit: int = 5_000

    # ── computer control (Phase 3) ───────────────────────────────────────────
    #: Master switch. Off means the service still reports *why* nothing works,
    #: rather than the endpoints disappearing.
    computer_enabled: bool = True
    #: Display to drive. Unset uses ``DISPLAY`` from the environment.
    computer_display: str | None = None
    #: Create an Xvfb display when none exists. Opt-in: it means JARVIS can
    #: launch and drive GUI applications on a headless machine, which is a
    #: capability the operator should choose deliberately.
    computer_virtual_display: bool = False
    computer_virtual_width: int = 1280
    computer_virtual_height: int = 800

    #: Directories JARVIS may read, and — separately — write and delete in.
    #: Empty means no filesystem access at all.
    computer_file_roots: list[Path] = Field(default_factory=list)
    computer_write_files: bool = False
    computer_delete_files: bool = False
    #: Working directory for commands. Defaults to the first file root.
    computer_working_directory: Path | None = None

    #: How long a screenshot stays in memory. Never written to disk unless
    #: retention is switched on (§6).
    computer_screenshot_ttl_seconds: int = 300
    computer_screenshot_retain: bool = False

    computer_max_steps: int = 25
    computer_task_timeout_seconds: float = 300.0

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
            "memory": {
                "enabled": self.memory_enabled,
                "capture_mode": self.memory_capture_mode,
                "max_injected": self.memory_max_injected,
            },
            "knowledge": {
                "enabled": self.knowledge_enabled,
                "max_injected": self.knowledge_max_injected,
                "roots_configured": len(self.knowledge_roots),
            },
            "obsidian": {
                # Whether a path is *configured*, never the path itself: this
                # dict goes over the API and a home directory layout is
                # personal information JARVIS has no reason to broadcast.
                "bootstrap_configured": self.obsidian_vault_path is not None,
                "allow_writes": self.obsidian_allow_writes,
                "allow_deletes": self.obsidian_allow_deletes,
            },
            "computer": {
                "enabled": self.computer_enabled,
                "virtual_display": self.computer_virtual_display,
                "file_roots_configured": len(self.computer_file_roots),
                "write_files": self.computer_write_files,
                "delete_files": self.computer_delete_files,
                "screenshot_retention": self.computer_screenshot_retain,
            },
        }

    @field_validator("knowledge_roots", "computer_file_roots", mode="after")
    @classmethod
    def _resolve_roots(cls, v: list[Path]) -> list[Path]:
        """Resolved once, here, so path containment checks downstream compare
        two absolute paths and cannot be defeated by ``..``."""
        return [p.expanduser().resolve() for p in v]


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
