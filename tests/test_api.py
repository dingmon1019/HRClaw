from __future__ import annotations


def test_dashboard_route(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "Win Agent Runtime" in response.text


def test_api_run_and_approve_flow(client):
    create_response = client.post(
        "/api/runs",
        json={"objective": "Create a task through the API", "task_title": "API task"},
    )
    assert create_response.status_code == 200
    payload = create_response.json()
    proposal_id = payload["proposals"][0]["id"]

    approve_response = client.post(
        f"/api/proposals/{proposal_id}/approve",
        json={"actor": "api-test", "reason": "approve through api"},
    )
    assert approve_response.status_code == 200
    result = approve_response.json()
    assert result["proposal"]["status"] == "executed"


def test_api_provider_list(client):
    response = client.get("/api/providers")
    assert response.status_code == 200
    names = {item["name"] for item in response.json()}
    assert "mock" in names
