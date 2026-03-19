from __future__ import annotations

from app.config.settings import AppSettings
from app.core.errors import ProviderError
from app.providers.base import BaseProvider
from app.providers.http_providers import (
    AnthropicProvider,
    GeminiProvider,
    GenericHTTPProvider,
    OpenAICompatibleProvider,
    OpenAIProvider,
)
from app.providers.mock_provider import MockProvider


class ProviderRegistry:
    def __init__(self, base_settings: AppSettings):
        providers = [
            MockProvider(base_settings),
            OpenAIProvider(base_settings),
            OpenAICompatibleProvider(base_settings),
            GenericHTTPProvider(base_settings),
            AnthropicProvider(base_settings),
            GeminiProvider(base_settings),
        ]
        self._providers: dict[str, BaseProvider] = {provider.name: provider for provider in providers}

    def get(self, name: str) -> BaseProvider:
        provider = self._providers.get(name)
        if provider is None:
            raise ProviderError(f"Unknown provider: {name}")
        return provider

    def list_all(self) -> list[BaseProvider]:
        return list(self._providers.values())

