"""Application container.

Owns the objects that live for the process: database, provider registry, tool
registry, router, activity bus, orchestrator. Constructed once at startup and
handed to request handlers via dependency injection, rather than living in
module-level globals — which is what makes it possible to stand up a fully
independent JARVIS inside a test.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from jarvis.agents.background import BackgroundRunner
from jarvis.activity.service import ActivityBus
from jarvis.browser import BrowserService, BrowserSettings
from jarvis.config import Settings, get_settings
from jarvis.computer.service import ComputerService, ComputerSettings
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
    computer: ComputerService
    #: Phase 4. Constructed at build time but deliberately not started: no
    #: Chromium process exists until something actually asks to browse.
    browser: BrowserService
    #: Phase D. Process-wide, because a job registry with per-request scope
    #: tracks nothing — the same reason the emergency stop lives on the
    #: computer service rather than being rebuilt each turn.
    #:
    #: Defaulted so a hand-assembled core (every test that builds one) keeps
    #: working: an empty runner is a correct runner, and requiring it would
    #: have made "does this build have background work?" a constructor detail
    #: rather than a capability question.
    background: BackgroundRunner = field(default_factory=BackgroundRunner)

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

        computer = ComputerService(
            ComputerSettings(
                enabled=settings.computer_enabled,
                display=settings.computer_display,
                use_virtual_display=settings.computer_virtual_display,
                virtual_width=settings.computer_virtual_width,
                virtual_height=settings.computer_virtual_height,
                file_roots=list(settings.computer_file_roots),
                can_write_files=settings.computer_write_files,
                can_delete_files=settings.computer_delete_files,
                working_directory=settings.computer_working_directory,
                screenshot_ttl_seconds=settings.computer_screenshot_ttl_seconds,
                screenshot_retain=settings.computer_screenshot_retain,
                screenshot_dir=settings.data_dir / "screenshots",
                max_steps=settings.computer_max_steps,
                task_timeout_seconds=settings.computer_task_timeout_seconds,
            ),
            router=router,
            activity_bus=bus,
        )

        # Constructed, not started. ``BrowserService.__init__`` does no I/O and
        # launches nothing; the first Chromium process appears on first use.
        browser = BrowserService(
            BrowserSettings(
                enabled=settings.browser_enabled,
                executable_path=settings.browser_executable_path,
                headless=settings.browser_headless,
                launch_timeout_seconds=settings.browser_launch_timeout_seconds,
                navigation_timeout_seconds=settings.browser_navigation_timeout_seconds,
                max_pages=settings.browser_max_pages,
                storage_dir=settings.browser_storage_dir,
                allow_localhost=settings.browser_allow_localhost,
                allow_private_networks=settings.browser_allow_private_networks,
                launch_args=tuple(settings.browser_launch_args),
            ),
            activity_bus=bus,
        )

        # Process-wide, like the emergency stop and for the same reason: a
        # registry with per-request scope tracks nothing. The runner reaches
        # the same stop object, so "stop everything" includes work nobody is
        # watching.
        background = BackgroundRunner(
            activity_factory=None,
            emergency_stop=computer.emergency_stop,
        )

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
            computer=computer,
            browser=browser,
            background=background,
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
            computer=computer,
            browser=browser,
            background=background,
        )

    async def startup(self, *, create_schema: bool = False) -> None:
        """Prepare the process for serving.

        ``create_schema`` is for tests and first-run convenience. Production
        applies Alembic migrations instead, so that schema changes are
        versioned rather than implicit.
        """
        if create_schema:
            await self.database.create_all()

        # Probed before the first request so /api/computer/status can answer
        # "why can't JARVIS see my screen?" immediately rather than after a
        # failed action.
        self.computer.start()

        async with self.database.session_factory() as session:
            await self.tools.sync_to_db(session)
            user = await self.ensure_default_user(session)
            await seed_default_grants(session, user.id)
            await self._bootstrap_obsidian(session, user.id)
            # The router is built before the database is readable, so a model
            # the user chose in the console is stored but not in force until
            # this runs. Without it the preference would appear to survive a
            # restart in the UI while every turn quietly used the .env default.
            from jarvis.providers.preferences import apply_to, stored

            apply_to(self.router, stored(user))
            await session.commit()

        log.info(
            "jarvis_started",
            environment=self.settings.environment,
            providers_configured=[p.key for p in self.providers.configured()],
            tools=len(self.tools.all()),
            auth_required=self.settings.require_auth,
        )

    async def shutdown(self) -> None:
        self.computer.shutdown()
        # Before the providers and the database, and unconditionally: if a
        # browser was launched it is a real OS process, and a JARVIS that exits
        # without closing it leaves Chromium running with nobody owning it.
        # A no-op when nothing launched, which is the usual case.
        await self.browser.shutdown()
        await self.providers.aclose()
        await self.database.dispose()
        log.info("jarvis_stopped")

    # ── helpers ──────────────────────────────────────────────────────────────

    async def _bootstrap_obsidian(self, session: AsyncSession, user_id: str) -> None:
        """Seed the vault connection from configuration, once.

        Only when nothing is configured yet: the live configuration lives on
        the ``knowledge_sources`` row so it can be changed from the UI, and a
        setting that reasserted itself on every restart would silently undo the
        user's choice to disconnect.

        A failure is logged, not raised. A vault that has moved is a reason for
        the panel to read DISCONNECTED, not a reason for the daemon to refuse
        to start.
        """
        if self.settings.obsidian_vault_path is None:
            return

        from jarvis.knowledge.providers.obsidian import ObsidianService

        service = ObsidianService(session, user_id)
        if await service.row() is not None:
            # Already connected. The *connection* is not reasserted — doing so
            # would undo a disconnect on every restart. The *permissions* are,
            # because a configuration file that says JARVIS may write is the
            # operator's instruction, and a setup script reporting "JARVIS may
            # create and update notes" while the running system refuses would
            # be a lie. Only reconciled when the vault path is configured here
            # too: someone managing the connection from the panel is not also
            # managing it from a file, and their choices should stand.
            await service.set_permissions(
                allow_writes=self.settings.obsidian_allow_writes,
                allow_deletes=self.settings.obsidian_allow_deletes,
            )
            return

        try:
            await service.connect(
                str(self.settings.obsidian_vault_path),
                vault_name=self.settings.obsidian_vault_name,
                allow_writes=self.settings.obsidian_allow_writes,
                allow_deletes=self.settings.obsidian_allow_deletes,
            )
        except Exception as exc:
            log.warning("obsidian_bootstrap_failed", error=str(exc))

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

    def subsystems(self) -> list[dict[str, object]]:
        """What is actually usable right now, and why anything is not.

        Replaces a hardcoded "NOT IMPLEMENTED" list in the console that had
        gone comprehensively stale: it still named memory, file access,
        computer control, browser control and agents as unbuilt, months after
        each shipped. A UI telling somebody a feature is missing when they have
        it is worse than saying nothing — they will not go looking for it.

        The lesson from that is not "correct the list". It is that a
        second, hand-written statement of what exists will always drift from
        the thing it describes. So this is computed from the same objects the
        subsystems themselves consult, and the console renders whatever it
        returns.

        The distinction it draws is the one the stale list could not: *built*
        and *available here* are different questions. Browser control is built;
        it is unavailable without Playwright installed. Saying "unavailable —
        pip install jarvis[browser]" is useful, and "not implemented" is false.

        There are three states rather than two, and the third is load-bearing.
        The browser is probed lazily — the probe starts a Playwright driver
        process, and paying for that on every start would be a real cost for a
        capability most turns never use. Until something asks, the honest
        answer is ``unknown``, not ``unavailable``: reporting a refusal for
        something nobody has looked at is the same overclaim as the stale list,
        pointed the other way.
        """
        computer_reason = None
        if not self.computer.settings.enabled:
            computer_reason = "Switched off (JARVIS_COMPUTER_ENABLED)."
        elif not self.computer.capabilities.display:
            from jarvis.computer.types import ActionKind

            computer_reason = self.computer.capabilities.reason_unavailable(
                ActionKind.CLICK
            )

        from jarvis.browser.capabilities import BrowserAvailability

        browser = self.browser.capabilities
        if browser.available:
            browser_state, browser_detail = "ready", None
        elif browser.state is BrowserAvailability.UNPROBED:
            browser_state = "unknown"
            browser_detail = "Not checked yet — probed the first time it is used."
        else:
            browser_state, browser_detail = "unavailable", browser.reason

        embeddings = self.embeddings.info
        configured = bool(self.providers.configured())
        vault = self.settings.obsidian_vault_path

        return [
            {
                "name": "Conversation",
                "state": "ready" if configured else "unavailable",
                "detail": None if configured else
                          "No AI provider is configured. Set ANTHROPIC_API_KEY.",
            },
            {
                "name": "Tasks & tools",
                "state": "ready",
                "detail": f"{len(self.tools.all())} tools registered.",
            },
            {
                "name": "Memory",
                # Ready either way — the caveat is about the *quality* of
                # recall, not whether it works. Reporting it unavailable
                # because embeddings are absent would hide a subsystem the user
                # can use today, which is the same overclaim in reverse.
                "state": "ready",
                "detail": None if embeddings.semantic else
                          "Search is lexical: it matches wording, not meaning.",
            },
            {
                "name": "Knowledge base",
                "state": "ready",
                "detail": None if self.settings.knowledge_roots else
                          "Uploads only — no directories approved for ingestion.",
            },
            {
                "name": "Obsidian",
                "state": "ready" if vault else "unavailable",
                "detail": None if vault else "No vault connected.",
            },
            {
                "name": "Computer control",
                "state": "ready" if computer_reason is None else "unavailable",
                "detail": computer_reason,
            },
            {
                "name": "Browser control",
                "state": browser_state,
                "detail": browser_detail,
            },
            {
                "name": "Agents & background work",
                "state": "ready",
                "detail": f"{len(self.background.active)} running.",
            },
        ]

    def status(self) -> dict[str, object]:
        return {
            "status": "ok",
            "phase": 1,
            "subsystems": self.subsystems(),
            "settings": self.settings.public_dict(),
            "providers": self.providers.describe(),
            "tools": {
                "count": len(self.tools.all()),
                "categories": self.tools.categories(),
                "names": [t.name for t in self.tools.all()],
            },
            "activity_subscribers": self.activity_bus.subscriber_count,
            "computer": {
                "enabled": self.computer.settings.enabled,
                "backend": self.computer.backend.key,
                "display": self.computer.capabilities.display,
                "display_kind": self.computer.capabilities.display_kind,
                "emergency_stop": self.computer.emergency_stop.engaged,
            },
            # Live browser stance, alongside computer's. Surfaced here rather
            # than read from the service by a route: the API layer is barred
            # from touching BrowserService at all, by a test, and the core is
            # the one place that legitimately owns it.
            "browser": {
                "enabled": self.browser.settings.enabled,
                "allow_localhost": self.browser.settings.allow_localhost,
                "allow_private_networks":
                    self.browser.settings.allow_private_networks,
                "headless": self.browser.settings.headless,
                "persists_storage": self.browser.settings.persists_storage,
                "pages_open": self.browser.page_count,
                "running": self.browser.started,
                # Ids and addresses only. Deliberately not titles: a title is
                # page-authored and this block is read by a status endpoint
                # whose other fields are all configuration — mixing untrusted
                # text into it would make one dict two trust levels.
                "pages": [
                    {"page_id": handle.page_id, "url": handle.page.url}
                    for handle in self.browser.pages()
                ],
            },
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
