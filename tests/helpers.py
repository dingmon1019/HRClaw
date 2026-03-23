from __future__ import annotations

import re

from fastapi.testclient import TestClient


CSRF_RE = re.compile(r'name="csrf_token" value="([^"]+)"')


def extract_csrf_token(html: str) -> str:
    match = CSRF_RE.search(html)
    assert match, "CSRF token was not found in the HTML response."
    return match.group(1)


def bootstrap_operator(
    client: TestClient,
    username: str = "operator",
    password: str = "SuperSecure123!",
) -> tuple[str, str]:
    response = client.get("/setup", headers={"accept": "text/html"})
    assert response.status_code == 200
    csrf_token = extract_csrf_token(response.text)
    create_response = client.post(
        "/setup",
        data={
            "username": username,
            "password": password,
            "confirm_password": password,
            "csrf_token": csrf_token,
        },
        headers={"accept": "text/html"},
        follow_redirects=False,
    )
    assert create_response.status_code == 303
    return username, password
