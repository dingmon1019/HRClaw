from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class AppSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app_name: str = "Win Agent Runtime"
    host: str = "127.0.0.1"
    port: int = 8000
    log_level: str = "info"

    database_path: Path = Path("data/win_agent_runtime.db")
    audit_log_path: Path = Path("data/audit/audit.jsonl")
    json_audit_enabled: bool = True

    runtime_mode: str = "safe"
    allowed_filesystem_roots: str = "."
    allowed_http_hosts: str = "127.0.0.1,localhost"
    powershell_allowlist: str = "Get-ChildItem,Get-Content,Test-Path,Resolve-Path,Get-Date"

    provider: str = "mock"
    fallback_provider: str = "mock"
    model: str = "mock-model"
    base_url: str | None = None
    api_key_env: str = "OPENAI_API_KEY"
    generic_http_endpoint: str | None = None
    provider_timeout_seconds: float = Field(default=30.0, ge=1.0, le=300.0)
    provider_max_retries: int = Field(default=2, ge=0, le=10)

    anthropic_api_key_env: str = "ANTHROPIC_API_KEY"
    gemini_api_key_env: str = "GEMINI_API_KEY"

    @property
    def project_root(self) -> Path:
        return Path(__file__).resolve().parents[2]

    @property
    def resolved_database_path(self) -> Path:
        return (self.project_root / self.database_path).resolve()

    @property
    def resolved_audit_log_path(self) -> Path:
        return (self.project_root / self.audit_log_path).resolve()


@lru_cache(maxsize=1)
def get_app_settings() -> AppSettings:
    return AppSettings()

