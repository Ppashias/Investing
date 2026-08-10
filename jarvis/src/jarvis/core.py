"""Application container.

Owns the objects that live for the process: database, provider registry, tool
registry, router, activity bus, orchestrator. Constructed once at startup and
handed to request handlers via dependency injection, rather than living in
module-level globals — which is what makes it possible to stand up a fully
independent JARVIS inside a test.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from jarvis.activity.service import ActivityBus
from jarvis.config import Settings, get_settings
from jarvis.context.manager import ContextBudget
from jarvis.db.base import Database
from jarvis.db.models import User
from jarvis.logging import configure_logging, get_logger
from jarvis.orchestrator.core import Orchestrator
from jarvis.permissions.engine import seed_default_grants
from jarvis.providers.embeddings import EmbeddingProvider, build_embedding_provider
from jarvis.providers.registry import ProviderRegistry, build_registry
from jarvis.providers.retry import RetryPolicy
from jarvis.providers.router import ModelRouter
from jarvis.tools.registry import ToolRegistry, build_default_registry

log = get_logger(__name__)

DEFAULT_USER_NAME = "operator"


@dataclass
class JarvisCore:
    settings: Settings
    database: Database
    providers: ProviderRegistry
    tools: ToolRegistry
    router: ModelRouter
    activity_bus: ActivityBus
    orchestrator: Orchestrator
    embeddings: EmbeddingProvider

    # ── lifecycle ────────────────────────────────────────────────────────────

    @classmethod
    def build(cls, settings: Settings | None = None) -> "JarvisCore":
        settings = settings or get_settings()
        configure_logging(settings.log_level, settings.log_format)

        database = Database(settings.resolved_database_url)
        providers = build_registry(settings)
        tools = build_default_registry()
        router = ModelRouter(providers, settings)
        bus = ActivityBus()
        embeddings = build_embedding_provider(settings)

        budget = ContextBudget(
            max_memories=settings.memory_max_injected,
            max_memory_chars=settings.memory_max_chars,
            max_knowledge=settings.knowledge_max_injected,
            max_knowledge_chars=settings.knowledge_max_chars,
        )

        orchestrator = Orchestrator(
            registry=tools,
            router=router,
            activity_bus=bus,
            retry=RetryPolicy(max_attempts=settings.provider_max_retries),
            tool_timeout_seconds=settings.tool_timeout_seconds,
            max_iterations=settings.max_agent_iterations,
            confirmation_ttl_seconds=settings.confirmation_ttl_seconds,
            embeddings=embeddings,
            context_budget=budget,
            memory_enabled=settings.memory_enabled,
            knowledge_enabled=settings.knowledge_enabled,
            memory_capture_mode=settings.memory_capture_mode,
            memory_min_importance=settings.memory_autostore_min_importance,
            memory_duplicate_threshold=settings.memory_duplicate_threshold,
        )

        return cls(
            settings=settings,
            database=database,
            providers=providers,
            tools=tools,
            router=router,
            activity_bus=bus,
            orchestrator=orchestrator,
            embeddings=embeddings,
        )

    async def startup(self, *, create_schema: bool = False) -> None:
        """Prepare the process for serving.

        ``create_schema`` is for tests and first-run convenience. Production
        applies Alembic migrations instead, so that schema changes are
        versioned rather than implicit.
        """
        if create_schema:
            await self.database.create_all()

        async with self.database.session_factory() as session:
            await self.tools.sync_to_db(session)
            user = await self.ensure_default_user(session)
            await seed_default_grants(session, user.id)
            await session.commit()

        log.info(
            "jarvis_started",
            environment=self.settings.environment,
            providers_configured=[p.key for p in self.providers.configured()],
            tools=len(self.tools.all()),
            auth_required=self.settings.require_auth,
        )

    async def shutdown(self) -> None:
        await self.providers.aclose()
        await self.database.dispose()
        log.info("jarvis_stopped")

    # ── helpers ──────────────────────────────────────────────────────────────

    @staticmethod
    async def ensure_default_user(session: AsyncSession) -> User:
        """JARVIS is single-user, but authorisation still needs a subject.

        Phase 1 resolves that subject implicitly. When real authentication
        arrives, this becomes a lookup from the session rather than a
        first-row fetch — callers already take the ``User`` as a parameter, so
        nothing downstream changes.
        """
        user = (
            await session.execute(select(User).order_by(User.created_at.asc()).limit(1))
        ).scalars().first()
        if user is None:
            user = User(name=DEFAULT_USER_NAME, display_name="Operator")
            session.add(user)
            await session.flush()
            log.info("default_user_created", user_id=user.id)
        return user

    def status(self) -> dict[str, object]:
        return {
            "status": "ok",
            "phase": 1,
            "settings": self.settings.public_dict(),
            "providers": self.providers.describe(),
            "tools": {
                "count": len(self.tools.all()),
                "categories": self.tools.categories(),
                "names": [t.name for t in self.tools.all()],
            },
            "activity_subscribers": self.activity_bus.subscriber_count,
            "embeddings": {
                "provider": self.embeddings.info.key,
                "model": self.embeddings.info.model,
                "dimensions": self.embeddings.info.dim,
                # The important field. False means retrieval matches wording,
                # not meaning, and every surface says so rather than letting
                # the user infer semantic search from the word "embeddings".
                "semantic": self.embeddings.info.semantic,
                "description": self.embeddings.info.description,
            },
        }
