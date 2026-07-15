from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

logger = logging.getLogger("gateway")


def error_envelope(
    message: str, error_type: str, param: str | None = None, code: str | None = None
) -> dict:
    return {"error": {"message": message, "type": error_type, "param": param, "code": code}}


class GatewayError(Exception):
    def __init__(
        self, message: str, status_code: int, error_type: str,
        param: str | None = None, code: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.error_type = error_type
        self.param = param
        self.code = code


class AuthError(GatewayError):
    def __init__(self, message: str = "Missing or invalid API key") -> None:
        super().__init__(message, 401, "authentication_error")


class BadRequestError(GatewayError):
    def __init__(self, message: str, param: str | None = None) -> None:
        super().__init__(message, 400, "invalid_request_error", param=param)


def register_exception_handlers(app: FastAPI) -> None:
    from app.providers.base import ProviderError

    @app.exception_handler(ProviderError)
    async def _provider(_: Request, exc: ProviderError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=error_envelope(exc.message, exc.error_type, exc.param, exc.code),
        )

    @app.exception_handler(GatewayError)
    async def _gateway(_: Request, exc: GatewayError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=error_envelope(exc.message, exc.error_type, exc.param, exc.code),
        )

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled error")
        return JSONResponse(
            status_code=500,
            content=error_envelope("Internal server error", "api_error"),
        )
