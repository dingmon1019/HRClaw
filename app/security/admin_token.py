from __future__ import annotations

from app.config.settings import AppSettings
from app.core.database import Database
from app.schemas.auth import CliTokenRecord
from app.services.auth_service import AuthService
from app.services.cli_token_service import CliTokenService


class AdminTokenService:
    def __init__(self, database: Database, auth_service: AuthService, base_settings: AppSettings):
        self.cli_tokens = CliTokenService(database, auth_service, base_settings)

    def issue(
        self,
        *,
        username: str,
        password: str,
        purpose: str,
        ttl_seconds: int | None = None,
    ) -> tuple[str, CliTokenRecord]:
        return self.cli_tokens.issue(
            username=username,
            password=password,
            purpose=purpose,
            ttl_seconds=ttl_seconds,
        )

    def verify(self, provided_token: str | None, *, purpose: str | None = None) -> CliTokenRecord:
        return self.cli_tokens.verify(provided_token, purpose=purpose)
