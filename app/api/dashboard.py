from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse

from app.core.security import require_api_key

router = APIRouter()

_STUB = "<html><body><h1>Gateway dashboard</h1><p>Coming in M5.</p></body></html>"


@router.get("/dashboard", dependencies=[Depends(require_api_key)])
async def dashboard() -> HTMLResponse:
    return HTMLResponse(content=_STUB)
