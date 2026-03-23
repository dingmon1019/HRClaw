from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.config.settings import AppSettings
from app.core.database import Database
from app.core.utils import new_id, random_token, utcnow_iso
from app.schemas.auth import SessionRecord, UserRecord


def _parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value)


class SessionService:
    def __init__(self, database: Database, base_settings: AppSettings):
        self.database = database
        self.base_settings = base_settings

    def now_utc(self) -> datetime:
        return datetime.now(UTC).replace(microsecond=0)

    def create(self, user: UserRecord, *, client_ip: str | None, user_agent: str | None) -> SessionRecord:
        now = self.now_utc()
        record = SessionRecord(
            id=new_id("session"),
            user_id=user.id,
            username=user.username,
            csrf_token=random_token(24),
            client_ip=client_ip,
            user_agent=user_agent,
            created_at=now.isoformat(),
            last_activity_at=now.isoformat(),
            recent_auth_at=now.isoformat(),
            expires_at=(now + timedelta(seconds=self.base_settings.session_max_age_seconds)).isoformat(),
            revoked_at=None,
        )
        self.database.execute(
            """
            INSERT INTO sessions(
                id, user_id, csrf_token, client_ip, user_agent, created_at, last_activity_at,
                recent_auth_at, expires_at, revoked_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.id,
                record.user_id,
                record.csrf_token,
                record.client_ip,
                record.user_agent,
                record.created_at,
                record.last_activity_at,
                record.recent_auth_at,
                record.expires_at,
                record.revoked_at,
            ),
        )
        return record

    def get_active(self, session_id: str | None) -> SessionRecord | None:
        if not session_id:
            return None
        row = self.database.fetch_one(
            """
            SELECT sessions.*, users.username
            FROM sessions
            JOIN users ON users.id = sessions.user_id
            WHERE sessions.id = ? AND sessions.revoked_at IS NULL
            """,
            (session_id,),
        )
        if row is None:
            return None
        record = self._row_to_record(row)
        if _parse_iso(record.expires_at) <= self.now_utc():
            self.revoke(session_id)
            return None
        return record

    def touch_activity(self, session_id: str) -> SessionRecord | None:
        now = utcnow_iso()
        self.database.execute(
            "UPDATE sessions SET last_activity_at = ? WHERE id = ? AND revoked_at IS NULL",
            (now, session_id),
        )
        return self.get_active(session_id)

    def mark_recent_auth(self, session_id: str) -> SessionRecord | None:
        now = utcnow_iso()
        self.database.execute(
            "UPDATE sessions SET recent_auth_at = ?, last_activity_at = ? WHERE id = ? AND revoked_at IS NULL",
            (now, now, session_id),
        )
        return self.get_active(session_id)

    def revoke(self, session_id: str | None) -> None:
        if not session_id:
            return
        self.database.execute(
            "UPDATE sessions SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
            (utcnow_iso(), session_id),
        )

    def is_idle_expired(self, record: SessionRecord) -> bool:
        return (
            self.now_utc() - _parse_iso(record.last_activity_at)
        ).total_seconds() > self.base_settings.session_idle_timeout_seconds

    def has_recent_auth(self, record: SessionRecord) -> bool:
        return (
            self.now_utc() - _parse_iso(record.recent_auth_at)
        ).total_seconds() <= self.base_settings.recent_auth_window_seconds

    @staticmethod
    def _row_to_record(row) -> SessionRecord:
        return SessionRecord(
            id=row["id"],
            user_id=row["user_id"],
            username=row["username"],
            csrf_token=row["csrf_token"],
            client_ip=row["client_ip"],
            user_agent=row["user_agent"],
            created_at=row["created_at"],
            last_activity_at=row["last_activity_at"],
            recent_auth_at=row["recent_auth_at"],
            expires_at=row["expires_at"],
            revoked_at=row["revoked_at"],
        )
