from __future__ import annotations

from app.config.settings import AppSettings
from app.core.container import AppContainer
from app.schemas.providers import ProviderRequest


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
