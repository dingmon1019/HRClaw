from __future__ import annotations

import hmac
from datetime import UTC, datetime, timedelta

from app.config.settings import AppSettings
from app.core.database import Database
from app.core.errors import AuthorizationError
from app.core.utils import new_id, random_token, sha256_hex, utcnow_iso
from app.schemas.auth import CliTokenRecord
from app.services.auth_service import AuthService


class CliTokenService:
    def __init__(self, database: Database, auth_service: AuthService, base_settings: AppSettings):
        self.database = database
        self.auth_service = auth_service
        self.base_settings = base_settings

    def issue(
        self,
        *,
        username: str,
        password: str,
        purpose: str,
        ttl_seconds: int | None = None,
    ) -> tuple[str, CliTokenRecord]:
        user = self.auth_service.authenticate(username, password)
        now = datetime.now(UTC).replace(microsecond=0)
        raw_token = random_token(32)
        record = CliTokenRecord(
            id=new_id("clitoken"),
            user_id=user.id,
            username=user.username,
            purpose=purpose,
            created_at=now.isoformat(),
            expires_at=(now + timedelta(seconds=ttl_seconds or self.base_settings.cli_token_ttl_seconds)).isoformat(),
            last_used_at=None,
            revoked_at=None,
        )
        self.database.execute(
            """
            INSERT INTO cli_tokens(id, user_id, purpose, token_hash, created_at, expires_at, last_used_at, revoked_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.id,
                record.user_id,
                purpose,
                sha256_hex(raw_token),
                record.created_at,
                record.expires_at,
                record.last_used_at,
                record.revoked_at,
            ),
        )
        return raw_token, record

    def verify(self, token: str | None, *, purpose: str | None = None) -> CliTokenRecord:
        candidate = (token or "").strip()
        if not candidate:
            raise AuthorizationError("A short-lived CLI authentication token is required for this action.")
        token_hash = sha256_hex(candidate)
        rows = self.database.fetch_all(
            """
            SELECT cli_tokens.*, users.username
            FROM cli_tokens
            JOIN users ON users.id = cli_tokens.user_id
            WHERE cli_tokens.revoked_at IS NULL
            ORDER BY cli_tokens.created_at DESC
            """
        )
        now = datetime.now(UTC)
        for row in rows:
            if purpose and row["purpose"] != purpose:
                continue
            if datetime.fromisoformat(row["expires_at"]) <= now:
                continue
            if hmac.compare_digest(row["token_hash"], token_hash):
                self.database.execute(
                    "UPDATE cli_tokens SET last_used_at = ? WHERE id = ?",
                    (utcnow_iso(), row["id"]),
                )
                return CliTokenRecord(
                    id=row["id"],
                    user_id=row["user_id"],
                    username=row["username"],
                    purpose=row["purpose"],
                    created_at=row["created_at"],
                    expires_at=row["expires_at"],
                    last_used_at=row["last_used_at"],
                    revoked_at=row["revoked_at"],
                )
        raise AuthorizationError("A valid unexpired CLI authentication token is required for this action.")
