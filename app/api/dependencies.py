from __future__ import annotations

from fastapi import Request

from app.core.config import AppConfig
from app.dashboard import DashboardService
from app.routing.gateway import GatewayService


def get_config(request: Request) -> AppConfig:
    return request.app.state.config


def get_gateway(request: Request) -> GatewayService:
    return request.app.state.gateway


def get_dashboard_service(request: Request) -> DashboardService:
    return request.app.state.dashboard
