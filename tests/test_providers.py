from __future__ import annotations

from app.config.settings import AppSettings
from app.core.container import AppContainer
from app.schemas.providers import ProviderConfigUpdate, ProviderRequest, ProviderResponse


def test_mock_provider_returns_content(container):
    response = container.provider_service.complete(
        ProviderRequest(prompt="Summarize readiness.", provider_name="mock", model_name="mock-model")
    )

    assert response.provider_name == "mock"
    assert "Mock provider summary" in response.content


def test_provider_falls_back_to_mock_when_primary_is_unconfigured(tmp_path):
    settings = AppSettings(
        app_name="Fallback Test",
        runtime_state_root=tmp_path / "runtime-state",
        database_path=tmp_path / "runtime.db",
        audit_log_path=tmp_path / "audit.jsonl",
        workspace_root=tmp_path / "workspace",
        allowed_filesystem_roots=str(tmp_path / "workspace"),
        provider="openai",
        fallback_provider="mock",
        model="mock-model",
        session_secret="test-session-secret",
    )
    container = AppContainer(settings)

    response = container.provider_service.complete(ProviderRequest(prompt="Fallback please"))

    assert response.provider_name == "mock"


def test_circuit_breaker_opens_after_repeated_provider_failure(tmp_path, monkeypatch):
    settings = AppSettings(
        app_name="Circuit Test",
        runtime_state_root=tmp_path / "runtime-state",
        database_path=tmp_path / "runtime.db",
        audit_log_path=tmp_path / "audit.jsonl",
        workspace_root=tmp_path / "workspace",
        allowed_filesystem_roots=str(tmp_path / "workspace"),
        provider="openai",
        fallback_provider="mock",
        model="mock-model",
        provider_circuit_breaker_threshold=1,
        provider_circuit_breaker_seconds=300,
        session_secret="test-session-secret",
    )
    container = AppContainer(settings)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(
        container.provider_registry.get("openai"),
        "complete",
        lambda request, provider_settings: (_ for _ in ()).throw(RuntimeError("provider boom")),
    )

    response = container.provider_service.complete(ProviderRequest(prompt="Circuit breaker fallback", provider_name="openai"))
    statuses = {status.name: status for status in container.provider_service.list_statuses()}

    assert response.provider_name == "mock"
    assert statuses["openai"].circuit_open is True
    assert statuses["openai"].last_error


def test_local_only_profile_prefers_local_provider(container):
    response = container.provider_service.complete(
        ProviderRequest(prompt="Stay local.", profile="local-only")
    )

    assert response.provider_name == "mock"


def test_provider_health_is_persisted_to_database(container):
    statuses = container.provider_service.list_statuses()

    row = container.database.fetch_one(
        "SELECT provider_name, metadata_json FROM provider_health WHERE provider_name = ?",
        (statuses[0].name,),
    )

    assert row is not None
    assert row["provider_name"] == statuses[0].name


def test_provider_runtime_state_is_reloaded_from_database(tmp_path, monkeypatch):
    settings = AppSettings(
        app_name="Provider Persistence Test",
        runtime_state_root=tmp_path / "runtime-state",
        database_path=tmp_path / "runtime.db",
        audit_log_path=tmp_path / "audit.jsonl",
        workspace_root=tmp_path / "workspace",
        allowed_filesystem_roots=str(tmp_path / "workspace"),
        provider="openai",
        fallback_provider="mock",
        model="mock-model",
        provider_circuit_breaker_threshold=1,
        provider_circuit_breaker_seconds=300,
        session_secret="test-session-secret",
        allow_insecure_local_storage=True,
    )
    first_container = AppContainer(settings)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(
        first_container.provider_registry.get("openai"),
        "complete",
        lambda request, provider_settings: (_ for _ in ()).throw(RuntimeError("provider boom")),
    )
    first_container.provider_service.complete(ProviderRequest(prompt="Trigger provider failure fallback", provider_name="openai"))

    second_container = AppContainer(settings)
    statuses = {status.name: status for status in second_container.provider_service.list_statuses()}

    assert statuses["openai"].circuit_open is True


def test_provider_specific_config_is_persisted_and_visible(container):
    record = container.settings_service.save_provider_config(
        ProviderConfigUpdate(
            provider_name="openai-compatible",
            enabled=True,
            base_url="http://127.0.0.1:11434/v1",
            api_key_env="LOCAL_OPENAI_KEY",
            default_model="local-model",
            allowed_hosts=["127.0.0.1", "localhost"],
            cost_tier="low",
            latency_tier="low",
            privacy_tier="local",
        ),
        actor="pytest",
        reason="provider-config-test",
    )

    settings = container.settings_service.get_effective_settings()
    loaded = next(config for config in settings.provider_configs if config.provider_name == "openai-compatible")
    statuses = {status.name: status for status in container.provider_service.list_statuses()}

    assert record.base_url == "http://127.0.0.1:11434/v1"
    assert loaded.default_model == "local-model"
    assert loaded.cost_tier == "low"
    assert loaded.latency_tier == "low"
    assert loaded.privacy_tier == "local"
    assert statuses["openai-compatible"].base_url == "http://127.0.0.1:11434/v1"
    assert statuses["openai-compatible"].allowed_hosts == ["127.0.0.1", "localhost"]
    assert statuses["openai-compatible"].privacy_posture == "local-gateway"
    assert statuses["openai-compatible"].endpoint_locality == "local-gateway"
    assert statuses["openai-compatible"].destination_summary == "127.0.0.1, localhost"
    assert statuses["openai-compatible"].cost_tier == "low"
    assert statuses["openai-compatible"].latency_tier == "low"
    assert statuses["openai-compatible"].privacy_tier == "local"


def test_disabled_provider_is_removed_from_candidate_chain(tmp_path):
    settings = AppSettings(
        app_name="Disabled Provider Test",
        runtime_state_root=tmp_path / "runtime-state",
        database_path=tmp_path / "runtime.db",
        audit_log_path=tmp_path / "audit.jsonl",
        workspace_root=tmp_path / "workspace",
        allowed_filesystem_roots=str(tmp_path / "workspace"),
        provider="openai",
        fallback_provider="mock",
        model="mock-model",
        session_secret="test-session-secret",
        allow_insecure_local_storage=True,
    )
    container = AppContainer(settings)
    container.settings_service.save_provider_config(
        ProviderConfigUpdate(provider_name="openai", enabled=False),
        actor="pytest",
        reason="disable-openai",
    )

    response = container.provider_service.complete(ProviderRequest(prompt="Prefer fallback when disabled"))

    assert response.provider_name == "mock"


def test_explicit_provider_override_is_respected_with_scoring(container, monkeypatch):
    container.settings_service.save_provider_config(
        ProviderConfigUpdate(
            provider_name="openai-compatible",
            enabled=True,
            base_url="http://127.0.0.1:11434/v1",
            default_model="local-model",
            cost_tier="low",
            latency_tier="low",
            privacy_tier="external",
        ),
        actor="pytest",
        reason="explicit-provider-override",
    )
    monkeypatch.setattr(
        container.provider_registry.get("openai-compatible"),
        "complete",
        lambda request, provider_settings: ProviderResponse(
            provider_name="openai-compatible",
            model_name=request.model_name or provider_settings.model,
            content="openai-compatible response",
            raw_response={},
        ),
    )

    response = container.provider_service.complete(
        ProviderRequest(
            prompt="Use the requested provider first.",
            provider_name="openai-compatible",
            profile="fast",
        )
    )
    statuses = {status.name: status for status in container.provider_service.list_statuses()}

    assert response.provider_name == "openai-compatible"
    assert statuses["openai-compatible"].last_score > 0
    assert "explicit=" in (statuses["openai-compatible"].last_routing_reason or "")


def test_all_enabled_configured_providers_enter_candidate_pool(container):
    container.settings_service.save_provider_config(
        ProviderConfigUpdate(
            provider_name="openai-compatible",
            enabled=True,
            base_url="http://127.0.0.1:11434/v1",
            default_model="local-model",
        ),
        actor="pytest",
        reason="enable-openai-compatible",
    )
    container.settings_service.save_provider_config(
        ProviderConfigUpdate(
            provider_name="generic-http",
            enabled=True,
            generic_http_endpoint="http://127.0.0.1:9000/generate",
            default_model="gateway-model",
        ),
        actor="pytest",
        reason="enable-generic-http",
    )

    candidates = container.provider_service._ranked_provider_candidates(  # noqa: SLF001
        ProviderRequest(prompt="Rank all enabled providers.", profile="fast"),
        container.settings_service.get_effective_settings(),
    )
    candidate_names = [candidate["provider_name"] for candidate in candidates]

    assert "mock" in candidate_names
    assert "openai-compatible" in candidate_names
    assert "generic-http" in candidate_names


def test_observed_latency_influences_provider_ranking(container):
    container.settings_service.save_provider_config(
        ProviderConfigUpdate(
            provider_name="openai-compatible",
            enabled=True,
            base_url="http://127.0.0.1:11434/v1",
            default_model="local-model",
            latency_tier="standard",
            privacy_tier="local",
        ),
        actor="pytest",
        reason="latency-openai-compatible",
    )
    container.settings_service.save_provider_config(
        ProviderConfigUpdate(
            provider_name="generic-http",
            enabled=True,
            generic_http_endpoint="http://127.0.0.1:9000/generate",
            default_model="gateway-model",
            latency_tier="standard",
            privacy_tier="local",
        ),
        actor="pytest",
        reason="latency-generic-http",
    )
    container.provider_service._provider_state["openai-compatible"] = {  # noqa: SLF001
        "failures": 0,
        "opened_until": 0.0,
        "last_error": None,
        "success_count": 5,
        "failure_count": 0,
        "last_score": 0.0,
        "last_routing_reason": None,
        "latency_ewma_ms": 80.0,
        "success_rate": 1.0,
        "consecutive_failures": 0,
        "last_error_category": None,
    }
    container.provider_service._provider_state["generic-http"] = {  # noqa: SLF001
        "failures": 0,
        "opened_until": 0.0,
        "last_error": None,
        "success_count": 5,
        "failure_count": 0,
        "last_score": 0.0,
        "last_routing_reason": None,
        "latency_ewma_ms": 1800.0,
        "success_rate": 1.0,
        "consecutive_failures": 0,
        "last_error_category": None,
    }

    candidates = container.provider_service._ranked_provider_candidates(  # noqa: SLF001
        ProviderRequest(prompt="Prefer the lowest observed latency.", profile="fast"),
        container.settings_service.get_effective_settings(),
    )

    assert candidates[0]["provider_name"] == "openai-compatible"


def test_review_capability_filters_provider_candidates(container):
    container.settings_service.save_provider_config(
        ProviderConfigUpdate(
            provider_name="openai-compatible",
            enabled=True,
            base_url="http://127.0.0.1:11434/v1",
            default_model="local-model",
        ),
        actor="pytest",
        reason="enable-review-capable-provider",
    )
    container.settings_service.save_provider_config(
        ProviderConfigUpdate(
            provider_name="generic-http",
            enabled=True,
            generic_http_endpoint="http://127.0.0.1:9000/generate",
            default_model="gateway-model",
        ),
        actor="pytest",
        reason="enable-non-review-provider",
    )

    candidates = container.provider_service._ranked_provider_candidates(  # noqa: SLF001
        ProviderRequest(prompt="Review this plan.", profile="strong", task_type="review-branch"),
        container.settings_service.get_effective_settings(),
    )
    candidate_names = [candidate["provider_name"] for candidate in candidates]

    assert "openai-compatible" in candidate_names
    assert "generic-http" not in candidate_names


def test_local_compatible_gateway_can_handle_local_only_prompts(container, monkeypatch):
    container.settings_service.save_provider_config(
        ProviderConfigUpdate(
            provider_name="openai-compatible",
            enabled=True,
            base_url="http://127.0.0.1:11434/v1",
            default_model="local-gpt-5",
            allowed_hosts=["127.0.0.1"],
            privacy_tier="local",
        ),
        actor="pytest",
        reason="local-compatible-gateway",
    )
    monkeypatch.setattr(
        container.provider_registry.get("openai-compatible"),
        "complete",
        lambda request, provider_settings: ProviderResponse(
            provider_name="openai-compatible",
            model_name=request.model_name or provider_settings.model,
            content="local gateway response",
            raw_response={},
        ),
    )

    response = container.provider_service.complete(
        ProviderRequest(
            provider_name="openai-compatible",
            prompt="Keep this local-only.",
            data_classification="local-only",
        )
    )
    statuses = {status.name: status for status in container.provider_service.list_statuses()}

    assert response.provider_name == "openai-compatible"
    assert statuses["openai-compatible"].endpoint_locality == "local-gateway"


def test_model_fit_influences_review_ranking(container):
    container.settings_service.save_provider_config(
        ProviderConfigUpdate(
            provider_name="openai-compatible",
            enabled=True,
            base_url="http://127.0.0.1:11434/v1",
            default_model="gpt-5",
            privacy_tier="local",
        ),
        actor="pytest",
        reason="review-model-fit-strong",
    )
    container.settings_service.save_provider_config(
        ProviderConfigUpdate(
            provider_name="generic-http",
            enabled=True,
            generic_http_endpoint="https://gateway.example.com/generate",
            default_model="mini-model",
            privacy_tier="external",
        ),
        actor="pytest",
        reason="review-model-fit-weak",
    )

    candidates = container.provider_service._ranked_provider_candidates(  # noqa: SLF001
        ProviderRequest(prompt="Review this plan carefully.", task_type="review-branch", profile="strong"),
        container.settings_service.get_effective_settings(),
    )

    assert candidates[0]["provider_name"] == "openai-compatible"


def test_budget_exhaustion_removes_provider_from_candidates(container):
    container.settings_service.save_provider_config(
        ProviderConfigUpdate(
            provider_name="openai-compatible",
            enabled=True,
            base_url="http://127.0.0.1:11434/v1",
            default_model="local-model",
            budget_limit_units=0.1,
        ),
        actor="pytest",
        reason="budget-limit-test",
    )
    container.provider_service._provider_state["openai-compatible"] = {  # noqa: SLF001
        "failures": 0,
        "opened_until": 0.0,
        "last_error": None,
        "success_count": 3,
        "failure_count": 0,
        "last_score": 0.0,
        "last_routing_reason": None,
        "latency_ewma_ms": 50.0,
        "success_rate": 1.0,
        "consecutive_failures": 0,
        "last_error_category": None,
        "budget_used_units": 0.2,
    }

    candidates = container.provider_service._ranked_provider_candidates(  # noqa: SLF001
        ProviderRequest(prompt="Avoid budget exhausted providers.", profile="fast"),
        container.settings_service.get_effective_settings(),
    )
    statuses = {status.name: status for status in container.provider_service.list_statuses()}

    assert all(candidate["provider_name"] != "openai-compatible" for candidate in candidates)
    assert statuses["openai-compatible"].budget_exhausted is True
