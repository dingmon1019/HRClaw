from __future__ import annotations

from time import time
from typing import Iterable

from app.core.errors import ProviderError
from app.providers.registry import ProviderRegistry
from app.schemas.providers import ProviderRequest, ProviderStatus, ProviderTestRequest, ProviderTestResult
from app.schemas.settings import EffectiveSettings
from app.services.settings_service import SettingsService


class ProviderService:
    PROFILE_FIELDS = {
        "fast": "fast_provider",
        "cheap": "cheap_provider",
        "strong": "strong_provider",
        "local-only": "local_provider",
    }

    def __init__(self, registry: ProviderRegistry, settings_service: SettingsService):
        self.registry = registry
        self.settings_service = settings_service
        self._provider_state: dict[str, dict[str, object]] = {}

    def complete(self, request: ProviderRequest, settings_override: EffectiveSettings | None = None):
        settings = settings_override or self.settings_service.get_effective_settings()
        last_error: Exception | None = None
        candidates = list(self._provider_candidates(request, settings))
        if not candidates:
            raise ProviderError("No provider candidates are configured for this request.")

        for provider_name in candidates:
            if self._is_circuit_open(provider_name):
                last_error = ProviderError(f"Circuit open for provider {provider_name}.")
                continue

            provider = self.registry.get(provider_name)
            attempts = settings.provider_max_retries + 1
            for _ in range(attempts):
                try:
                    response = provider.complete(
                        request.model_copy(update={"provider_name": provider_name}),
                        settings,
                    )
                    self._record_success(provider_name)
                    return response
                except Exception as exc:
                    last_error = exc
                    self._record_failure(provider_name, settings, exc)
        raise ProviderError(f"Provider request failed after fallback attempts: {last_error}")

    def list_statuses(self, settings_override: EffectiveSettings | None = None) -> list[ProviderStatus]:
        settings = settings_override or self.settings_service.get_effective_settings()
        statuses: list[ProviderStatus] = []
        for provider in self.registry.list_all():
            status = provider.status(settings)
            state = self._provider_state.get(provider.name, {})
            statuses.append(
                status.model_copy(
                    update={
                        "available": status.available and not self._is_circuit_open(provider.name),
                        "circuit_open": self._is_circuit_open(provider.name),
                        "last_error": state.get("last_error"),
                    }
                )
            )
        return statuses

    def test_provider(
        self,
        request: ProviderTestRequest,
        settings_override: EffectiveSettings | None = None,
    ) -> ProviderTestResult:
        settings = settings_override or self.settings_service.get_effective_settings()
        provider_name = request.provider_name or settings.provider
        model_name = request.model_name or settings.model
        try:
            response = self.complete(
                ProviderRequest(
                    provider_name=provider_name,
                    model_name=model_name,
                    prompt=request.prompt,
                    system_prompt="Return a brief readiness response for a local agent runtime operator.",
                ),
                settings_override=settings,
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

    def resolve_profile_provider(
        self,
        profile: str | None,
        settings_override: EffectiveSettings | None = None,
    ) -> str | None:
        settings = settings_override or self.settings_service.get_effective_settings()
        request = ProviderRequest(prompt="profile-resolution", profile=profile)
        for candidate in self._provider_candidates(request, settings):
            return candidate
        return None

    def _provider_candidates(self, request: ProviderRequest, settings: EffectiveSettings) -> Iterable[str]:
        yielded: set[str] = set()

        def emit(candidate: str | None):
            if candidate and candidate not in yielded:
                yielded.add(candidate)
                return candidate
            return None

        if request.provider_name:
            candidate = emit(request.provider_name)
            if candidate:
                yield candidate

        requested_profile = request.profile
        if requested_profile:
            configured = getattr(settings, self.PROFILE_FIELDS.get(requested_profile, ""), None)
            for candidate in [configured, settings.provider, settings.fallback_provider]:
                emitted = emit(candidate)
                if emitted:
                    yield emitted
            if requested_profile == "local-only":
                emitted = emit("mock")
                if emitted:
                    yield emitted

        for candidate in [settings.provider, settings.fallback_provider]:
            emitted = emit(candidate)
            if emitted:
                yield emitted

    def _is_circuit_open(self, provider_name: str) -> bool:
        state = self._provider_state.get(provider_name) or {}
        opened_until = float(state.get("opened_until") or 0)
        return opened_until > time()

    def _record_success(self, provider_name: str) -> None:
        self._provider_state[provider_name] = {
            "failures": 0,
            "opened_until": 0.0,
            "last_error": None,
        }

    def _record_failure(self, provider_name: str, settings: EffectiveSettings, exc: Exception) -> None:
        state = self._provider_state.setdefault(
            provider_name,
            {"failures": 0, "opened_until": 0.0, "last_error": None},
        )
        failures = int(state.get("failures") or 0) + 1
        opened_until = float(state.get("opened_until") or 0)
        if failures >= settings.provider_circuit_breaker_threshold:
            opened_until = time() + settings.provider_circuit_breaker_seconds
            failures = 0
        self._provider_state[provider_name] = {
            "failures": failures,
            "opened_until": opened_until,
            "last_error": str(exc),
        }
