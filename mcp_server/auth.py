"""Bearer-token auth middleware for the DJ Treta MCP SSE server.

Token read from env var `DJTRETA_MCP_TOKEN`. All routes except /health
require `Authorization: Bearer <token>`. Missing/wrong token → 401.
"""
from __future__ import annotations

import os

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse


PUBLIC_PATHS = {"/health"}


class BearerAuthMiddleware(BaseHTTPMiddleware):
    """Require Authorization: Bearer <DJTRETA_MCP_TOKEN> on non-public routes."""

    def __init__(self, app, token: str | None = None):
        super().__init__(app)
        self._token = token or os.environ.get("DJTRETA_MCP_TOKEN", "")
        if not self._token:
            # Refuse to run without a token — explicit failure is better
            # than silently accepting all traffic.
            raise RuntimeError(
                "DJTRETA_MCP_TOKEN env var is required for the MCP server"
            )

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        # Strip trailing slashes for matching
        normalised = path.rstrip("/") or "/"
        if normalised in PUBLIC_PATHS:
            return await call_next(request)

        header = request.headers.get("authorization", "")
        prefix = "Bearer "
        if not header.startswith(prefix):
            return JSONResponse(
                {"error": "unauthorized", "reason": "missing bearer token"},
                status_code=401,
            )
        supplied = header[len(prefix):].strip()
        if supplied != self._token:
            return JSONResponse(
                {"error": "unauthorized", "reason": "invalid bearer token"},
                status_code=401,
            )
        return await call_next(request)
