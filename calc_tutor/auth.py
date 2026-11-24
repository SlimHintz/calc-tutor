"""Bearer-token ASGI middleware.

/mcp and /api/* require Authorization: Bearer <CALC_TUTOR_TOKEN or an OAuth
access token>. The web UI shell (/, /static/*), /health, and the OAuth
plumbing are public -- the UI holds no data until it has a token.
"""

from __future__ import annotations

import hmac
import logging

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from . import config, oauth
from .db import connect

_logger: logging.Logger | None = None
_EXEMPT_PREFIXES = ("/oauth/", "/.well-known/", "/static/")
_EXEMPT_PATHS = ("/", "/health", "/manifest.webmanifest", "/favicon.svg")


def log_auth_event(message: str, *args) -> None:
    global _logger
    if _logger is None:
        _logger = logging.getLogger("calc_tutor.auth")
        _logger.setLevel(logging.INFO)
        path = config.log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        h = logging.FileHandler(path)
        h.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
        _logger.addHandler(h)
    _logger.info(message, *args)


def token_ok(provided: str) -> bool:
    token = config.auth_token()
    if not provided or not token:
        return False
    if hmac.compare_digest(provided, token):
        return True
    with connect() as conn:
        return oauth.is_valid_access_token(conn, provided)


class BearerAuthMiddleware:
    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope["path"]
        if path in _EXEMPT_PATHS or path.startswith(_EXEMPT_PREFIXES):
            await self.app(scope, receive, send)
            return
        if not config.auth_token():
            await JSONResponse({"error": "CALC_TUTOR_TOKEN is not set"}, status_code=500)(
                scope, receive, send)
            return
        headers = dict(scope.get("headers") or [])
        auth = headers.get(b"authorization", b"").decode("latin-1")
        provided = auth[7:] if auth.startswith("Bearer ") else ""
        if not token_ok(provided):
            client = scope.get("client")
            log_auth_event("auth failure path=%s source_ip=%s", path,
                           client[0] if client else "unknown")
            hdrs = {}
            if config.public_url():
                hdrs["WWW-Authenticate"] = (f'Bearer resource_metadata="{config.public_url()}'
                                            '/.well-known/oauth-protected-resource"')
            await JSONResponse({"error": "unauthorized"}, status_code=401, headers=hdrs)(
                scope, receive, send)
            return
        await self.app(scope, receive, send)
