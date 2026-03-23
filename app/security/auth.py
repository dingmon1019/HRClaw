from __future__ import annotations

from fastapi import Request

from app.schemas.auth import SessionUser, UserRecord


SESSION_USER_ID = "user_id"
SESSION_USERNAME = "username"
SESSION_RECENT_AUTH_AT = "recent_auth_at"
SESSION_AUTHENTICATED_AT = "authenticated_at"


def login_session(request: Request, user: UserRecord, authenticated_at: int) -> None:
    request.session[SESSION_USER_ID] = user.id
    request.session[SESSION_USERNAME] = user.username
    request.session[SESSION_AUTHENTICATED_AT] = authenticated_at
    request.session[SESSION_RECENT_AUTH_AT] = authenticated_at


def logout_session(request: Request) -> None:
    request.session.clear()


def read_session_user(request: Request, recent_auth: bool) -> SessionUser | None:
    user_id = request.session.get(SESSION_USER_ID)
    username = request.session.get(SESSION_USERNAME)
    if not user_id or not username:
        return None
    return SessionUser(id=user_id, username=username, recent_auth=recent_auth)


def mark_recent_auth(request: Request, timestamp: int) -> None:
    request.session[SESSION_RECENT_AUTH_AT] = timestamp
