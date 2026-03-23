from __future__ import annotations

from typing import Iterable
from urllib.parse import urlparse

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse


class LocalhostSecurityMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, trusted_hosts: Iterable[str], max_request_size_bytes: int):
        super().__init__(app)
        self.trusted_hosts = {host.lower() for host in trusted_hosts}
        self.max_request_size_bytes = max_request_size_bytes

    async def dispatch(self, request: Request, call_next):
        host_header = request.headers.get("host", "")
        host = host_header.split(":", 1)[0].lower()
        if self.trusted_hosts and host not in self.trusted_hosts:
            return PlainTextResponse("Host header is not allowed.", status_code=400)

        content_length = request.headers.get("content-length")
        if content_length:
            try:
                if int(content_length) > self.max_request_size_bytes:
                    return PlainTextResponse("Request payload is too large.", status_code=413)
            except ValueError:
                return PlainTextResponse("Invalid content-length header.", status_code=400)

        if request.method.upper() in {"POST", "PUT", "PATCH", "DELETE"}:
            origin = request.headers.get("origin")
            if origin:
                parsed_origin = urlparse(origin)
                origin_host = (parsed_origin.hostname or "").lower()
                if origin_host not in self.trusted_hosts:
                    return PlainTextResponse("Origin is not allowed.", status_code=403)
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self'; "
            "style-src 'self'; "
            "img-src 'self' data:; "
            "font-src 'self'; "
            "connect-src 'self'; "
            "object-src 'none'; "
            "base-uri 'self'; "
            "frame-ancestors 'none'; "
            "form-action 'self'"
        )
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Cache-Control"] = "no-store"
        return response
