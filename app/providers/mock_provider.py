from __future__ import annotations

from app.providers.base import BaseProvider
from app.schemas.providers import ProviderRequest, ProviderResponse
from app.schemas.settings import EffectiveSettings


class MockProvider(BaseProvider):
    name = "mock"
    description = "Deterministic provider for local testing and offline development."
    profiles = ["fast", "cheap", "local-only"]
    supports_local = True
    supports_remote = False
    capabilities = ["text", "planning", "review"]

    def complete(self, request: ProviderRequest, settings: EffectiveSettings) -> ProviderResponse:
        prompt = request.prompt.strip()
        content = (
            "Mock provider summary: "
            + (prompt[:240] + "..." if len(prompt) > 240 else prompt)
        )
        return ProviderResponse(
            provider_name=self.name,
            model_name=request.model_name or settings.model,
            content=content,
            raw_response={"mode": "mock"},
        )
