from __future__ import annotations

import hmac
import os

from app.config.settings import AppSettings
from app.core.errors import AuthorizationError
from app.core.utils import ensure_parent_dir, random_token


class AdminTokenService:
    def __init__(self, base_settings: AppSettings):
        self.base_settings = base_settings

    def ensure_token(self) -> str:
        env_token = os.getenv("WIN_AGENT_ADMIN_TOKEN", "").strip()
        if env_token:
            return env_token
        token_path = self.base_settings.resolved_admin_token_path
        ensure_parent_dir(token_path)
        if token_path.exists():
            return token_path.read_text(encoding="utf-8").strip()
        token = random_token(24)
        token_path.write_text(token, encoding="utf-8")
        return token

    def verify(self, provided_token: str | None) -> None:
        expected = self.ensure_token()
        candidate = (provided_token or "").strip()
        if not candidate or not hmac.compare_digest(candidate, expected):
            raise AuthorizationError("A valid local admin token is required for this CLI action.")
