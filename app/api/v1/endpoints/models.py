from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.dependencies import get_config
from app.core.config import AppConfig
from app.core.security import require_api_key
from app.schemas.openai import models_list_response

router = APIRouter()


@router.get("/models", dependencies=[Depends(require_api_key)])
async def list_models(config: AppConfig = Depends(get_config)) -> dict:
    return models_list_response(config.pools.keys())
