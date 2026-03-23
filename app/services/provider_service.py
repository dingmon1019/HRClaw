from __future__ import annotations

from time import time
from typing import Iterable

from app.audit.service import AuditService
from app.core.database import Database
from app.core.errors import ProviderError
from app.core.utils import json_dumps, json_loads, utcnow_iso
from app.providers.registry import ProviderRegistry
from app.schemas.actions import DataClassification
from app.schemas.providers import (
    ProviderConfigRecord,
    ProviderRequest,
    ProviderStatus,
    ProviderTestRequest,
    ProviderTestResult,
)
from app.schemas.settings import EffectiveSettings
from app.services.settings_service import SettingsService


class ProviderService:
    PROFILE_FIELDS = {
        "fast": "fast_provider",
        "cheap": "cheap_provider",
        "strong": "strong_provider",
        "local-only": "local_provider",
        "privacy-preferred": "privacy_provider",
    }
    TASK_CAPABILITY_MAP = {
        "planning-summary": {"planning"},
        "report-plan": {"text"},
        "provider-test": {"text"},
    }

    def __init__(
        self,
        registry: ProviderRegistry,
        settings_service: SettingsService,
        database: Database,
        audit_service: AuditService,
    ):
        self.registry = registry
        self.settings_service = settings_service
        self.database = database
        self.audit_service = audit_service
        self._provider_state: dict[str, dict[str, object]] = self._load_provider_state()

    def complete(self, request: ProviderRequest, settings_override: EffectiveSettings | None = None):
        settings = settings_override or self.settings_service.get_effective_settings()
        last_error: Exception | None = None
        candidates = list(self._provider_candidates(request, settings))
        if not candidates:
            raise ProviderError("No provider candidates are configured for this request.")

        chosen_candidate: str | None = None
        for provider_name in candidates:
            provider = self.registry.get(provider_name)
            provider_settings = self._settings_for_provider(provider_name, settings)
            try:
                self._validate_provider_usage(provider_name, provider.supports_remote, request, provider_settings)
                self._validate_provider_capabilities(provider_name, provider.capabilities, request)
            except ProviderError as exc:
                last_error = exc
                self._record_failure(provider_name, provider_settings, exc)
                continue

            if self._is_circuit_open(provider_name):
                last_error = ProviderError(f"Circuit open for provider {provider_name}.")
                continue

            attempts = provider_settings.provider_max_retries + 1
            for _ in range(attempts):
                try:
                    response = provider.complete(
                        request.model_copy(update={"provider_name": provider_name}),
                        provider_settings,
                    )
                    if chosen_candidate and chosen_candidate != provider_name:
                        self.audit_service.emit(
                            "provider.fallback",
                            {
                                "from_provider": chosen_candidate,
                                "to_provider": provider_name,
                                "correlation_id": request.correlation_id,
                            },
                        )
                    chosen_candidate = provider_name
                    self._record_success(provider_name)
                    return response
                except Exception as exc:
                    last_error = exc
                    if chosen_candidate is None:
                        chosen_candidate = provider_name
                    self._record_failure(provider_name, provider_settings, exc)
        raise ProviderError(f"Provider request failed after fallback attempts: {last_error}")

    def list_statuses(self, settings_override: EffectiveSettings | None = None) -> list[ProviderStatus]:
        settings = settings_override or self.settings_service.get_effective_settings()
        statuses: list[ProviderStatus] = []
        checked_at = utcnow_iso()
        for provider in self.registry.list_all():
            provider_settings = self._settings_for_provider(provider.name, settings)
            provider_config = self._provider_config(provider.name, settings)
            status = provider.status(provider_settings)
            state = self._provider_state.get(provider.name, {})
            normalized = status.model_copy(
                update={
                    "enabled": provider_config.enabled,
                    "available": provider_config.enabled and status.available and not self._is_circuit_open(provider.name),
                    "circuit_open": self._is_circuit_open(provider.name),
                    "last_error": state.get("last_error"),
                    "healthy": provider_config.enabled and status.configured and not self._is_circuit_open(provider.name),
                    "allowed_hosts": provider_config.allowed_hosts,
                    "last_checked_at": checked_at,
                    "base_url": provider_config.base_url,
                    "generic_http_endpoint": provider_config.generic_http_endpoint,
                    "api_key_env": provider_config.api_key_env,
                    "default_model": provider_config.default_model,
                    "auth_source": provider_config.auth_source,
                    "privacy_posture": self._privacy_posture(provider.name, status.supports_remote),
                    "egress_posture": self._egress_posture(provider_config, settings, status.supports_remote),
                    "destination_summary": self._destination_summary(provider_config, settings),
                }
            )
            statuses.append(normalized)
            self.database.execute(
                """
                INSERT INTO provider_health(provider_name, healthy, circuit_open, last_error, metadata_json, checked_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(provider_name) DO UPDATE SET
                    healthy = excluded.healthy,
                    circuit_open = excluded.circuit_open,
                    last_error = excluded.last_error,
                    metadata_json = excluded.metadata_json,
                    checked_at = excluded.checked_at
                """,
                (
                    normalized.name,
                    int(normalized.healthy),
                    int(normalized.circuit_open),
                    normalized.last_error,
                    json_dumps(
                        {
                            **normalized.model_dump(mode="json"),
                            "failures": int(state.get("failures") or 0),
                            "opened_until": float(state.get("opened_until") or 0.0),
                        }
                    ),
                    checked_at,
                ),
            )
        return statuses

    def test_provider(
        self,
        request: ProviderTestRequest,
        settings_override: EffectiveSettings | None = None,
    ) -> ProviderTestResult:
        settings = settings_override or self.settings_service.get_effective_settings()
        provider_name = request.provider_name or settings.provider
        resolved_settings = self._settings_for_provider(provider_name, settings)
        model_name = request.model_name or resolved_settings.model
        try:
            response = self.complete(
                ProviderRequest(
                    provider_name=provider_name,
                    model_name=model_name,
                    prompt=request.prompt,
                    system_prompt="Return a brief readiness response for a local agent runtime operator.",
                    data_classification=request.data_classification,
                    task_type="provider-test",
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
            provider = self.registry.get(candidate)
            try:
                self._validate_provider_usage(candidate, provider.supports_remote, request, settings)
            except ProviderError:
                continue
            return candidate
        return None

    def _provider_candidates(self, request: ProviderRequest, settings: EffectiveSettings) -> Iterable[str]:
        yielded: set[str] = set()

        def emit(candidate: str | None):
            if candidate and candidate not in yielded:
                config = self._provider_config(candidate, settings)
                if not config.enabled:
                    return None
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
            if requested_profile in {"local-only", "privacy-preferred"}:
                emitted = emit("mock")
                if emitted:
                    yield emitted

        for candidate in [settings.provider, settings.fallback_provider, settings.privacy_provider]:
            emitted = emit(candidate)
            if emitted:
                yield emitted

    def _validate_provider_usage(
        self,
        provider_name: str,
        supports_remote: bool,
        request: ProviderRequest,
        settings: EffectiveSettings,
    ) -> None:
        classification = DataClassification(request.data_classification)
        if classification == DataClassification.LOCAL_ONLY and supports_remote:
            raise ProviderError(f"Provider {provider_name} is remote-only and cannot handle local-only data.")
        if (
            classification == DataClassification.RESTRICTED
            and supports_remote
            and not settings.allow_restricted_provider_egress
        ):
            raise ProviderError(
                f"Restricted data cannot be sent to remote provider {provider_name} without override."
            )

    def _validate_provider_capabilities(
        self,
        provider_name: str,
        capabilities: list[str],
        request: ProviderRequest,
    ) -> None:
        required = self.TASK_CAPABILITY_MAP.get(request.task_type or "", {"text"})
        if not required.issubset(set(capabilities)):
            missing = ", ".join(sorted(required.difference(set(capabilities))))
            raise ProviderError(
                f"Provider {provider_name} does not support required capabilities: {missing}."
            )

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
        self._persist_provider_state(provider_name)

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
        self._persist_provider_state(provider_name)

    def _load_provider_state(self) -> dict[str, dict[str, object]]:
        rows = self.database.fetch_all("SELECT provider_name, metadata_json, last_error FROM provider_health")
        state: dict[str, dict[str, object]] = {}
        for row in rows:
            metadata = json_loads(row["metadata_json"], {})
            state[row["provider_name"]] = {
                "failures": int(metadata.get("failures") or 0),
                "opened_until": float(metadata.get("opened_until") or 0.0),
                "last_error": row["last_error"] or metadata.get("last_error"),
            }
        return state

    def _persist_provider_state(self, provider_name: str) -> None:
        state = self._provider_state.get(provider_name, {})
        checked_at = utcnow_iso()
        self.database.execute(
            """
            INSERT INTO provider_health(provider_name, healthy, circuit_open, last_error, metadata_json, checked_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(provider_name) DO UPDATE SET
                healthy = excluded.healthy,
                circuit_open = excluded.circuit_open,
                last_error = excluded.last_error,
                metadata_json = excluded.metadata_json,
                checked_at = excluded.checked_at
            """,
            (
                provider_name,
                int(not self._is_circuit_open(provider_name)),
                int(self._is_circuit_open(provider_name)),
                state.get("last_error"),
                json_dumps(state),
                checked_at,
            ),
        )

    def _provider_config(self, provider_name: str, settings: EffectiveSettings) -> ProviderConfigRecord:
        for config in settings.provider_configs:
            if config.provider_name == provider_name:
                return config
        return ProviderConfigRecord(provider_name=provider_name)

    def _settings_for_provider(self, provider_name: str, settings: EffectiveSettings) -> EffectiveSettings:
        config = self._provider_config(provider_name, settings)
        allowed_hosts = config.allowed_hosts or settings.provider_allowed_hosts
        return settings.model_copy(
            update={
                "base_url": config.base_url if config.base_url is not None else settings.base_url,
                "generic_http_endpoint": (
                    config.generic_http_endpoint
                    if config.generic_http_endpoint is not None
                    else settings.generic_http_endpoint
                ),
                "api_key_env": config.api_key_env or settings.api_key_env,
                "model": config.default_model or settings.model,
                "provider_allowed_hosts": allowed_hosts,
            }
        )

    @staticmethod
    def _privacy_posture(provider_name: str, supports_remote: bool) -> str:
        if provider_name == "mock" or not supports_remote:
            return "local-only"
        return "external-egress"

    @staticmethod
    def _destination_summary(config: ProviderConfigRecord, settings: EffectiveSettings) -> str:
        destinations = config.allowed_hosts or settings.provider_allowed_hosts
        if destinations:
            return ", ".join(destinations)
        return "no outbound host configured"

    def _egress_posture(
        self,
        config: ProviderConfigRecord,
        settings: EffectiveSettings,
        supports_remote: bool,
    ) -> str:
        if not config.enabled:
            return "disabled"
        if not supports_remote:
            return "no external egress"
        if config.allowed_hosts:
            return "restricted allowlist"
        if settings.provider_allowed_hosts:
            return "inherits global allowlist"
        return "no outbound host configured"
