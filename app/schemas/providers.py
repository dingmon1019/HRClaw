from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ProviderRequest(BaseModel):
    provider_name: str | None = None
    model_name: str | None = None
    profile: str | None = None
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
    profiles: list[str] = Field(default_factory=list)
    supports_local: bool = False
    supports_remote: bool = True
    circuit_open: bool = False
    last_error: str | None = None


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
