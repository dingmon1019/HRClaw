from __future__ import annotations

import os
from abc import ABC, abstractmethod

from app.config.settings import AppSettings
from app.core.errors import ProviderError
from app.schemas.providers import ProviderRequest, ProviderResponse, ProviderStatus
from app.schemas.settings import EffectiveSettings


class BaseProvider(ABC):
    name = "base"
    description = "Abstract provider"

    def __init__(self, base_settings: AppSettings):
        self.base_settings = base_settings

    @abstractmethod
    def complete(self, request: ProviderRequest, settings: EffectiveSettings) -> ProviderResponse:
        raise NotImplementedError

    def status(self, settings: EffectiveSettings) -> ProviderStatus:
        return ProviderStatus(
            name=self.name,
            available=True,
            configured=self.is_configured(settings),
            description=self.description,
        )

    def is_configured(self, settings: EffectiveSettings) -> bool:
        return True

    @staticmethod
    def env_value(env_name: str) -> str:
        value = os.getenv(env_name, "").strip()
        if not value:
            raise ProviderError(f"Environment variable {env_name} is not set.")
        return value

