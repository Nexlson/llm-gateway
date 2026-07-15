from __future__ import annotations

from dataclasses import dataclass

from app.core.config import PoolEntry


@dataclass
class Resolution:
    pool: str
    provider: str
    model: str
    route_stage: str
    route_reason: str


class PoolResolver:
    def __init__(self, pools: dict[str, list[PoolEntry]], default_pool: str) -> None:
        self._pools = pools
        self._default_pool = default_pool

    def resolve(self, requested_model: str) -> Resolution:
        if requested_model in self._pools:
            entry = self._pools[requested_model][0]
            return Resolution(
                pool=requested_model, provider=entry.provider, model=entry.model,
                route_stage="passthrough",
                route_reason=f"m1-passthrough:pool={requested_model}",
            )
        entry = self._pools[self._default_pool][0]
        return Resolution(
            pool=self._default_pool, provider=entry.provider, model=entry.model,
            route_stage="passthrough",
            route_reason=f"m1-passthrough:default:model-not-a-pool:{requested_model}",
        )
