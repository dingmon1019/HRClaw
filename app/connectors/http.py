from __future__ import annotations

from typing import Any

import httpx

from app.connectors.base import BaseConnector
from app.core.errors import ConnectorError
from app.policy.network_guard import enforce_response_constraints, validate_url
from app.services.settings_service import SettingsService


class HttpConnector(BaseConnector):
    name = "http"
    description = "HTTP connector with host allowlist enforcement."

    def __init__(self, settings_service: SettingsService):
        self.settings_service = settings_service

    def healthcheck(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "available": True,
            "description": self.description,
            "allowed_hosts": self.settings_service.get_effective_settings().allowed_http_hosts,
        }

    def collect(self, payload: dict[str, Any]) -> dict[str, Any]:
        url = payload.get("url")
        settings = self.settings_service.get_effective_settings()
        self._assert_request_allowed(url, "GET")
        response = self._perform_request("GET", url, {}, None, settings)
        return {
            "url": url,
            "status_code": response.status_code,
            "headers": dict(response.headers),
            "body_preview": self._preview_body(response, settings.http_max_response_bytes),
        }

    def execute(self, action_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        url = payload.get("url")
        method = action_type.split(".", 1)[1].upper()
        settings = self.settings_service.get_effective_settings()
        self._assert_request_allowed(url, method)
        headers = payload.get("headers") or {}
        body = payload.get("body")
        response = self._perform_request(method, url, headers, body, settings)
        return {
            "url": url,
            "method": method,
            "status_code": response.status_code,
            "headers": dict(response.headers),
            "body_preview": self._preview_body(response, settings.http_max_response_bytes),
        }

    def _assert_request_allowed(self, url: str | None, method: str) -> None:
        settings = self.settings_service.get_effective_settings()
        validate_url(
            url,
            allowed_schemes=settings.allowed_http_schemes,
            allowed_ports=settings.allowed_http_ports,
            allowed_hosts=settings.allowed_http_hosts,
            allow_private_network=settings.allow_http_private_network,
            purpose="http connector",
        )
        if method not in {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"}:
            raise ConnectorError(f"HTTP method {method} is not allowed.")

    @staticmethod
    def _preview_body(response: httpx.Response, max_bytes: int) -> str:
        content_type = response.headers.get("content-type", "")
        text_like = content_type.startswith("text/") or "json" in content_type or "xml" in content_type
        if not text_like:
            return f"Binary or unsupported content-type: {content_type or 'unknown'}"
        body = response.text
        return body[:max_bytes]

    @staticmethod
    def _perform_request(method: str, url: str, headers: dict, body: str | None, settings) -> httpx.Response:
        timeout = httpx.Timeout(settings.http_timeout_seconds)
        with httpx.Client(timeout=timeout, follow_redirects=settings.http_follow_redirects) as client:
            response = client.request(method, url, headers=headers, content=body)
            response.raise_for_status()
            enforce_response_constraints(response, max_response_bytes=settings.http_max_response_bytes)
            return response
