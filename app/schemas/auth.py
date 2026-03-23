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


class LoginRequest(BaseModel):
    username: str = Field(min_length=3, max_length=100)
    password: str = Field(min_length=8, max_length=200)


class SetupRequest(LoginRequest):
    confirm_password: str = Field(min_length=8, max_length=200)
