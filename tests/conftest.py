from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.testclient import TestClient

from app.api.routes import router
from app.config.settings import AppSettings
from app.core.container import AppContainer


@pytest.fixture()
def app_settings(tmp_path: Path) -> AppSettings:
    return AppSettings(
        app_name="Win Agent Runtime Test",
        database_path=tmp_path / "runtime.db",
        audit_log_path=tmp_path / "audit" / "audit.jsonl",
        allowed_filesystem_roots=str(tmp_path),
        allowed_http_hosts="127.0.0.1,localhost,testserver",
        powershell_allowlist="Get-ChildItem,Get-Content,Get-Date",
        provider="mock",
        fallback_provider="mock",
        model="mock-model",
        runtime_mode="safe",
    )


@pytest.fixture()
def container(app_settings: AppSettings) -> AppContainer:
    return AppContainer(app_settings)


@pytest.fixture()
def client(container: AppContainer, app_settings: AppSettings) -> TestClient:
    app = FastAPI(title=app_settings.app_name)
    app.state.container = container
    app.state.templates = Jinja2Templates(directory=str(app_settings.project_root / "ui"))
    app.mount("/static", StaticFiles(directory=str(app_settings.project_root / "ui" / "static")), name="static")
    app.include_router(router)
    return TestClient(app)

