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
        database_path=tmp_path / "runtime.db",
        audit_log_path=tmp_path / "audit.jsonl",
        allowed_filesystem_roots=str(tmp_path),
        provider="openai",
        fallback_provider="mock",
        model="mock-model",
    )
    container = AppContainer(settings)

    response = container.provider_service.complete(ProviderRequest(prompt="Fallback please"))

    assert response.provider_name == "mock"

