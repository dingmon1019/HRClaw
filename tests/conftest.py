from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api.app import create_app
from app.config.settings import AppSettings
from app.core.container import AppContainer
from tests.helpers import bootstrap_operator, extract_csrf_token


@pytest.fixture()
def app_settings(tmp_path: Path) -> AppSettings:
    workspace_root = tmp_path / "workspace"
    return AppSettings(
        app_name="Win Agent Runtime Test",
        runtime_state_root=tmp_path / "runtime-state",
        database_path=tmp_path / "runtime.db",
        audit_log_path=tmp_path / "audit" / "audit.jsonl",
        workspace_root=workspace_root,
        allowed_filesystem_roots=str(workspace_root),
        allowed_http_hosts="example.com,127.0.0.1,localhost,testserver",
        trusted_hosts="127.0.0.1,localhost,testserver",
        provider="mock",
        fallback_provider="mock",
        model="mock-model",
        runtime_mode="safe",
        graph_execution_mode="inline_compat",
        allow_insecure_local_storage=True,
        session_secret="test-session-secret",
        session_cookie_name="test_session",
    )


@pytest.fixture()
def app(app_settings: AppSettings):
    return create_app(app_settings)


@pytest.fixture()
def container(app) -> AppContainer:
    return app.state.container


@pytest.fixture()
def client(app) -> TestClient:
    return TestClient(app)


@pytest.fixture()
def authenticated_client(client: TestClient) -> TestClient:
    bootstrap_operator(client)
    return client


@pytest.fixture()
def auth_headers(authenticated_client: TestClient) -> dict[str, str]:
    response = authenticated_client.get("/", headers={"accept": "text/html"})
    csrf_token = extract_csrf_token(response.text)
    return {
        "accept": "application/json",
        "x-csrf-token": csrf_token,
    }
