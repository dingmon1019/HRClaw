from __future__ import annotations

import httpx

from app.core.errors import ProviderError
from app.policy.network_guard import validate_provider_url
from app.schemas.settings import EffectiveSettings


def post_json(
    *,
    url: str,
    headers: dict[str, str] | None,
    payload: dict,
    settings: EffectiveSettings,
) -> dict:
    validate_provider_url(
        url,
        allowed_schemes=settings.allowed_http_schemes,
        allowed_ports=settings.allowed_http_ports,
        allowed_hosts=settings.provider_allowed_hosts,
        allow_private_network=settings.allow_provider_private_network,
    )
    timeout = httpx.Timeout(settings.provider_timeout_seconds)
    with httpx.Client(timeout=timeout, follow_redirects=False) as client:
        response = client.post(url, headers=headers or {}, json=payload)
        response.raise_for_status()
        if len(response.content) > settings.http_max_response_bytes:
            raise ProviderError("Provider response exceeded the configured size limit.")
        content_type = response.headers.get("content-type", "")
        if "json" not in content_type:
            raise ProviderError(f"Provider returned unsupported content-type: {content_type or 'unknown'}")
        return response.json()
