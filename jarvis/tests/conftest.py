"""Test fixtures.

Every test gets an isolated in-memory database and its own :class:`JarvisCore`,
so tests neither share state nor touch the developer's real data directory.

:class:`StubProvider` is a **test double**, not a shipped fake. It lives here,
never in ``src``, and exists so the agent loop, tool execution, and error
handling can be exercised deterministically without a network call or an API
key. Scripting its responses is also the only way to test paths a real provider
produces rarely — rate limits, refusals, malformed tool arguments.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio

os.environ.setdefault("JARVIS_ENV_FILE", "/nonexistent-so-tests-ignore-dotenv")
os.environ["JARVIS_REQUIRE_AUTH"] = "false"
os.environ["JARVIS_ENVIRONMENT"] = "test"
os.environ["JARVIS_LOG_LEVEL"] = "CRITICAL"
os.environ["JARVIS_DATABASE_URL"] = "sqlite+aiosqlite:///:memory:"

from jarvis.activity.service import ActivityBus  # noqa: E402
from jarvis.browser import BrowserService, BrowserSettings  # noqa: E402
from jarvis.config import Settings, reset_config_caches  # noqa: E402
from jarvis.computer.service import ComputerService, ComputerSettings  # noqa: E402
from jarvis.core import JarvisCore  # noqa: E402
from jarvis.db.base import Database  # noqa: E402
from jarvis.db.models import User  # noqa: E402
from jarvis.logging import configure_logging  # noqa: E402
from jarvis.orchestrator.core import Orchestrator  # noqa: E402
from jarvis.permissions.engine import seed_default_grants  # noqa: E402
from jarvis.providers.base import (  # noqa: E402
    AIProvider,
    CompletionRequest,
    CompletionResult,
    ModelInfo,
    ProviderCapability,
    StreamEnd,
    StreamEvent,
    StreamStart,
    TextBlock,
    TextDelta,
    ToolUseBlock,
    Usage,
)
from jarvis.providers.embeddings import LexicalEmbeddingProvider
from jarvis.providers.registry import ProviderRegistry  # noqa: E402
from jarvis.providers.retry import RetryPolicy  # noqa: E402
from jarvis.providers.router import ModelRouter  # noqa: E402
from jarvis.tools.registry import build_default_registry  # noqa: E402

configure_logging("CRITICAL")

STUB_CAPS = frozenset(
    {
        ProviderCapability.TEXT,
        ProviderCapability.STREAMING,
        ProviderCapability.TOOL_USE,
        ProviderCapability.STRUCTURED_OUTPUT,
    }
)


class StubProvider(AIProvider):
    """Scriptable provider double.

    Give it a list of ``CompletionResult`` objects (or exceptions) and it
    returns them in order, recording every request it received.
    """

    key = "stub"
    display_name = "Stub provider"

    def __init__(
        self,
        responses: list[CompletionResult | Exception] | None = None,
        *,
        configured: bool = True,
        capabilities: frozenset[ProviderCapability] = STUB_CAPS,
    ) -> None:
        self.responses: list[CompletionResult | Exception] = responses or []
        self.requests: list[CompletionRequest] = []
        self.call_count = 0
        self._configured = configured
        self._capabilities = capabilities

    # -- AIProvider -----------------------------------------------------------

    @property
    def capabilities(self) -> frozenset[ProviderCapability]:
        return self._capabilities

    @property
    def models(self) -> dict[str, ModelInfo]:
        return {
            "stub-model": ModelInfo(
                id="stub-model",
                display_name="Stub",
                context_window=200_000,
                max_output_tokens=8_192,
                capabilities=self._capabilities,
                input_price_per_mtok=1.0,
                output_price_per_mtok=2.0,
            )
        }

    @property
    def default_model(self) -> str:
        return "stub-model"

    def is_configured(self) -> bool:
        return self._configured

    async def complete(self, request: CompletionRequest) -> CompletionResult:
        self.requests.append(request)
        self.call_count += 1
        if not self.responses:
            return text_result("stub reply")
        nxt = self.responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    async def stream(  # type: ignore[override]
        self, request: CompletionRequest
    ) -> AsyncIterator[StreamEvent]:
        yield StreamStart(model=self.default_model, provider=self.key)
        result = await self.complete(request)
        for block in result.content:
            if isinstance(block, TextBlock):
                yield TextDelta(text=block.text)
        yield StreamEnd(result=result)


def text_result(text: str, **kwargs: Any) -> CompletionResult:
    return CompletionResult(
        content=[TextBlock(text=text)],
        stop_reason=kwargs.pop("stop_reason", "end_turn"),
        model=kwargs.pop("model", "stub-model"),
        provider=kwargs.pop("provider", "stub"),
        usage=kwargs.pop("usage", Usage(input_tokens=10, output_tokens=5, cost_micros=20)),
        latency_ms=kwargs.pop("latency_ms", 1.0),
    )


def tool_result(
    tool_name: str, arguments: dict[str, Any], *, call_id: str = "tu_1", text: str = ""
) -> CompletionResult:
    content: list[Any] = []
    if text:
        content.append(TextBlock(text=text))
    content.append(ToolUseBlock(id=call_id, name=tool_name, input=arguments))
    return CompletionResult(
        content=content,
        stop_reason="tool_use",
        model="stub-model",
        provider="stub",
        usage=Usage(input_tokens=20, output_tokens=15, cost_micros=50),
        latency_ms=1.0,
    )


# ── fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def settings() -> Settings:
    reset_config_caches()
    return Settings(
        environment="test",
        database_url="sqlite+aiosqlite:///:memory:",
        require_auth=False,
        log_level="CRITICAL",
        max_agent_iterations=4,
        tool_timeout_seconds=5.0,
    )


@pytest_asyncio.fixture
async def database() -> AsyncIterator[Database]:
    db = Database("sqlite+aiosqlite:///:memory:")
    await db.create_all()
    try:
        yield db
    finally:
        await db.dispose()


@pytest_asyncio.fixture
async def session(database: Database):
    async with database.session_factory() as s:
        yield s


@pytest_asyncio.fixture
async def user(session) -> User:
    u = User(name="tester", display_name="Tester")
    session.add(u)
    await session.flush()
    await seed_default_grants(session, u.id)
    await session.commit()
    return u


@pytest.fixture
def stub() -> StubProvider:
    return StubProvider()


@pytest_asyncio.fixture
async def core(settings: Settings, stub: StubProvider) -> AsyncIterator[JarvisCore]:
    """A fully wired JARVIS whose only provider is the stub."""
    database = Database(settings.resolved_database_url)
    providers = ProviderRegistry()
    providers.register(stub)
    tools = build_default_registry()
    router = ModelRouter(providers, settings)
    bus = ActivityBus()

    # The lexical vectoriser, deliberately: tests must be deterministic and
    # must not need an embedding endpoint. Its scores are lexical, which is
    # exactly what the retrieval tests assert against.
    embeddings = LexicalEmbeddingProvider()

    # A computer service with no display: the tests assert refusal behaviour
    # and policy decisions, both of which are display-independent. The tests
    # that need a real display create their own service and skip when there
    # is none.
    computer = ComputerService(
        ComputerSettings(enabled=True, use_virtual_display=False),
        router=router,
        activity_bus=bus,
    )
    computer.start()

    instance = JarvisCore(
        settings=settings,
        database=database,
        providers=providers,
        tools=tools,
        router=router,
        activity_bus=bus,
        embeddings=embeddings,
        computer=computer,
        # Constructed, never launched. Nothing in the general fixture browses,
        # and a Chromium process per test would be absurd; the browser tests
        # build their own service with their own settings.
        browser=BrowserService(BrowserSettings()),
        orchestrator=Orchestrator(
            registry=tools,
            router=router,
            activity_bus=bus,
            retry=RetryPolicy(max_attempts=2, base_delay=0.001),
            tool_timeout_seconds=settings.tool_timeout_seconds,
            max_iterations=settings.max_agent_iterations,
            embeddings=embeddings,
            # Ambient capture off by default in tests: it would fire a second
            # provider call on every orchestrator test and consume stub
            # responses the test queued for the main turn. The evaluator has
            # its own tests that switch it on explicitly.
            memory_capture_mode="off",
            computer=computer,
        ),
    )
    await instance.startup(create_schema=True)
    try:
        yield instance
    finally:
        await instance.shutdown()


@pytest_asyncio.fixture
async def client(core: JarvisCore, settings: Settings):
    """HTTP client bound to the stub-backed core."""
    from fastapi.testclient import TestClient

    from jarvis.api.app import create_app

    app = create_app(settings, core=core)
    # The core is already started; suppress the lifespan's second startup by
    # handing the app a ready instance and entering the context normally —
    # startup() is idempotent for schema creation and tool sync.
    with TestClient(app) as c:
        yield c
