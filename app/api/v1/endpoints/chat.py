from __future__ import annotations

from uuid import uuid4

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from app.api.dependencies import get_gateway
from app.core.errors import BadRequestError
from app.core.security import require_api_key
from app.routing.gateway import GatewayService

router = APIRouter()


@router.post("/chat/completions", dependencies=[Depends(require_api_key)])
async def chat_completions(
    request: Request, gateway: GatewayService = Depends(get_gateway)
) -> JSONResponse:
    try:
        body = await request.json()
    except Exception as exc:
        raise BadRequestError("Request body must be valid JSON") from exc
    if not isinstance(body, dict):
        raise BadRequestError("Request body must be a JSON object")

    request_id = uuid4().hex
    result = await gateway.complete(request_id, body, dict(request.headers))
    return JSONResponse(content=result, status_code=200)
