from __future__ import annotations

import time

from app.core.errors import BadRequestError
from app.core.logging import log_request
from app.cost.tracker import CostTracker
from app.providers.base import ProviderAdapter, ProviderError
from app.repositories.requests_repo import RequestRecord, RequestsRepository
from app.routing.pools import PoolResolver, Resolution


class GatewayService:
    def __init__(
        self, resolver: PoolResolver, registry: dict[str, ProviderAdapter],
        cost: CostTracker, repo: RequestsRepository,
    ) -> None:
        self._resolver = resolver
        self._registry = registry
        self._cost = cost
        self._repo = repo

    async def complete(self, request_id: str, body: dict) -> dict:
        requested = body.get("model")
        if not requested:
            raise BadRequestError("Missing required field 'model'", param="model")

        resolution = self._resolver.resolve(requested)
        adapter = self._registry[resolution.provider]
        started = time.monotonic()
        try:
            resp = await adapter.chat_completion(body, resolution.model)
        except ProviderError as exc:
            await self._record(request_id, resolution, exc.status_code, 0, 0,
                               int((time.monotonic() - started) * 1000))
            raise
        latency_ms = int((time.monotonic() - started) * 1000)
        await self._record(request_id, resolution, resp.status_code,
                           resp.input_tokens, resp.output_tokens, latency_ms)
        return resp.body

    async def _record(
        self, request_id: str, resolution: Resolution, status: int,
        input_tokens: int, output_tokens: int, latency_ms: int,
    ) -> None:
        cost = self._cost.compute(resolution.model, input_tokens, output_tokens)
        record = RequestRecord(
            id=request_id, ts=int(time.time()), pool=resolution.pool,
            provider=resolution.provider, model=resolution.model,
            route_stage=resolution.route_stage, route_reason=resolution.route_reason,
            input_tokens=input_tokens, output_tokens=output_tokens, cost_usd=cost,
            latency_ms=latency_ms, status=status, fallback_from=None,
        )
        await self._repo.insert(record)
        log_request({
            "request_id": request_id, "pool": resolution.pool,
            "model": resolution.model, "route_stage": resolution.route_stage,
            "input_tokens": input_tokens, "output_tokens": output_tokens,
            "cost_usd": cost, "latency_ms": latency_ms, "status": status,
            "fallback_hops": 0,
        })
