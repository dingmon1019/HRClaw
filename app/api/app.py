from __future__ import annotations

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.api.routes import router
from app.config.settings import get_app_settings
from app.core.container import AppContainer


def create_app() -> FastAPI:
    settings = get_app_settings()
    app = FastAPI(title=settings.app_name)
    app.state.container = AppContainer(settings)
    app.state.templates = Jinja2Templates(directory=str(settings.project_root / "ui"))
    app.mount("/static", StaticFiles(directory=str(settings.project_root / "ui" / "static")), name="static")
    app.include_router(router)
    return app
