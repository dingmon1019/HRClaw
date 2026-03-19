from __future__ import annotations

from pydantic import BaseModel, Field

from app.schemas.actions import RuntimeMode


class EffectiveSettings(BaseModel):
    app_name: str
    runtime_mode: RuntimeMode
    provider: str
    fallback_provider: str | None = None
    model: str
    base_url: str | None = None
    api_key_env: str
    generic_http_endpoint: str | None = None
    provider_timeout_seconds: float
    provider_max_retries: int
    json_audit_enabled: bool
    allowed_filesystem_roots: list[str] = Field(default_factory=list)
    allowed_http_hosts: list[str] = Field(default_factory=list)
    powershell_allowlist: list[str] = Field(default_factory=list)


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
    json_audit_enabled: bool
    allowed_filesystem_roots: str
    allowed_http_hosts: str
    powershell_allowlist: str
