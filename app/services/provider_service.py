from __future__ import annotations

from typing import Iterable

from app.core.errors import ProviderError
from app.providers.registry import ProviderRegistry
from app.schemas.providers import ProviderRequest, ProviderStatus, ProviderTestRequest, ProviderTestResult
from app.services.settings_service import SettingsService


class ProviderService:
    def __init__(self, registry: ProviderRegistry, settings_service: SettingsService):
        self.registry = registry
        self.settings_service = settings_service

    def complete(self, request: ProviderRequest):
        settings = self.settings_service.get_effective_settings()
        provider_candidates = self._provider_candidates(
            request.provider_name,
            settings.provider,
            settings.fallback_provider,
        )
        last_error: Exception | None = None
        for provider_name in provider_candidates:
            provider = self.registry.get(provider_name)
            for _ in range(settings.provider_max_retries + 1):
                try:
                    return provider.complete(request, settings)
                except Exception as exc:
                    last_error = exc
        raise ProviderError(f"Provider request failed: {last_error}")

    def list_statuses(self) -> list[ProviderStatus]:
        settings = self.settings_service.get_effective_settings()
        return [provider.status(settings) for provider in self.registry.list_all()]

    def test_provider(self, request: ProviderTestRequest) -> ProviderTestResult:
        settings = self.settings_service.get_effective_settings()
        provider_name = request.provider_name or settings.provider
        model_name = request.model_name or settings.model
        try:
            response = self.complete(
                ProviderRequest(
                    provider_name=provider_name,
                    model_name=model_name,
                    prompt=request.prompt,
                    system_prompt="Return a brief readiness response for a local agent runtime operator.",
                )
            )
            return ProviderTestResult(
                provider_name=response.provider_name,
                model_name=response.model_name,
                ok=True,
                message="Provider responded successfully.",
                content=response.content,
            )
        except Exception as exc:
            return ProviderTestResult(
                provider_name=provider_name,
                model_name=model_name,
                ok=False,
                message=str(exc),
                content=None,
            )

    @staticmethod
    def _provider_candidates(
        requested_name: str | None,
        default_name: str,
        fallback_name: str | None,
    ) -> Iterable[str]:
        yielded: set[str] = set()
        for candidate in [requested_name, default_name, fallback_name]:
            if candidate and candidate not in yielded:
                yielded.add(candidate)
                yield candidate
