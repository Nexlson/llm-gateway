from fastapi import APIRouter

from app.api.v1.endpoints import chat, models

router = APIRouter()
router.include_router(models.router)
router.include_router(chat.router)
