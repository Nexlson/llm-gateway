from datetime import UTC, datetime

import pytest

from app.core.config import DashboardConfig, PriceEntry
from app.dashboard import DashboardService
from app.repositories.requests_repo import (
    BreakdownRow,
    RequestListRow,
    SpendWindows,
    TokenCostTotals,
)


class FakeDashboardRepository:
    def __init__(self) -> None:
        self.boundaries: tuple[int, int, int] | None = None

    async def spend_windows(
        self, today_start: int, week_start: int, month_start: int
    ) -> SpendWindows:
        self.boundaries = (today_start, week_start, month_start)
        return SpendWindows(today=1.0, week=2.0, month=3.0)

    async def breakdown(self, column: str, since: int) -> list[BreakdownRow]:
        values = {
            "pool": [BreakdownRow("cheap", 3, 0.5)],
            "model": [BreakdownRow("tiny", 3, 0.5)],
            "provider": [BreakdownRow("acme", 3, 0.5)],
            "route_stage": [BreakdownRow("rule", 2, 0.4)],
        }
        return values[column]

    async def token_cost_totals(self, since: int) -> TokenCostTotals:
        return TokenCostTotals(input_tokens=1_000_000, output_tokens=500_000, cost=3.0)

    async def recent_requests(self, limit: int) -> list[RequestListRow]:
        return []

    async def fallback_events(self, limit: int) -> list[RequestListRow]:
        return []


@pytest.mark.asyncio
async def test_service_uses_utc_calendar_boundaries_and_baseline_math():
    repo = FakeDashboardRepository()
    now = datetime(2026, 7, 16, 12, 30, tzinfo=UTC)  # Thursday
    service = DashboardService(
        repo,
        DashboardConfig(budget_usd=10.0, baseline_model="frontier"),
        {"frontier": PriceEntry(input_per_1m=4.0, output_per_1m=12.0)},
        clock=lambda: now,
    )

    view = await service.snapshot()

    assert repo.boundaries == (
        int(datetime(2026, 7, 16, tzinfo=UTC).timestamp()),
        int(datetime(2026, 7, 13, tzinfo=UTC).timestamp()),
        int(datetime(2026, 7, 1, tzinfo=UTC).timestamp()),
    )
    assert view.spend == SpendWindows(today=1.0, week=2.0, month=3.0)
    assert view.budget_percent == pytest.approx(30.0)
    assert view.baseline is not None
    assert view.baseline.estimated_cost == pytest.approx(10.0)
    assert view.baseline.savings == pytest.approx(7.0)
    assert view.routing_by_pool[0].share == pytest.approx(100.0)


@pytest.mark.asyncio
async def test_service_leaves_optional_metrics_unconfigured():
    view = await DashboardService(
        FakeDashboardRepository(), DashboardConfig(), {},
        clock=lambda: datetime(2026, 7, 16, tzinfo=UTC),
    ).snapshot()

    assert view.budget_percent is None
    assert view.baseline is None
