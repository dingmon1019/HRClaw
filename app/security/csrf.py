from __future__ import annotations

from fastapi import Request

from app.core.errors import CsrfError
from app.core.utils import random_token


SESSION_CSRF_TOKEN = "csrf_token"


def ensure_csrf_token(request: Request) -> str:
    token = request.session.get(SESSION_CSRF_TOKEN)
    if not token:
        token = random_token(24)
        request.session[SESSION_CSRF_TOKEN] = token
    return token


async def validate_csrf(request: Request) -> None:
    session_token = ensure_csrf_token(request)
    request_token = request.headers.get("x-csrf-token")
    if request_token is None:
        form = await request.form()
        request_token = form.get("csrf_token")
    if not request_token or request_token != session_token:
        raise CsrfError("CSRF token is missing or invalid.")

