from __future__ import annotations

from dataclasses import dataclass

from app.providers.base import ProviderAdapter, ProviderError, ProviderResponse
from app.routing.health import HealthTracker
from app.routing.pools import PoolResolver
from app.routing.router import Resolution


@dataclass
class ExecutionOutcome:
    provider: str
    model: str
    response: ProviderResponse | None
    error: ProviderError | None
    fallback_from: str | None
    fallback_hops: int


def _entry_id(provider: str, model: str) -> str:
    return f"{provider}/{model}"


class FallbackExecutor:
    """Runs the ordered pool chain, skipping entries in cooldown and advancing
    past retryable failures. Returns an ExecutionOutcome; never raises."""

    def __init__(
        self,
        registry: dict[str, ProviderAdapter],
        pool_resolver: PoolResolver,
        health: HealthTracker,
    ) -> None:
        self._registry = registry
        self._pools = pool_resolver
        self._health = health

    async def execute(self, body: dict, resolution: Resolution) -> ExecutionOutcome:
        entries = self._pools.entries(resolution.pool)
        attempts = [
            e for e in entries if self._health.is_healthy(e.provider, e.model)
        ]
        if not attempts:
            attempts = list(entries)   # whole pool cooling -> best-effort (decision #5)

        first_id = _entry_id(attempts[0].provider, attempts[0].model)
        last_error: ProviderError | None = None

        for idx, entry in enumerate(attempts):
            adapter = self._registry[entry.provider]
            try:
                resp = await adapter.chat_completion(body, entry.model)
            except ProviderError as exc:
                if not exc.retryable:
                    return ExecutionOutcome(
                        provider=entry.provider, model=entry.model,
                        response=None, error=exc,
                        fallback_from=(first_id if idx > 0 else None),
                        fallback_hops=idx,
                    )
                self._health.mark_unhealthy(entry.provider, entry.model)
                last_error = exc
                continue
            return ExecutionOutcome(
                provider=entry.provider, model=entry.model,
                response=resp, error=None,
                fallback_from=(first_id if idx > 0 else None),
                fallback_hops=idx,
            )

        last = attempts[-1]
        return ExecutionOutcome(
            provider=last.provider, model=last.model,
            response=None, error=last_error,
            fallback_from=(first_id if len(attempts) > 1 else None),
            fallback_hops=len(attempts) - 1,
        )
