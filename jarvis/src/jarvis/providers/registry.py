"""Provider registry — construction and lookup."""

from __future__ import annotations

from jarvis.config import Settings, get_secret
from jarvis.errors import ProviderNotConfiguredError
from jarvis.logging import get_logger
from jarvis.providers.anthropic_provider import AnthropicProvider
from jarvis.providers.base import AIProvider, ModelInfo, ProviderCapability
from jarvis.providers.openai_compat import OpenAICompatProvider, local_provider

log = get_logger(__name__)


class ProviderRegistry:
    """Holds the configured providers for the process.

    A provider is *registered* whether or not it has credentials; an
    unconfigured provider still shows up in ``/system/status`` so the UI can
    tell you what is missing rather than pretending it does not exist. Only
    :meth:`configured` entries are eligible for routing.
    """

    def __init__(self) -> None:
        self._providers: dict[str, AIProvider] = {}

    def register(self, provider: AIProvider) -> None:
        self._providers[provider.key] = provider
        log.info(
            "provider_registered",
            provider=provider.key,
            configured=provider.is_configured(),
            capabilities=sorted(c.value for c in provider.capabilities),
        )

    def get(self, key: str) -> AIProvider:
        provider = self._providers.get(key)
        if provider is None:
            raise ProviderNotConfiguredError(f"No provider registered under '{key}'")
        return provider

    def try_get(self, key: str) -> AIProvider | None:
        return self._providers.get(key)

    def all(self) -> list[AIProvider]:
        return list(self._providers.values())

    def configured(self) -> list[AIProvider]:
        return [p for p in self._providers.values() if p.is_configured()]

    def with_capabilities(
        self, required: frozenset[ProviderCapability]
    ) -> list[AIProvider]:
        return [p for p in self.configured() if required <= p.capabilities]

    def describe(self) -> list[dict[str, object]]:
        return [p.describe() for p in self._providers.values()]

    def has_any_configured(self) -> bool:
        return bool(self.configured())

    async def aclose(self) -> None:
        for provider in self._providers.values():
            closer = getattr(provider, "aclose", None)
            if closer is not None:
                await closer()


def build_registry(settings: Settings) -> ProviderRegistry:
    """Construct providers from configuration.

    Credentials are looked up by *name* through the secrets chain, so a key can
    live in the environment during development and in the OS keychain on the
    desktop without any code change.
    """
    registry = ProviderRegistry()

    registry.register(
        AnthropicProvider(
            api_key=get_secret(settings.anthropic_api_key_name),
            default_model=settings.model_conversation,
            timeout=settings.provider_timeout_seconds,
        )
    )

    openai_key = get_secret(settings.openai_api_key_name)
    if openai_key or settings.openai_base_url:
        base_url = settings.openai_base_url or "https://api.openai.com/v1"
        is_local = any(
            host in base_url for host in ("localhost", "127.0.0.1", "0.0.0.0")
        )
        if is_local:
            registry.register(
                local_provider(
                    base_url=base_url,
                    default_model=(
                        settings.openai_compat_models[0]
                        if settings.openai_compat_models
                        else "local-model"
                    ),
                    models=_declared_models(
                        settings.openai_compat_models,
                        capabilities=frozenset(
                            {ProviderCapability.TEXT, ProviderCapability.STREAMING}
                        ),
                    ),
                )
            )
        else:
            registry.register(
                OpenAICompatProvider(
                    base_url=base_url,
                    api_key=openai_key,
                    default_model=(
                        settings.openai_compat_models[0]
                        if settings.openai_compat_models
                        else "gpt-4o-mini"
                    ),
                    models=_declared_models(settings.openai_compat_models),
                    timeout=settings.provider_timeout_seconds,
                )
            )

    if not registry.has_any_configured():
        log.warning(
            "no_provider_configured",
            hint=(
                f"Set {settings.anthropic_api_key_name} in the environment or the "
                "OS keychain. JARVIS will serve non-model endpoints but cannot "
                "answer conversationally."
            ),
        )
    return registry


def _declared_models(
    names: list[str],
    *,
    capabilities: frozenset[ProviderCapability] | None = None,
) -> dict[str, ModelInfo]:
    """Turn the configured model names into a declared catalogue.

    Only the *default* model used to be recorded, so an OpenAI-compatible
    provider reported an empty ``models`` dict. Harmless for routing — the
    router falls back to ``default_model`` — and not harmless for anything that
    asks a provider what it offers. The console's model picker does, so a
    machine running Ollama got an empty dropdown and no way to choose the second
    model it had pulled.

    The numbers are deliberately conservative placeholders. A local runtime does
    not publish a context window over this API, and inventing a large one would
    let ``min_context_tokens`` select a provider that then truncates silently —
    a lie the model never mentions. Price is zero because it is: the electricity
    is already being spent.
    """
    caps = capabilities or frozenset(
        {ProviderCapability.TEXT, ProviderCapability.STREAMING,
         ProviderCapability.TOOL_USE}
    )
    return {
        name: ModelInfo(
            id=name,
            display_name=name,
            # Zero rather than a guess. `_apply_constraints` treats an
            # undeclared window as "do not exclude", which is the right
            # behaviour for a runtime that cannot be asked.
            context_window=0,
            max_output_tokens=0,
            capabilities=caps,
        )
        for name in names
    }
