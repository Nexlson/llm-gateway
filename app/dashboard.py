from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from app.core.config import DashboardConfig, PriceEntry
from app.repositories.requests_repo import (
    BreakdownRow,
    RequestListRow,
    RequestsRepository,
    SpendWindows,
)


@dataclass(frozen=True)
class RoutingShare:
    key: str
    count: int
    cost: float
    share: float


@dataclass(frozen=True)
class BaselineEstimate:
    model: str
    estimated_cost: float
    actual_cost: float
    savings: float


@dataclass(frozen=True)
class DashboardView:
    generated_at: datetime
    spend: SpendWindows
    budget_usd: float | None
    budget_percent: float | None
    baseline: BaselineEstimate | None
    by_pool: list[BreakdownRow]
    by_model: list[BreakdownRow]
    by_provider: list[BreakdownRow]
    routing_by_pool: list[RoutingShare]
    routing_by_stage: list[RoutingShare]
    recent: list[RequestListRow]
    fallbacks: list[RequestListRow]


class DashboardService:
    def __init__(
        self,
        repository: RequestsRepository,
        config: DashboardConfig,
        prices: dict[str, PriceEntry],
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._repository = repository
        self._config = config
        self._prices = prices
        self._clock = clock or (lambda: datetime.now(UTC))

    async def snapshot(self) -> DashboardView:
        now = self._clock().astimezone(UTC)
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        week_start = today_start - timedelta(days=today_start.weekday())
        month_start = today_start.replace(day=1)
        month_ts = int(month_start.timestamp())

        spend = await self._repository.spend_windows(
            int(today_start.timestamp()), int(week_start.timestamp()), month_ts
        )
        by_pool = await self._repository.breakdown("pool", month_ts)
        by_model = await self._repository.breakdown("model", month_ts)
        by_provider = await self._repository.breakdown("provider", month_ts)
        by_stage = await self._repository.breakdown("route_stage", month_ts)
        totals = await self._repository.token_cost_totals(month_ts)
        recent = await self._repository.recent_requests(
            self._config.recent_request_limit
        )
        fallbacks = await self._repository.fallback_events(
            self._config.fallback_event_limit
        )

        budget_percent = None
        if self._config.budget_usd is not None:
            budget_percent = spend.month / self._config.budget_usd * 100

        baseline = None
        if self._config.baseline_model is not None:
            price = self._prices[self._config.baseline_model]
            estimated = (
                totals.input_tokens / 1_000_000 * price.input_per_1m
                + totals.output_tokens / 1_000_000 * price.output_per_1m
            )
            baseline = BaselineEstimate(
                model=self._config.baseline_model,
                estimated_cost=estimated,
                actual_cost=totals.cost,
                savings=estimated - totals.cost,
            )

        return DashboardView(
            generated_at=now,
            spend=spend,
            budget_usd=self._config.budget_usd,
            budget_percent=budget_percent,
            baseline=baseline,
            by_pool=by_pool,
            by_model=by_model,
            by_provider=by_provider,
            routing_by_pool=_with_shares(by_pool),
            routing_by_stage=_with_shares(by_stage),
            recent=recent,
            fallbacks=fallbacks,
        )


def _with_shares(rows: list[BreakdownRow]) -> list[RoutingShare]:
    total = sum(row.count for row in rows)
    return [
        RoutingShare(
            key=row.key,
            count=row.count,
            cost=row.cost,
            share=(row.count / total * 100 if total else 0.0),
        )
        for row in rows
    ]
