from __future__ import annotations

from fastapi import Request

from app.schemas.auth import SessionRecord, SessionUser, UserRecord
from app.services.session_service import SessionService


SESSION_ID = "session_id"


def login_session(
    request: Request,
    user: UserRecord,
    *,
    session_service: SessionService,
    client_ip: str | None,
    user_agent: str | None,
) -> SessionRecord:
    request.session.clear()
    record = session_service.create(user, client_ip=client_ip, user_agent=user_agent)
    request.session[SESSION_ID] = record.id
    return record


def logout_session(request: Request, session_service: SessionService) -> None:
    session_id = request.session.get(SESSION_ID)
    session_service.revoke(session_id)
    request.session.clear()


def read_session_user(session_record: SessionRecord | None, *, recent_auth: bool) -> SessionUser | None:
    if session_record is None:
        return None
    return SessionUser(id=session_record.user_id, username=session_record.username, recent_auth=recent_auth)


def mark_recent_auth(request: Request, session_service: SessionService) -> SessionRecord | None:
    session_id = request.session.get(SESSION_ID)
    if not session_id:
        return None
    return session_service.mark_recent_auth(session_id)


def touch_session_activity(request: Request, session_service: SessionService) -> SessionRecord | None:
    session_id = request.session.get(SESSION_ID)
    if not session_id:
        return None
    return session_service.touch_activity(session_id)
