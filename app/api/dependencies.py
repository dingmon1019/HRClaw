from __future__ import annotations

from fastapi import Request
from fastapi.templating import Jinja2Templates

from app.core.container import AppContainer


def get_container(request: Request) -> AppContainer:
    return request.app.state.container


def get_templates(request: Request) -> Jinja2Templates:
    return request.app.state.templates

