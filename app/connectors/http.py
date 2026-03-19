from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

import httpx

from app.connectors.base import BaseConnector
from app.core.errors import ConnectorError
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
        self._assert_host_allowed(url)
        with httpx.Client(timeout=20.0) as client:
            response = client.get(url)
            response.raise_for_status()
        return {
            "url": url,
            "status_code": response.status_code,
            "headers": dict(response.headers),
            "body_preview": response.text[:2000],
        }

    def execute(self, action_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        url = payload.get("url")
        method = action_type.split(".", 1)[1].upper()
        self._assert_host_allowed(url)
        headers = payload.get("headers") or {}
        body = payload.get("body")
        with httpx.Client(timeout=30.0) as client:
            response = client.request(method, url, headers=headers, content=body)
            response.raise_for_status()
        return {
            "url": url,
            "method": method,
            "status_code": response.status_code,
            "headers": dict(response.headers),
            "body_preview": response.text[:2000],
        }

    def _assert_host_allowed(self, url: str | None) -> None:
        if not url:
            raise ConnectorError("HTTP connector requires a URL.")
        host = urlparse(url).hostname
        if not host:
            raise ConnectorError("Invalid URL.")
        allowed_hosts = self.settings_service.get_effective_settings().allowed_http_hosts
        if allowed_hosts and host not in allowed_hosts:
            raise ConnectorError(f"Host {host} is outside the configured allowlist.")

