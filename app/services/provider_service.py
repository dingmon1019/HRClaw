from __future__ import annotations

from time import time
from typing import Iterable

from app.audit.service import AuditService
from app.core.database import Database
from app.core.errors import ProviderError
from app.core.utils import json_dumps, json_loads, sha256_hex, utcnow_iso
from app.providers.registry import ProviderRegistry
from app.security.windows_credential_store import WindowsCredentialStore
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
        "planning-branch": {"planning"},
        "review-branch": {"review"},
        "report-plan": {"text"},
        "provider-test": {"text"},
    }

    def __init__(
        self,
        registry: ProviderRegistry,
        settings_service: SettingsService,
        database: Database,
        audit_service: AuditService,
        credential_store: WindowsCredentialStore | None = None,
    ):
        self.registry = registry
        self.settings_service = settings_service
        self.database = database
        self.audit_service = audit_service
        self.credential_store = credential_store or WindowsCredentialStore()
        self._provider_state: dict[str, dict[str, object]] = self._load_provider_state()

    def complete(self, request: ProviderRequest, settings_override: EffectiveSettings | None = None):
        settings = settings_override or self.settings_service.get_effective_settings()
        last_error: Exception | None = None
        candidates = self._ranked_provider_candidates(request, settings)
        if not candidates:
            raise ProviderError("No provider candidates are configured for this request.")

        self.audit_service.emit(
            "provider.routing_planned",
            {
                "correlation_id": request.correlation_id,
                "task_type": request.task_type,
                "profile": request.profile,
                "candidates": [
                    {
                        "provider": candidate["provider_name"],
                        "score": candidate["score"],
                        "reason": candidate["reason"],
                    }
                    for candidate in candidates
                ],
            },
        )

        primary_candidate = candidates[0]["provider_name"]
        for candidate in candidates:
            provider_name = candidate["provider_name"]
            provider = candidate["provider"]
            provider_settings = candidate["settings"]
            provider_request = candidate["request"]
            attempts = provider_settings.provider_max_retries + 1
            for _ in range(attempts):
                try:
                    self._emit_prompt_outbound(provider_name, provider_request)
                    started = time()
                    response = provider.complete(provider_request, provider_settings)
                    latency_ms = max(1.0, (time() - started) * 1000.0)
                    response.raw_response["_routing"] = {
                        "selected_provider": provider_name,
                        "score": candidate["score"],
                        "reason": candidate["reason"],
                        "profile": request.profile,
                        "task_type": request.task_type,
                        "prompt_variant": provider_request.metadata.get("selected_prompt_variant"),
                        "routing_mode": provider_request.metadata.get("selected_routing_mode"),
                        "outbound_classification": provider_request.data_classification,
                        "prompt_digest": provider_request.metadata.get("selected_prompt_digest"),
                        "prompt_governance": provider_request.metadata.get("prompt_governance"),
                    }
                    if primary_candidate != provider_name:
                        self.audit_service.emit(
                            "provider.fallback",
                            {
                                "from_provider": primary_candidate,
                                "to_provider": provider_name,
                                "correlation_id": request.correlation_id,
                                "reason": candidate["reason"],
                            },
                        )
                    self.audit_service.emit(
                        "provider.routing_selected",
                        {
                            "provider": provider_name,
                            "score": candidate["score"],
                            "reason": candidate["reason"],
                            "correlation_id": request.correlation_id,
                        },
                    )
                    self._record_success(
                        provider_name,
                        score=candidate["score"],
                        reason=candidate["reason"],
                        latency_ms=latency_ms,
                    )
                    return response
                except Exception as exc:
                    last_error = exc
                    self._record_failure(
                        provider_name,
                        provider_settings,
                        exc,
                        score=candidate["score"],
                        reason=candidate["reason"],
                    )
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
            credential_available = self._credential_available(provider_config)
            normalized = status.model_copy(
                update={
                    "enabled": provider_config.enabled,
                    "available": provider_config.enabled
                    and self._provider_configured(provider, provider_settings, provider_config)
                    and status.available
                    and not self._is_circuit_open(provider.name),
                    "circuit_open": self._is_circuit_open(provider.name),
                    "last_error": state.get("last_error"),
                    "healthy": provider_config.enabled and self._provider_configured(provider, provider_settings, provider_config) and not self._is_circuit_open(provider.name),
                    "allowed_hosts": provider_config.allowed_hosts,
                    "last_checked_at": checked_at,
                    "base_url": provider_config.base_url,
                    "generic_http_endpoint": provider_config.generic_http_endpoint,
                    "api_key_env": provider_config.api_key_env,
                    "default_model": provider_config.default_model,
                    "auth_source": provider_config.auth_source,
                    "credential_target": provider_config.credential_target,
                    "credential_available": credential_available,
                    "configured": self._provider_configured(provider, provider_settings, provider_config),
                    "cost_tier": provider_config.cost_tier,
                    "latency_tier": provider_config.latency_tier,
                    "privacy_tier": provider_config.privacy_tier,
                    "privacy_posture": self._privacy_posture(provider.name, status.supports_remote),
                    "egress_posture": self._egress_posture(provider_config, settings, status.supports_remote),
                    "destination_summary": self._destination_summary(provider_config, settings),
                    "success_count": int(state.get("success_count") or 0),
                    "failure_count": int(state.get("failure_count") or 0),
                    "last_score": float(state.get("last_score") or 0.0),
                    "last_routing_reason": state.get("last_routing_reason"),
                    "latency_ewma_ms": float(state.get("latency_ewma_ms") or 0.0),
                    "success_rate": float(state.get("success_rate") or 0.0),
                    "consecutive_failures": int(state.get("consecutive_failures") or 0),
                    "last_error_category": state.get("last_error_category"),
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
                            "success_count": int(state.get("success_count") or 0),
                            "failure_count": int(state.get("failure_count") or 0),
                            "last_score": float(state.get("last_score") or 0.0),
                            "last_routing_reason": state.get("last_routing_reason"),
                            "latency_ewma_ms": float(state.get("latency_ewma_ms") or 0.0),
                            "success_rate": float(state.get("success_rate") or 0.0),
                            "consecutive_failures": int(state.get("consecutive_failures") or 0),
                            "last_error_category": state.get("last_error_category"),
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
        request = ProviderRequest(prompt="profile-resolution", profile=profile, task_type="planning-summary")
        candidates = self._ranked_provider_candidates(request, settings)
        return candidates[0]["provider_name"] if candidates else None

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

        preferred = getattr(settings, self.PROFILE_FIELDS.get(request.profile or "", ""), None)
        for candidate in [
            request.provider_name,
            preferred,
            settings.provider,
            settings.fallback_provider,
            settings.privacy_provider,
            settings.local_provider,
        ]:
            emitted = emit(candidate)
            if emitted:
                yield emitted
        for provider in self.registry.list_all():
            emitted = emit(provider.name)
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

    def _record_success(self, provider_name: str, *, score: float, reason: str, latency_ms: float) -> None:
        previous = self._provider_state.get(provider_name) or {}
        success_count = int(previous.get("success_count") or 0) + 1
        failure_count = int(previous.get("failure_count") or 0)
        previous_latency = float(previous.get("latency_ewma_ms") or 0.0)
        latency_ewma = latency_ms if previous_latency <= 0 else (previous_latency * 0.7) + (latency_ms * 0.3)
        self._provider_state[provider_name] = {
            "failures": 0,
            "opened_until": 0.0,
            "last_error": None,
            "success_count": success_count,
            "failure_count": failure_count,
            "last_score": float(score),
            "last_routing_reason": reason,
            "latency_ewma_ms": latency_ewma,
            "success_rate": success_count / max(1, success_count + failure_count),
            "consecutive_failures": 0,
            "last_error_category": None,
        }
        self._persist_provider_state(provider_name)

    def _record_failure(
        self,
        provider_name: str,
        settings: EffectiveSettings,
        exc: Exception,
        *,
        score: float = 0.0,
        reason: str | None = None,
    ) -> None:
        state = self._provider_state.setdefault(
            provider_name,
            {
                "failures": 0,
                "opened_until": 0.0,
                "last_error": None,
                "success_count": 0,
                "failure_count": 0,
                "latency_ewma_ms": 0.0,
                "success_rate": 0.0,
                "consecutive_failures": 0,
                "last_error_category": None,
            },
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
            "success_count": int(state.get("success_count") or 0),
            "failure_count": int(state.get("failure_count") or 0) + 1,
            "last_score": float(score),
            "last_routing_reason": reason,
            "latency_ewma_ms": float(state.get("latency_ewma_ms") or 0.0),
            "success_rate": int(state.get("success_count") or 0)
            / max(1, int(state.get("success_count") or 0) + int(state.get("failure_count") or 0) + 1),
            "consecutive_failures": int(state.get("consecutive_failures") or 0) + 1,
            "last_error_category": self._error_category(exc),
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
                "success_count": int(metadata.get("success_count") or 0),
                "failure_count": int(metadata.get("failure_count") or 0),
                "last_score": float(metadata.get("last_score") or 0.0),
                "last_routing_reason": metadata.get("last_routing_reason"),
                "latency_ewma_ms": float(metadata.get("latency_ewma_ms") or 0.0),
                "success_rate": float(metadata.get("success_rate") or 0.0),
                "consecutive_failures": int(metadata.get("consecutive_failures") or 0),
                "last_error_category": metadata.get("last_error_category"),
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

    def _provider_configured(
        self,
        provider,
        provider_settings: EffectiveSettings,
        provider_config: ProviderConfigRecord,
    ) -> bool:
        if provider_config.auth_source == "credential-manager":
            return self._credential_available(provider_config)
        return provider.is_configured(provider_settings)

    def _credential_available(self, config: ProviderConfigRecord) -> bool:
        if config.auth_source != "credential-manager":
            return False
        return self.credential_store.has_credential(config.credential_target)

    def _resolve_provider_request(
        self,
        request: ProviderRequest,
        provider,
        provider_config: ProviderConfigRecord,
    ) -> ProviderRequest:
        metadata = dict(request.metadata)
        if provider_config.auth_source == "credential-manager":
            metadata["resolved_secret"] = self.credential_store.read_secret(provider_config.credential_target)
        prompt_variants = metadata.get("prompt_variants") if isinstance(metadata.get("prompt_variants"), dict) else {}
        prompt_variant_name = "remote" if provider.supports_remote else "local"
        prompt_variant = prompt_variants.get(prompt_variant_name) if prompt_variants else None
        if prompt_variant is None and prompt_variants:
            prompt_variant = prompt_variants.get("local") or prompt_variants.get("remote")
            if prompt_variant is prompt_variants.get("local"):
                prompt_variant_name = "local"
            elif prompt_variant is prompt_variants.get("remote"):
                prompt_variant_name = "remote"
        if prompt_variant:
            metadata["selected_prompt_variant"] = prompt_variant_name
            metadata["selected_routing_mode"] = prompt_variant.get("routing_mode")
            metadata["selected_prompt_digest"] = sha256_hex(prompt_variant.get("prompt", request.prompt))
            return request.model_copy(
                update={
                    "prompt": prompt_variant.get("prompt", request.prompt),
                    "system_prompt": prompt_variant.get("system_prompt", request.system_prompt),
                    "data_classification": prompt_variant.get("data_classification", request.data_classification),
                    "metadata": metadata,
                }
            )
        return request.model_copy(update={"metadata": metadata})

    def _ranked_provider_candidates(self, request: ProviderRequest, settings: EffectiveSettings) -> list[dict[str, object]]:
        ranked: list[dict[str, object]] = []
        for provider_name in self._provider_candidates(request, settings):
            provider = self.registry.get(provider_name)
            provider_settings = self._settings_for_provider(provider_name, settings)
            provider_config = self._provider_config(provider_name, settings)
            try:
                self._validate_provider_capabilities(provider_name, provider.capabilities, request)
                if self._is_circuit_open(provider_name):
                    raise ProviderError(f"Circuit open for provider {provider_name}.")
                if not self._provider_configured(provider, provider_settings, provider_config):
                    raise ProviderError(f"Provider {provider_name} is not configured for auth source {provider_config.auth_source}.")
                resolved_request = self._resolve_provider_request(request, provider, provider_config)
                self._validate_provider_usage(provider_name, provider.supports_remote, resolved_request, provider_settings)
                score, reason = self._score_provider(provider_name, provider, provider_config, resolved_request)
                ranked.append(
                    {
                        "provider_name": provider_name,
                        "provider": provider,
                        "settings": provider_settings,
                        "request": resolved_request.model_copy(update={"provider_name": provider_name}),
                        "score": score,
                        "reason": reason,
                    }
                )
            except Exception as exc:
                self._record_routing_skip(provider_name, str(exc))
        ranked.sort(key=lambda item: (float(item["score"]), item["provider_name"] == settings.provider), reverse=True)
        return ranked

    def _score_provider(
        self,
        provider_name: str,
        provider,
        provider_config: ProviderConfigRecord,
        request: ProviderRequest,
    ) -> tuple[float, str]:
        classification = DataClassification(request.data_classification)
        required = self.TASK_CAPABILITY_MAP.get(request.task_type or "", {"text"})
        capability_fit = 40.0 + (10.0 * len(required.intersection(set(provider.capabilities))))
        privacy_score = 10.0 if provider.supports_local else -5.0
        if request.profile in {"local-only", "privacy-preferred"}:
            privacy_score += 20.0 if provider.supports_local else -20.0
        if classification in {DataClassification.LOCAL_ONLY, DataClassification.RESTRICTED}:
            privacy_score += 15.0 if provider.supports_local else -25.0

        cost_map = {"low": 15.0, "standard": 6.0, "high": -8.0}
        latency_map = {"low": 15.0, "standard": 6.0, "high": -8.0}
        privacy_tier_map = {"local": 12.0, "standard": 4.0, "external": -6.0}
        cost_score = cost_map.get(provider_config.cost_tier, 0.0)
        latency_score = latency_map.get(provider_config.latency_tier, 0.0)
        posture_score = privacy_tier_map.get(provider_config.privacy_tier, 0.0)
        if request.profile == "cheap":
            cost_score *= 1.5
        if request.profile == "fast":
            latency_score *= 1.5
        if request.profile == "strong":
            capability_fit += 10.0 if "review" in provider.capabilities else 0.0

        state = self._provider_state.get(provider_name) or {}
        success_count = int(state.get("success_count") or 0)
        failure_count = int(state.get("failure_count") or 0)
        success_rate = float(state.get("success_rate") or 0.0)
        consecutive_failures = int(state.get("consecutive_failures") or 0)
        latency_ewma_ms = float(state.get("latency_ewma_ms") or 0.0)
        latency_observed_score = 0.0 if latency_ewma_ms <= 0 else max(-18.0, 18.0 - (latency_ewma_ms / 120.0))
        reliability_score = (success_rate * 20.0) + (success_count * 1.5) - (failure_count * 2.5) - (consecutive_failures * 6.0)
        current_settings = self.settings_service.get_effective_settings()
        if provider_name == current_settings.provider:
            reliability_score += 2.0
        preferred_provider = getattr(current_settings, self.PROFILE_FIELDS.get(request.profile or "", ""), None)
        if preferred_provider and provider_name == preferred_provider:
            reliability_score += 10.0
        error_category_penalty = -8.0 if state.get("last_error_category") in {"timeout", "network", "auth"} else 0.0
        explicit_override = 100.0 if request.provider_name == provider_name else 0.0
        total = (
            capability_fit
            + privacy_score
            + cost_score
            + latency_score
            + posture_score
            + latency_observed_score
            + reliability_score
            + error_category_penalty
            + explicit_override
        )
        reason = (
            f"capability={capability_fit:.1f}, privacy={privacy_score:.1f}, cost={cost_score:.1f}, "
            f"latency={latency_score:.1f}, observed_latency={latency_observed_score:.1f}, posture={posture_score:.1f}, "
            f"reliability={reliability_score:.1f}, error_penalty={error_category_penalty:.1f}, explicit={explicit_override:.1f}"
        )
        return total, reason

    def _record_routing_skip(self, provider_name: str, reason: str) -> None:
        state = self._provider_state.get(provider_name) or {
            "failures": 0,
            "opened_until": 0.0,
            "last_error": None,
            "success_count": 0,
            "failure_count": 0,
            "latency_ewma_ms": 0.0,
            "success_rate": 0.0,
            "consecutive_failures": 0,
            "last_error_category": None,
        }
        self._provider_state[provider_name] = {
            **state,
            "last_routing_reason": reason,
        }
        self._persist_provider_state(provider_name)

    def _emit_prompt_outbound(self, provider_name: str, request: ProviderRequest) -> None:
        governance = request.metadata.get("prompt_governance") if request.metadata else None
        self.audit_service.emit(
            "provider.prompt_outbound",
            {
                "provider": provider_name,
                "correlation_id": request.correlation_id,
                "task_type": request.task_type,
                "selected_variant": request.metadata.get("selected_prompt_variant") if request.metadata else None,
                "routing_mode": request.metadata.get("selected_routing_mode") if request.metadata else None,
                "outbound_classification": request.data_classification,
                "prompt_digest": request.metadata.get("selected_prompt_digest") if request.metadata else sha256_hex(request.prompt),
                "prompt_governance": governance or {},
            },
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

    @staticmethod
    def _error_category(exc: Exception) -> str:
        text = str(exc).lower()
        if "timeout" in text:
            return "timeout"
        if "401" in text or "403" in text or "unauthorized" in text or "forbidden" in text:
            return "auth"
        if "connect" in text or "dns" in text or "network" in text:
            return "network"
        return "unknown"
