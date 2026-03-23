from __future__ import annotations

from pydantic import BaseModel, Field


class UserRecord(BaseModel):
    id: str
    username: str
    is_active: bool
    created_at: str
    updated_at: str


class SessionUser(BaseModel):
    id: str
    username: str
    recent_auth: bool


class SessionRecord(BaseModel):
    id: str
    user_id: str
    username: str
    csrf_token: str
    client_ip: str | None = None
    user_agent: str | None = None
    created_at: str
    last_activity_at: str
    recent_auth_at: str
    expires_at: str
    revoked_at: str | None = None


class CliTokenRecord(BaseModel):
    id: str
    user_id: str
    username: str
    purpose: str
    created_at: str
    expires_at: str
    last_used_at: str | None = None
    revoked_at: str | None = None


class LoginRequest(BaseModel):
    username: str = Field(min_length=3, max_length=100)
    password: str = Field(min_length=8, max_length=200)


class SetupRequest(LoginRequest):
    confirm_password: str = Field(min_length=8, max_length=200)


class CliTokenIssueRequest(LoginRequest):
    purpose: str = Field(min_length=3, max_length=100)
