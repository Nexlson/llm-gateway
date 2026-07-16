from __future__ import annotations

import os
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI

from app.api.dashboard import router as dashboard_router
from app.api.v1.router import router as v1_router
from app.core.config import AppConfig, load_config
from app.core.database import Database
from app.core.errors import register_exception_handlers
from app.core.logging import configure_logging
from app.cost.tracker import CostTracker
from app.providers.registry import build_registry
from app.repositories.requests_repo import RequestsRepository
from app.routing.executor import FallbackExecutor
from app.routing.gateway import GatewayService
from app.routing.health import HealthTracker
from app.routing.pools import PoolResolver
from app.routing.router import Router
from app.routing.rules import RuleEngine


def create_app(config: AppConfig | None = None) -> FastAPI:
    if config is None:
        config = load_config(os.environ.get("GATEWAY_CONFIG", "config.yaml"))

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        configure_logging()
        db = Database(config.db_path)
        await db.connect()
        http_client = httpx.AsyncClient()
        registry = build_registry(config, http_client)
        rule_engine = RuleEngine(config.rules)
        pool_resolver = PoolResolver(config.pools, config.default_pool)
        router = Router(rule_engine, pool_resolver)
        health = HealthTracker(cooldown_seconds=config.cooldown_seconds)
        executor = FallbackExecutor(registry, pool_resolver, health)
        cost = CostTracker(config.prices)
        repo = RequestsRepository(db.connection)
        app.state.config = config
        app.state.db = db
        app.state.http_client = http_client
        app.state.gateway = GatewayService(router, executor, cost, repo)
        try:
            yield
        finally:
            await http_client.aclose()
            await db.disconnect()

    app = FastAPI(title="LLM Cost-Aware Routing Gateway", lifespan=lifespan)
    register_exception_handlers(app)

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"status": "ok"}

    app.include_router(v1_router, prefix="/v1")
    app.include_router(dashboard_router)
    return app


def __getattr__(name: str):
    # Lazily build the module-level `app` for `uvicorn app.main:app` without
    # loading config.yaml at import time (tests import create_app directly and
    # must not trigger config loading).
    if name == "app":
        return create_app()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
