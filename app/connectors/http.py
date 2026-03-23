from __future__ import annotations

import ipaddress
import socket
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
        if not url:
            raise ConnectorError("HTTP connector requires a URL.")
        settings = self.settings_service.get_effective_settings()
        parsed = urlparse(url)
        host = parsed.hostname
        if not host:
            raise ConnectorError("Invalid URL.")
        if parsed.scheme not in settings.allowed_http_schemes:
            raise ConnectorError(f"Scheme {parsed.scheme or '(empty)'} is not allowed.")
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        if settings.allowed_http_ports and port not in settings.allowed_http_ports:
            raise ConnectorError(f"Port {port} is not allowed.")
        if method not in {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"}:
            raise ConnectorError(f"HTTP method {method} is not allowed.")
        allowed_hosts = settings.allowed_http_hosts
        if allowed_hosts and host not in allowed_hosts:
            raise ConnectorError(f"Host {host} is outside the configured allowlist.")
        if not settings.allow_http_private_network and self._is_private_target(host):
            raise ConnectorError(f"Private or localhost target {host} is blocked by policy.")

    @staticmethod
    def _is_private_target(host: str) -> bool:
        if host in {"localhost"}:
            return True
        try:
            return ipaddress.ip_address(host).is_private or ipaddress.ip_address(host).is_loopback
        except ValueError:
            infos = socket.getaddrinfo(host, None)
            for info in infos:
                address = info[4][0]
                ip = ipaddress.ip_address(address)
                if ip.is_private or ip.is_loopback:
                    return True
        return False

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
            if len(response.content) > settings.http_max_response_bytes:
                raise ConnectorError("HTTP response exceeded the configured size limit.")
            return response
