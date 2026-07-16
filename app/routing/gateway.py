from __future__ import annotations

import time

from app.core.errors import BadRequestError
from app.core.logging import log_request
from app.cost.tracker import CostTracker
from app.repositories.requests_repo import RequestRecord, RequestsRepository
from app.routing.executor import ExecutionOutcome, FallbackExecutor
from app.routing.router import Resolution, Router


class GatewayService:
    def __init__(
        self, router: Router, executor: FallbackExecutor,
        cost: CostTracker, repo: RequestsRepository,
    ) -> None:
        self._router = router
        self._executor = executor
        self._cost = cost
        self._repo = repo

    async def complete(self, request_id: str, body: dict, headers: dict[str, str]) -> dict:
        requested = body.get("model")
        if not requested:
            raise BadRequestError("Missing required field 'model'", param="model")

        resolution = await self._router.route(body, headers)
        started = time.monotonic()
        outcome = await self._executor.execute(body, resolution)
        latency_ms = int((time.monotonic() - started) * 1000)

        if outcome.response is not None:
            await self._record(request_id, resolution, outcome,
                               status=outcome.response.status_code,
                               input_tokens=outcome.response.input_tokens,
                               output_tokens=outcome.response.output_tokens,
                               latency_ms=latency_ms)
            return outcome.response.body

        assert outcome.error is not None
        await self._record(request_id, resolution, outcome,
                           status=outcome.error.status_code,
                           input_tokens=0, output_tokens=0, latency_ms=latency_ms)
        raise outcome.error

    async def _record(
        self, request_id: str, resolution: Resolution, outcome: ExecutionOutcome,
        status: int, input_tokens: int, output_tokens: int, latency_ms: int,
    ) -> None:
        if outcome.fallback_hops > 0:
            route_stage = "fallback"
            final_id = f"{outcome.provider}/{outcome.model}"
            route_reason = (
                f"{resolution.route_reason}"
                f"|fallback:{outcome.fallback_from}->{final_id}"
                f"|hops={outcome.fallback_hops}"
            )
        else:
            route_stage = resolution.route_stage
            route_reason = resolution.route_reason

        cost = self._cost.compute(outcome.model, input_tokens, output_tokens)
        record = RequestRecord(
            id=request_id, ts=int(time.time()), pool=resolution.pool,
            provider=outcome.provider, model=outcome.model,
            route_stage=route_stage, route_reason=route_reason,
            input_tokens=input_tokens, output_tokens=output_tokens, cost_usd=cost,
            latency_ms=latency_ms, status=status, fallback_from=outcome.fallback_from,
        )
        await self._repo.insert(record)
        log_request({
            "request_id": request_id, "pool": resolution.pool,
            "provider": outcome.provider, "model": outcome.model,
            "route_stage": route_stage, "route_reason": route_reason,
            "input_tokens": input_tokens, "output_tokens": output_tokens,
            "cost_usd": cost, "latency_ms": latency_ms, "status": status,
            "fallback_hops": outcome.fallback_hops, "fallback_from": outcome.fallback_from,
        })
