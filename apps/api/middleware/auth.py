"""JWT bearer authentication middleware."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import jwt
from fastapi.responses import JSONResponse
from jwt import ExpiredSignatureError, InvalidTokenError
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from apps.api.config import Settings


class AuthMiddleware(BaseHTTPMiddleware):
    """Decode RS256 JWTs for protected API routes and attach user context."""

    def __init__(self, app, *, settings: Settings) -> None:
        super().__init__(app)
        self._settings = settings

    async def dispatch(self, request: Request, call_next):
        if request.url.path in {"/health", "/ready"}:
            return await call_next(request)
        if not request.url.path.startswith("/api/v1"):
            return await call_next(request)

        authorization = request.headers.get("Authorization")
        if not authorization:
            return _auth_error("missing_token", "Missing bearer token")
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not token:
            return _auth_error("invalid_token", "Authorization header must use Bearer scheme")

        try:
            payload = jwt.decode(
                token,
                self._settings.jwt_public_key,
                algorithms=["RS256"],
                options={"require": ["exp"]},
            )
        except ExpiredSignatureError:
            return _auth_error("token_expired", "Bearer token has expired")
        except InvalidTokenError:
            return _auth_error("invalid_token", "Bearer token is invalid")

        raw_user_uuid = payload.get("user_uuid") or payload.get("sub")
        if raw_user_uuid is None:
            return _auth_error("invalid_token", "Bearer token is missing user identity")

        try:
            request.state.user_uuid = uuid.UUID(str(raw_user_uuid))
        except ValueError:
            return _auth_error("invalid_token", "Bearer token user identity is malformed")

        request.state.token_payload = payload
        request.state.token_expiry = datetime.fromtimestamp(payload["exp"], tz=UTC)
        return await call_next(request)


def _auth_error(code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=401,
        content={"detail": {"code": code, "message": message}},
        headers={"WWW-Authenticate": "Bearer"},
    )
