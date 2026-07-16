from __future__ import annotations

from app.routing.executor import FallbackExecutor
from app.routing.pools import PoolResolver
from app.routing.router import Resolution


class ExecutorProbeCaller:
    """Production ClassifierCaller: runs the classifier probe against its
    configured cheap pool via the shared M3 FallbackExecutor and returns the
    model's raw text. Raises on any failure so the Classifier degrades to its
    fallback_pool. Does not persist anything (the executor never writes rows)."""

    def __init__(
        self, executor: FallbackExecutor, pool_resolver: PoolResolver, pool: str
    ) -> None:
        self._executor = executor
        self._pools = pool_resolver
        self._pool = pool

    async def __call__(self, probe: dict) -> str:
        entry = self._pools.first_entry(self._pool)
        resolution = Resolution(
            pool=self._pool, provider=entry.provider, model=entry.model,
            route_stage="classifier", route_reason="classifier-probe",
        )
        outcome = await self._executor.execute(probe, resolution)
        if outcome.response is None:
            raise RuntimeError("classifier probe failed (pool exhausted)")
        return outcome.response.body["choices"][0]["message"]["content"]
