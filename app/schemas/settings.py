from __future__ import annotations

from pydantic import BaseModel, Field

from app.schemas.actions import RuntimeMode


class EffectiveSettings(BaseModel):
    app_name: str
    runtime_mode: RuntimeMode
    workspace_root: str
    provider: str
    fallback_provider: str | None = None
    model: str
    base_url: str | None = None
    api_key_env: str
    generic_http_endpoint: str | None = None
    provider_timeout_seconds: float
    provider_max_retries: int
    provider_circuit_breaker_threshold: int
    provider_circuit_breaker_seconds: int
    summary_profile: str
    planning_profile: str
    fast_provider: str | None = None
    cheap_provider: str | None = None
    strong_provider: str | None = None
    local_provider: str | None = None
    json_audit_enabled: bool
    session_max_age_seconds: int
    recent_auth_window_seconds: int
    max_request_size_bytes: int
    trusted_hosts: list[str] = Field(default_factory=list)
    allowed_http_schemes: list[str] = Field(default_factory=list)
    allowed_http_ports: list[int] = Field(default_factory=list)
    allow_http_private_network: bool = False
    http_follow_redirects: bool = False
    http_timeout_seconds: float = 10.0
    http_max_response_bytes: int = 1_048_576
    allowed_filesystem_roots: list[str] = Field(default_factory=list)
    allowed_http_hosts: list[str] = Field(default_factory=list)
    enable_outlook_connector: bool = False
    enable_system_connector: bool = True
    configured_secret_envs: list[str] = Field(default_factory=list)


class SettingsUpdate(BaseModel):
    runtime_mode: RuntimeMode
    provider: str
    fallback_provider: str | None = None
    model: str
    base_url: str | None = None
    api_key_env: str
    generic_http_endpoint: str | None = None
    provider_timeout_seconds: float
    provider_max_retries: int
    provider_circuit_breaker_threshold: int
    provider_circuit_breaker_seconds: int
    summary_profile: str
    planning_profile: str
    fast_provider: str | None = None
    cheap_provider: str | None = None
    strong_provider: str | None = None
    local_provider: str | None = None
    json_audit_enabled: bool
    session_max_age_seconds: int
    recent_auth_window_seconds: int
    max_request_size_bytes: int
    allowed_http_schemes: str
    allowed_http_ports: str
    allow_http_private_network: bool
    http_follow_redirects: bool
    http_timeout_seconds: float
    http_max_response_bytes: int
    allowed_filesystem_roots: str
    allowed_http_hosts: str
    enable_system_connector: bool
    enable_outlook_connector: bool


class SanitizedSettingsExport(BaseModel):
    runtime_mode: RuntimeMode
    workspace_root: str
    provider: str
    fallback_provider: str | None = None
    model: str
    base_url: str | None = None
    api_key_env: str
    generic_http_endpoint: str | None = None
    provider_timeout_seconds: float
    provider_max_retries: int
    provider_circuit_breaker_threshold: int
    provider_circuit_breaker_seconds: int
    summary_profile: str
    planning_profile: str
    fast_provider: str | None = None
    cheap_provider: str | None = None
    strong_provider: str | None = None
    local_provider: str | None = None
    json_audit_enabled: bool
    session_max_age_seconds: int
    recent_auth_window_seconds: int
    max_request_size_bytes: int
    allowed_http_schemes: list[str]
    allowed_http_ports: list[int]
    allow_http_private_network: bool
    http_follow_redirects: bool
    http_timeout_seconds: float
    http_max_response_bytes: int
    allowed_filesystem_roots: list[str]
    allowed_http_hosts: list[str]
    enable_system_connector: bool
    enable_outlook_connector: bool
