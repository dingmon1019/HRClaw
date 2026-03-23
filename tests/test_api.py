from __future__ import annotations

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


def test_dashboard_exposes_assistant_first_entry(authenticated_client):
    response = authenticated_client.get("/", headers={"accept": "text/html"})

    assert response.status_code == 200
    assert "Ask The Agent" in response.text
    assert "Interpret and Prepare" in response.text


def test_settings_page_shows_provider_catalog_and_windows_ops(authenticated_client):
    response = authenticated_client.get("/settings", headers={"accept": "text/html"})

    assert response.status_code == 200
    assert "Provider Catalog" in response.text
    assert "Windows Operations" in response.text
    assert "Privacy Posture" in response.text
    assert "Storage posture" in response.text


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
