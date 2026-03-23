from __future__ import annotations

from urllib.parse import urlencode, urlparse

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.middleware.sessions import SessionMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.api.routes import router
from app.config.settings import get_app_settings
from app.core.container import AppContainer
from app.core.errors import (
    AuthenticationError,
    AuthorizationError,
    CsrfError,
    InvalidStateError,
    NotFoundError,
    RateLimitError,
)
from app.core.utils import ensure_parent_dir, random_token
from app.security.middleware import LocalhostSecurityMiddleware


def _load_session_secret(container: AppContainer) -> str:
    if container.base_settings.session_secret:
        return container.base_settings.session_secret
    secret_path = container.base_settings.resolved_session_secret_path
    ensure_parent_dir(secret_path)
    if secret_path.exists():
        return secret_path.read_text(encoding="utf-8").strip()
    secret = random_token(32)
    secret_path.write_text(secret, encoding="utf-8")
    return secret


def create_app(settings=None) -> FastAPI:
    settings = settings or get_app_settings()
    app = FastAPI(title=settings.app_name)
    app.state.container = AppContainer(settings)
    session_secret = _load_session_secret(app.state.container)
    app.add_middleware(
        SessionMiddleware,
        secret_key=session_secret,
        session_cookie=app.state.container.base_settings.session_cookie_name,
        max_age=app.state.container.base_settings.session_max_age_seconds,
        same_site="strict",
        https_only=app.state.container.base_settings.secure_cookies,
    )
    app.add_middleware(
        LocalhostSecurityMiddleware,
        trusted_hosts=app.state.container.settings_service.get_effective_settings().trusted_hosts,
        max_request_size_bytes=app.state.container.settings_service.get_effective_settings().max_request_size_bytes,
    )
    app.state.templates = Jinja2Templates(directory=str(settings.project_root / "ui"))
    app.mount("/static", StaticFiles(directory=str(settings.project_root / "ui" / "static")), name="static")
    app.include_router(router)

    def wants_html(request: Request) -> bool:
        accept = request.headers.get("accept", "")
        return request.url.path.startswith("/api/") is False and "text/html" in accept

    def redirect_with_error(request: Request, message: str, status_code: int = 303):
        referer = request.headers.get("referer")
        if referer:
            parsed = urlparse(referer)
            path = parsed.path or request.url.path
            if parsed.query:
                path = f"{path}?{parsed.query}"
        else:
            path = request.url.path if request.method == "GET" else "/"
        separator = "&" if "?" in path else "?"
        return RedirectResponse(url=f"{path}{separator}{urlencode({'error': message})}", status_code=status_code)

    @app.exception_handler(AuthenticationError)
    @app.exception_handler(AuthorizationError)
    @app.exception_handler(CsrfError)
    @app.exception_handler(RateLimitError)
    @app.exception_handler(InvalidStateError)
    async def handled_client_error(request: Request, exc: Exception):
        if wants_html(request):
            return redirect_with_error(request, str(exc))
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.exception_handler(NotFoundError)
    async def handled_not_found(request: Request, exc: NotFoundError):
        if wants_html(request):
            return redirect_with_error(request, str(exc))
        return JSONResponse({"detail": str(exc)}, status_code=404)

    @app.exception_handler(Exception)
    async def handled_unexpected_error(request: Request, exc: Exception):  # pragma: no cover
        if wants_html(request):
            return redirect_with_error(request, "Unexpected internal error.")
        return JSONResponse({"detail": "Unexpected internal error."}, status_code=500)

    return app
