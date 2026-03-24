from __future__ import annotations

import ipaddress
from urllib.parse import urlparse

import httpx

from app.core.errors import ConnectorError, ProviderError


def validate_url(
    url: str | None,
    *,
    allowed_schemes: list[str],
    allowed_ports: list[int],
    allowed_hosts: list[str],
    allow_private_network: bool,
    purpose: str,
) -> tuple[str, int]:
    if not url:
        raise ConnectorError(f"{purpose} requires a URL.")
    parsed = urlparse(url)
    host = parsed.hostname
    if not host:
        raise ConnectorError(f"Invalid URL for {purpose}.")
    if parsed.scheme not in allowed_schemes:
        raise ConnectorError(f"Scheme {parsed.scheme or '(empty)'} is not allowed for {purpose}.")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if allowed_ports and port not in allowed_ports:
        raise ConnectorError(f"Port {port} is not allowed for {purpose}.")
    if allowed_hosts and host not in allowed_hosts:
        raise ConnectorError(f"Host {host} is outside the configured allowlist for {purpose}.")
    if not allow_private_network and is_private_target(host):
        raise ConnectorError(f"Private or localhost target {host} is blocked for {purpose}.")
    return host, port


def validate_provider_url(
    url: str | None,
    *,
    allowed_schemes: list[str],
    allowed_ports: list[int],
    allowed_hosts: list[str],
    allow_private_network: bool,
) -> None:
    try:
        validate_url(
            url,
            allowed_schemes=allowed_schemes,
            allowed_ports=allowed_ports,
            allowed_hosts=allowed_hosts,
            allow_private_network=allow_private_network,
            purpose="provider egress",
        )
    except ConnectorError as exc:
        raise ProviderError(str(exc)) from exc


def is_private_target(host: str) -> bool:
    normalized = (host or "").strip().strip("[]").lower()
    if normalized in {"localhost", "localhost.localdomain"} or normalized.endswith(".localhost"):
        return True
    try:
        ip = ipaddress.ip_address(normalized)
        return ip.is_private or ip.is_loopback
    except ValueError:
        return False


def enforce_response_constraints(
    response: httpx.Response,
    *,
    max_response_bytes: int,
    allowed_text_types: tuple[str, ...] = ("text/",),
) -> None:
    if len(response.content) > max_response_bytes:
        raise ConnectorError("HTTP response exceeded the configured size limit.")
    content_type = response.headers.get("content-type", "")
    text_like = any(content_type.startswith(prefix) for prefix in allowed_text_types) or any(
        marker in content_type for marker in ("json", "xml")
    )
    if not text_like:
        raise ConnectorError(f"Unsupported response content-type: {content_type or 'unknown'}.")
