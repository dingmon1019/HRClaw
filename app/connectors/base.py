from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class BaseConnector(ABC):
    name = "base"
    description = "Abstract connector"

    @abstractmethod
    def healthcheck(self) -> dict[str, Any]:
        raise NotImplementedError

    def collect(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {}

    @abstractmethod
    def execute(self, action_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

