from __future__ import annotations

from app.config.settings import AppSettings
from app.core.container import AppContainer
from app.schemas.providers import ProviderConfigUpdate, ProviderRequest


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


def test_circuit_breaker_opens_after_repeated_provider_failure(tmp_path):
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

    response = container.provider_service.complete(ProviderRequest(prompt="Circuit breaker fallback"))
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


def test_provider_runtime_state_is_reloaded_from_database(tmp_path):
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
    first_container.provider_service.complete(ProviderRequest(prompt="Trigger provider failure fallback"))

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
        ),
        actor="pytest",
        reason="provider-config-test",
    )

    settings = container.settings_service.get_effective_settings()
    loaded = next(config for config in settings.provider_configs if config.provider_name == "openai-compatible")
    statuses = {status.name: status for status in container.provider_service.list_statuses()}

    assert record.base_url == "http://127.0.0.1:11434/v1"
    assert loaded.default_model == "local-model"
    assert statuses["openai-compatible"].base_url == "http://127.0.0.1:11434/v1"
    assert statuses["openai-compatible"].allowed_hosts == ["127.0.0.1", "localhost"]
    assert statuses["openai-compatible"].privacy_posture == "external-egress"
    assert statuses["openai-compatible"].destination_summary == "127.0.0.1, localhost"


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
