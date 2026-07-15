from __future__ import annotations

import secrets

from fastapi import Request

from app.core.errors import AuthError


async def require_api_key(request: Request) -> None:
    header = request.headers.get("Authorization", "")
    prefix = "Bearer "
    if not header.startswith(prefix):
        raise AuthError()
    provided = header[len(prefix):]
    expected = request.app.state.config.api_key
    if not secrets.compare_digest(provided, expected):
        raise AuthError()
