from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ProviderRequest(BaseModel):
    provider_name: str | None = None
    model_name: str | None = None
    prompt: str
    system_prompt: str | None = None
    response_format: str = "text"


class ProviderResponse(BaseModel):
    provider_name: str
    model_name: str
    content: str
    raw_response: dict[str, Any] = Field(default_factory=dict)


class ProviderStatus(BaseModel):
    name: str
    available: bool
    configured: bool
    description: str


class ProviderTestRequest(BaseModel):
    provider_name: str | None = None
    model_name: str | None = None
    prompt: str = "Return a one-line readiness confirmation."


class ProviderTestResult(BaseModel):
    provider_name: str
    model_name: str
    ok: bool
    message: str
    content: str | None = None

