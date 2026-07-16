from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse

from app.api.dashboard_renderer import render_dashboard
from app.api.dependencies import get_dashboard_service
from app.core.security import require_api_key
from app.dashboard import DashboardService

router = APIRouter()

@router.get("/dashboard", dependencies=[Depends(require_api_key)])
async def dashboard(
    service: DashboardService = Depends(get_dashboard_service),
) -> HTMLResponse:
    return HTMLResponse(
        content=render_dashboard(await service.snapshot()),
        headers={"Cache-Control": "no-store"},
    )
