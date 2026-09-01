"""API key authentication middleware for the assistant server."""

from __future__ import annotations

import logging
import os
import secrets

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

logger = logging.getLogger(__name__)


class AuthMiddleware(BaseHTTPMiddleware):
    """Validates ``Authorization: Bearer <key>`` on ``/v1/*`` and ``/api/*`` routes.

    Webhook routes and health checks are exempt â€” they use
    per-channel signature verification instead.
    """

    def __init__(self, app, api_key: str = "") -> None:  # noqa: ANN001
        super().__init__(app)
        self._api_key = api_key or os.environ.get("BUJJI_API_KEY", "")

    async def dispatch(self, request: Request, call_next):  # noqa: ANN001
        if self._api_key and self._requires_auth(request.url.path):
            token = self._extract_token(request)
            if not token:
                return JSONResponse(
                    {"detail": "Missing API key"},
                    status_code=401,
                )
            # Constant-time comparison to avoid leaking the key via timing.
            if not secrets.compare_digest(token, self._api_key):
                return JSONResponse(
                    {"detail": "Invalid API key"},
                    status_code=401,
                )
        return await call_next(request)

    @staticmethod
    def _extract_token(request: Request) -> str:
        """Read the key from any supported location.

        Accepts ``Authorization: Bearer <key>`` (programmatic clients),
        ``X-API-Key`` header, or ``?api_key=`` query param (the Android
        companion scheme). Returns ``""`` if none present.
        """
        auth = request.headers.get("Authorization", "")
        scheme, _, value = auth.partition(" ")
        if scheme.lower() == "bearer" and value:
            return value
        header_key = request.headers.get("X-API-Key", "")
        if header_key:
            return header_key
        return request.query_params.get("api_key", "")

    @staticmethod
    def _requires_auth(path: str) -> bool:
        """Protect API routes and operational metrics; leave the UI/health open.

        ``/metrics`` exposes request/token counters that should not be readable
        by unauthenticated clients, so it is gated alongside ``/v1`` and
        ``/api``. ``/health`` stays open for liveness probes.
        """
        return (
            path.startswith("/v1/")
            or path.startswith("/api/")
            or path == "/metrics"
            or path.startswith("/metrics/")
        )



def generate_api_key() -> str:
    """Generate a new API key with ``oj_sk_`` prefix."""
    return f"oj_sk_{secrets.token_urlsafe(32)}"


def check_bind_safety(host: str, *, api_key: str) -> None:
    """Refuse to bind non-loopback without an API key.

    Raises ``SystemExit`` if *host* is not a loopback address and
    *api_key* is empty.
    """
    import ipaddress
    import sys

    try:
        is_loop = ipaddress.ip_address(host).is_loopback
    except ValueError:
        # An empty host means "bind to all interfaces" (like 0.0.0.0), which is
        # NOT loopback and must require a key.
        is_loop = host == "localhost"

    if not is_loop and not api_key:
        logger.error(
            "Binding to %s requires BUJJI_API_KEY to be set. "
            "Run: assistant auth generate-key",
            host,
        )
        sys.exit(1)


def websocket_authorized(websocket, expected_key: str) -> bool:  # noqa: ANN001
    """Return ``True`` if a WebSocket connection presents the expected key.

    ``AuthMiddleware`` is a ``BaseHTTPMiddleware`` and never sees WebSocket
    upgrade requests, so streaming endpoints must check the token themselves
    in the handshake before calling ``websocket.accept()``.

    When *expected_key* is empty, authentication is disabled (the loopback /
    local-only default, matching :class:`AuthMiddleware`) and all connections
    are allowed. The token may be supplied either as a ``?token=`` query
    parameter â€” browsers cannot set headers on a WebSocket handshake â€” or via
    an ``Authorization: Bearer <key>`` header for programmatic clients.
    """
    if not expected_key:
        return True
    token = websocket.query_params.get("token", "")
    if not token:
        auth = websocket.headers.get("authorization", "")
        scheme, _, value = auth.partition(" ")
        if scheme.lower() == "bearer":
            token = value
    if not token:
        return False
    return secrets.compare_digest(token, expected_key)
