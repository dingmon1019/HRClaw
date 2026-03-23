from __future__ import annotations

import hashlib
import hmac
import secrets
from time import time

from app.core.database import Database
from app.core.errors import AuthenticationError
from app.core.utils import new_id, utcnow_iso
from app.schemas.auth import UserRecord


class AuthService:
    HASH_ITERATIONS = 600_000

    def __init__(self, database: Database):
        self.database = database

    def has_users(self) -> bool:
        row = self.database.fetch_one("SELECT COUNT(*) AS count FROM users")
        return bool(row and row["count"] > 0)

    def create_initial_user(self, username: str, password: str) -> UserRecord:
        if self.has_users():
            raise AuthenticationError("Initial setup has already been completed.")
        return self.create_user(username, password)

    def create_user(self, username: str, password: str) -> UserRecord:
        user_id = new_id("user")
        now = utcnow_iso()
        password_hash = self.hash_password(password)
        self.database.execute(
            """
            INSERT INTO users(id, username, password_hash, is_active, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (user_id, username.strip().lower(), password_hash, 1, now, now),
        )
        return self.get_user_by_id(user_id)

    def authenticate(self, username: str, password: str) -> UserRecord:
        row = self.database.fetch_one(
            "SELECT * FROM users WHERE username = ? AND is_active = 1",
            (username.strip().lower(),),
        )
        if row is None or not self.verify_password(password, row["password_hash"]):
            raise AuthenticationError("Invalid username or password.")
        return self._row_to_user(row)

    def verify_current_password(self, user_id: str, password: str) -> None:
        row = self.database.fetch_one(
            "SELECT * FROM users WHERE id = ? AND is_active = 1",
            (user_id,),
        )
        if row is None or not self.verify_password(password, row["password_hash"]):
            raise AuthenticationError("Current password is incorrect.")

    def get_user_by_id(self, user_id: str) -> UserRecord:
        row = self.database.fetch_one("SELECT * FROM users WHERE id = ?", (user_id,))
        if row is None:
            raise AuthenticationError("User not found.")
        return self._row_to_user(row)

    @classmethod
    def hash_password(cls, password: str) -> str:
        salt = secrets.token_hex(16)
        derived = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), cls.HASH_ITERATIONS)
        return f"pbkdf2_sha256${cls.HASH_ITERATIONS}${salt}${derived.hex()}"

    @classmethod
    def verify_password(cls, password: str, password_hash: str) -> bool:
        algorithm, iterations_text, salt, expected = password_hash.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        derived = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            salt.encode("utf-8"),
            int(iterations_text),
        )
        return hmac.compare_digest(derived.hex(), expected)

    @staticmethod
    def now_epoch() -> int:
        return int(time())

    @staticmethod
    def _row_to_user(row) -> UserRecord:
        return UserRecord(
            id=row["id"],
            username=row["username"],
            is_active=bool(row["is_active"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
