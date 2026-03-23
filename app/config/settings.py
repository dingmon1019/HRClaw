from __future__ import annotations

from functools import lru_cache
import os
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def _default_runtime_state_root(app_slug: str = "WinAgentRuntime") -> Path:
    local_appdata = os.getenv("LOCALAPPDATA", "").strip()
    if local_appdata:
        return Path(local_appdata) / app_slug
    return Path.home() / f".{app_slug.lower()}"


def _resolve_under(base_dir: Path, value: Path | None, fallback_name: str) -> Path:
    candidate = value or Path(fallback_name)
    if candidate.is_absolute():
        return candidate.resolve()
    return (base_dir / candidate).resolve()


class AppSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app_name: str = "Win Agent Runtime"
    app_slug: str = "WinAgentRuntime"
    host: str = "127.0.0.1"
    port: int = 8000
    log_level: str = "info"

    runtime_state_root: Path | None = None
    data_dir: Path | None = None
    secrets_dir: Path | None = None
    logs_dir: Path | None = None

    database_path: Path = Path("win_agent_runtime.db")
    audit_log_path: Path = Path("audit.jsonl")
    json_audit_enabled: bool = True
    workspace_root: Path = Path("workspace")
    protected_blob_dir: Path = Path("protected_blobs")
    local_protection_mode: str = "dpapi"
    history_retention_days: int = Field(default=30, ge=1, le=3650)

    runtime_mode: str = "safe"
    allowed_filesystem_roots: str = "workspace"
    allowed_http_hosts: str = "127.0.0.1,localhost"
    allowed_http_schemes: str = "http,https"
    allowed_http_ports: str = "80,443,8000,8080,11434"
    allow_http_private_network: bool = False
    http_follow_redirects: bool = False
    http_timeout_seconds: float = Field(default=10.0, ge=1.0, le=120.0)
    http_max_response_bytes: int = Field(default=1_048_576, ge=1024, le=10_485_760)
    filesystem_max_read_bytes: int = Field(default=262_144, ge=4096, le=5_242_880)
    enable_outlook_connector: bool = False
    enable_system_connector: bool = True

    provider: str = "mock"
    fallback_provider: str = "mock"
    model: str = "mock-model"
    base_url: str | None = None
    api_key_env: str = "OPENAI_API_KEY"
    generic_http_endpoint: str | None = None
    provider_timeout_seconds: float = Field(default=30.0, ge=1.0, le=300.0)
    provider_max_retries: int = Field(default=2, ge=0, le=10)
    provider_circuit_breaker_threshold: int = Field(default=3, ge=1, le=20)
    provider_circuit_breaker_seconds: int = Field(default=60, ge=5, le=3600)
    summary_profile: str = "fast"
    planning_profile: str = "strong"
    fast_provider: str | None = None
    cheap_provider: str | None = None
    strong_provider: str | None = None
    local_provider: str | None = "mock"
    privacy_provider: str | None = "mock"
    provider_allowed_hosts: str = (
        "api.openai.com,api.anthropic.com,generativelanguage.googleapis.com,localhost,127.0.0.1"
    )
    allow_provider_private_network: bool = False
    allow_restricted_provider_egress: bool = False

    anthropic_api_key_env: str = "ANTHROPIC_API_KEY"
    gemini_api_key_env: str = "GEMINI_API_KEY"
    session_secret: str | None = None
    session_secret_path: Path = Path("session_secret.bin")
    session_cookie_name: str = "win_agent_session"
    session_max_age_seconds: int = Field(default=3600, ge=300, le=86400)
    session_idle_timeout_seconds: int = Field(default=900, ge=60, le=86400)
    recent_auth_window_seconds: int = Field(default=300, ge=30, le=3600)
    cli_token_ttl_seconds: int = Field(default=900, ge=60, le=86400)
    secure_cookies: bool = False
    max_request_size_bytes: int = Field(default=1_048_576, ge=4096, le=10_485_760)
    trusted_hosts: str = "127.0.0.1,localhost"
    login_rate_limit_attempts: int = Field(default=5, ge=1, le=100)
    login_rate_limit_window_seconds: int = Field(default=60, ge=10, le=3600)
    approval_rate_limit_attempts: int = Field(default=20, ge=1, le=200)
    approval_rate_limit_window_seconds: int = Field(default=60, ge=10, le=3600)
    worker_poll_interval_seconds: float = Field(default=2.0, ge=0.1, le=60.0)
    worker_lease_seconds: int = Field(default=45, ge=5, le=3600)
    worker_max_attempts: int = Field(default=3, ge=1, le=20)

    @property
    def project_root(self) -> Path:
        return Path(__file__).resolve().parents[2]

    @property
    def resolved_runtime_state_root(self) -> Path:
        base = self.runtime_state_root or _default_runtime_state_root(self.app_slug)
        return base.resolve()

    @property
    def resolved_data_dir(self) -> Path:
        return _resolve_under(self.resolved_runtime_state_root, self.data_dir, "data")

    @property
    def resolved_secrets_dir(self) -> Path:
        return _resolve_under(self.resolved_runtime_state_root, self.secrets_dir, "secrets")

    @property
    def resolved_logs_dir(self) -> Path:
        return _resolve_under(self.resolved_runtime_state_root, self.logs_dir, "logs")

    @property
    def resolved_database_path(self) -> Path:
        return _resolve_under(self.resolved_data_dir, self.database_path, "win_agent_runtime.db")

    @property
    def resolved_audit_log_path(self) -> Path:
        return _resolve_under(self.resolved_logs_dir, self.audit_log_path, "audit.jsonl")

    @property
    def resolved_workspace_root(self) -> Path:
        return _resolve_under(self.resolved_runtime_state_root, self.workspace_root, "workspace")

    @property
    def resolved_session_secret_path(self) -> Path:
        return _resolve_under(self.resolved_secrets_dir, self.session_secret_path, "session_secret.bin")

    @property
    def resolved_protected_blob_dir(self) -> Path:
        return _resolve_under(self.resolved_secrets_dir, self.protected_blob_dir, "protected_blobs")


@lru_cache(maxsize=1)
def get_app_settings() -> AppSettings:
    return AppSettings()
