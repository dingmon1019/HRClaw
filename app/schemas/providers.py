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
    data_classification: str = "external-ok"
    task_type: str | None = None
    correlation_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ProviderResponse(BaseModel):
    provider_name: str
    model_name: str
    content: str
    raw_response: dict[str, Any] = Field(default_factory=dict)


class ProviderStatus(BaseModel):
    name: str
    available: bool
    configured: bool
    enabled: bool = True
    description: str
    profiles: list[str] = Field(default_factory=list)
    supports_local: bool = False
    supports_remote: bool = True
    circuit_open: bool = False
    last_error: str | None = None
    healthy: bool = True
    capabilities: list[str] = Field(default_factory=list)
    allowed_hosts: list[str] = Field(default_factory=list)
    last_checked_at: str | None = None
    base_url: str | None = None
    generic_http_endpoint: str | None = None
    api_key_env: str | None = None
    default_model: str | None = None
    auth_source: str = "env"
    credential_target: str | None = None
    credential_available: bool = False
    cost_tier: str = "standard"
    latency_tier: str = "standard"
    privacy_tier: str = "standard"
    privacy_posture: str = "external-egress"
    egress_posture: str = "inherits global allowlist"
    destination_summary: str = ""
    endpoint_locality: str = "remote-endpoint"
    model_inventory: list[dict[str, Any]] = Field(default_factory=list)
    budget_limit_units: float | None = None
    budget_used_units: float = 0.0
    budget_exhausted: bool = False
    success_count: int = 0
    failure_count: int = 0
    last_score: float = 0.0
    last_routing_reason: str | None = None
    latency_ewma_ms: float = 0.0
    success_rate: float = 0.0
    consecutive_failures: int = 0
    last_error_category: str | None = None


class ProviderConfigRecord(BaseModel):
    provider_name: str
    enabled: bool = True
    base_url: str | None = None
    generic_http_endpoint: str | None = None
    api_key_env: str | None = None
    default_model: str | None = None
    allowed_hosts: list[str] = Field(default_factory=list)
    auth_source: str = "env"
    credential_target: str | None = None
    cost_tier: str = "standard"
    latency_tier: str = "standard"
    privacy_tier: str = "standard"
    budget_limit_units: float | None = None
    updated_at: str | None = None


class ProviderConfigUpdate(BaseModel):
    provider_name: str
    enabled: bool = True
    base_url: str | None = None
    generic_http_endpoint: str | None = None
    api_key_env: str | None = None
    default_model: str | None = None
    allowed_hosts: list[str] = Field(default_factory=list)
    auth_source: str = "env"
    credential_target: str | None = None
    cost_tier: str = "standard"
    latency_tier: str = "standard"
    privacy_tier: str = "standard"
    budget_limit_units: float | None = None


class ProviderTestRequest(BaseModel):
    provider_name: str | None = None
    model_name: str | None = None
    prompt: str = "Return a one-line readiness confirmation."
    data_classification: str = "external-ok"


class ProviderTestResult(BaseModel):
    provider_name: str
    model_name: str
    ok: bool
    message: str
    content: str | None = None
