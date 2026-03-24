from __future__ import annotations

import json

from app.schemas.providers import ProviderConfigUpdate
from tests.helpers import bootstrap_operator, extract_csrf_token


def test_protected_routes_redirect_to_setup_before_bootstrap(client):
    response = client.get("/", headers={"accept": "text/html"}, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/setup"


def test_setup_stores_password_hash_and_logout_requires_csrf(client, container):
    _, password = bootstrap_operator(client)
    row = container.database.fetch_one("SELECT username, password_hash FROM users LIMIT 1")
    assert row["username"] == "operator"
    assert row["password_hash"] != password
    assert row["password_hash"].startswith("pbkdf2_sha256$")

    home = client.get("/", headers={"accept": "text/html"})
    csrf_token = extract_csrf_token(home.text)
    logout = client.post(
        "/logout",
        data={"csrf_token": csrf_token},
        headers={"accept": "text/html"},
        follow_redirects=False,
    )
    assert logout.status_code == 303
    assert logout.headers["location"].startswith("/login")


def test_api_requires_authentication_before_setup(client):
    response = client.get("/api/providers", headers={"accept": "application/json"})
    assert response.status_code == 403


def test_api_run_requires_csrf(authenticated_client, auth_headers):
    response = authenticated_client.post(
        "/api/runs",
        json={"objective": "Create a task through the API", "task_title": "API task"},
        headers={"accept": "application/json"},
    )
    assert response.status_code == 400
    assert "CSRF" in response.json()["detail"]


def test_api_run_and_queue_flow(authenticated_client, auth_headers):
    create_response = authenticated_client.post(
        "/api/runs",
        json={"objective": "Create a task through the API", "task_title": "API task"},
        headers=auth_headers,
    )
    assert create_response.status_code == 200
    payload = create_response.json()
    proposal_id = payload["proposals"][0]["id"]

    approve_response = authenticated_client.post(
        f"/api/proposals/{proposal_id}/approve",
        json={"actor": "ignored", "reason": "queue from api"},
        headers=auth_headers,
    )
    assert approve_response.status_code == 202
    result = approve_response.json()
    assert result["proposal"]["status"] == "queued"
    assert result["job"]["status"] == "queued"


def test_api_provider_list_after_login(authenticated_client, auth_headers):
    response = authenticated_client.get("/api/providers", headers={"accept": "application/json"})
    assert response.status_code == 200
    names = {item["name"] for item in response.json()}
    assert "mock" in names


def test_run_page_uses_assistant_first_copy(authenticated_client):
    response = authenticated_client.get("/run", headers={"accept": "text/html"})

    assert response.status_code == 200
    assert "Ask The Agent" in response.text
    assert "Expert Inputs" in response.text
    assert "What You Will See Next" in response.text
    assert "Choose Workspace File" in response.text


def test_dashboard_exposes_assistant_first_entry(authenticated_client):
    response = authenticated_client.get("/", headers={"accept": "text/html"})

    assert response.status_code == 200
    assert "Ask The Agent" in response.text
    assert "Interpret and Prepare" in response.text


def test_dashboard_shows_worker_task_status_when_available(authenticated_client, monkeypatch):
    monkeypatch.setattr(
        "app.api.routes._run_windows_script",
        lambda *args, **kwargs: json.dumps(
            {
                "TaskName": "WinAgentRuntime.Worker",
                "State": "Ready",
                "LastRunTime": "2026-03-24 13:10:00",
                "NextRunTime": "2026-03-24 13:15:00",
                "LastTaskResult": 0,
            }
        ),
    )

    response = authenticated_client.get("/", headers={"accept": "text/html"})

    assert response.status_code == 200
    assert "Worker Task" in response.text
    assert "Ready" in response.text
    assert "Agent Work Root" in response.text


def test_settings_page_shows_provider_catalog_and_windows_ops(authenticated_client):
    response = authenticated_client.get("/settings", headers={"accept": "text/html"})

    assert response.status_code == 200
    assert "Provider Catalog" in response.text
    assert "Windows Operations" in response.text
    assert "Worker Task Control" in response.text
    assert "Privacy Posture" in response.text
    assert "Storage posture" in response.text
    assert "windows-credential-manager" in response.text
    assert "Observed Metrics" in response.text
    assert "Credential Secret" in response.text


def test_run_detail_renders_task_graph_summary(authenticated_client, auth_headers):
    create_response = authenticated_client.post(
        "/api/runs",
        json={"objective": "Create a task through the API", "task_title": "API task"},
        headers=auth_headers,
    )
    run_id = create_response.json()["run_id"]

    response = authenticated_client.get(f"/runs/{run_id}", headers={"accept": "text/html"})

    assert response.status_code == 200
    assert "Task Graph" in response.text
    assert "Stored node details" in response.text
    assert "approval required" in response.text
    assert "Execution Boundary" in response.text
    assert "Merge reviewed branches" in response.text
    assert "Agent Swimlanes" in response.text
    assert "Dependency Edges" in response.text
    assert "Agent Work Areas" in response.text


def test_run_detail_shows_deferred_evidence(authenticated_client, auth_headers):
    container = authenticated_client.app.state.container
    target = container.base_settings.resolved_workspace_root / "notes.txt"
    target.write_text("TOP SECRET FILE CONTENT", encoding="utf-8")

    create_response = authenticated_client.post(
        "/api/runs",
        json={"objective": "Inspect a local note", "filesystem_path": "notes.txt"},
        headers=auth_headers,
    )
    run_id = create_response.json()["run_id"]

    response = authenticated_client.get(f"/runs/{run_id}", headers={"accept": "text/html"})

    assert response.status_code == 200
    assert "Deferred evidence" in response.text
    assert "Gather filesystem evidence with filesystem.read_text" in response.text


def test_workspace_picker_returns_workspace_relative_path(authenticated_client, auth_headers, monkeypatch):
    container = authenticated_client.app.state.container
    selected = container.base_settings.resolved_workspace_root / "picked.txt"
    selected.write_text("picked", encoding="utf-8")

    monkeypatch.setattr(
        "app.api.routes._run_windows_script",
        lambda *args, **kwargs: str(selected),
    )

    response = authenticated_client.post(
        "/api/windows/workspace-file-picker",
        headers=auth_headers,
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["cancelled"] is False
    assert payload["filesystem_path"] == "picked.txt"


def test_workspace_picker_rejects_outside_workspace(authenticated_client, auth_headers, monkeypatch):
    outside = authenticated_client.app.state.container.base_settings.project_root / "README.md"
    monkeypatch.setattr(
        "app.api.routes._run_windows_script",
        lambda *args, **kwargs: str(outside),
    )

    response = authenticated_client.post(
        "/api/windows/workspace-file-picker",
        headers=auth_headers,
    )

    assert response.status_code == 400
    assert "managed workspace" in response.json()["detail"]


def test_worker_task_status_action_renders_structured_result(authenticated_client, monkeypatch):
    monkeypatch.setattr(
        "app.api.routes._run_windows_script",
        lambda *args, **kwargs: json.dumps(
            {
                "TaskName": "WinAgentRuntime.Worker",
                "State": "Ready",
                "LastRunTime": "2026-03-24 13:10:00",
                "NextRunTime": "2026-03-24 13:15:00",
                "LastTaskResult": 0,
            }
        ),
    )

    page = authenticated_client.get("/settings", headers={"accept": "text/html"})
    csrf_token = extract_csrf_token(page.text)
    response = authenticated_client.post(
        "/settings/windows/worker-task",
        data={
            "csrf_token": csrf_token,
            "action": "status",
            "task_name": "WinAgentRuntime.Worker",
            "token_file": "worker.token",
            "limit": 0,
            "interval": 2,
            "current_password": "",
        },
        headers={"accept": "text/html"},
    )

    assert response.status_code == 200
    assert "Worker Task Status" in response.text
    assert "WinAgentRuntime.Worker" in response.text
    assert "Ready" in response.text


def test_provider_credential_check_renders_status(authenticated_client, monkeypatch):
    container = authenticated_client.app.state.container
    container.settings_service.save_provider_config(
        ProviderConfigUpdate(
            provider_name="openai",
            enabled=True,
            auth_source="credential-manager",
            credential_target="WinAgentRuntime/provider/openai",
        ),
        actor="pytest",
        reason="credential-check",
    )
    monkeypatch.setattr(container.windows_credential_store, "available", True)
    monkeypatch.setattr(
        container.windows_credential_store,
        "describe",
        lambda target: {"available": True, "target": target, "configured": True},
    )

    page = authenticated_client.get("/settings", headers={"accept": "text/html"})
    csrf_token = extract_csrf_token(page.text)
    response = authenticated_client.post(
        "/settings/provider-config/test-credential",
        data={
            "csrf_token": csrf_token,
            "provider_name": "openai",
            "credential_target": "WinAgentRuntime/provider/openai",
            "current_password": "",
        },
        headers={"accept": "text/html"},
    )

    assert response.status_code == 200
    assert "Credential Check" in response.text
    assert "WinAgentRuntime/provider/openai" in response.text
