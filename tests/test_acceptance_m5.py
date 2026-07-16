from datetime import UTC, datetime

from app.core.config import DashboardConfig
from app.dashboard import DashboardService
from app.repositories.requests_repo import RequestsRepository


async def _insert(
    app,
    *,
    request_id: str,
    ts: int,
    pool: str,
    provider: str,
    model: str,
    stage: str,
    reason: str,
    input_tokens: int,
    output_tokens: int,
    cost: float,
    fallback_from: str | None = None,
) -> None:
    await app.state.db.connection.execute(
        """
        INSERT INTO requests (
          id, ts, pool, provider, model, route_stage, route_reason,
          input_tokens, output_tokens, cost_usd, latency_ms, status, fallback_from
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            request_id, ts, pool, provider, model, stage, reason,
            input_tokens, output_tokens, cost, 25, 200, fallback_from,
        ),
    )


async def test_m5_dashboard_end_to_end_from_request_ledger(
    app, client, auth_headers, test_config
):
    now = datetime(2026, 7, 16, 12, tzinfo=UTC)
    await _insert(
        app, request_id="today", ts=int(datetime(2026, 7, 16, tzinfo=UTC).timestamp()),
        pool="cheap", provider="deepseek", model="deepseek-chat", stage="rule",
        reason="rule:short", input_tokens=1_000_000, output_tokens=0, cost=1.0,
    )
    await _insert(
        app, request_id="week", ts=int(datetime(2026, 7, 14, tzinfo=UTC).timestamp()),
        pool="default", provider="anthropic", model="claude-sonnet-5", stage="fallback",
        reason="classifier:default|fallback", input_tokens=0, output_tokens=500_000,
        cost=5.0, fallback_from="deepseek/deepseek-chat",
    )
    await _insert(
        app, request_id="month", ts=int(datetime(2026, 7, 2, tzinfo=UTC).timestamp()),
        pool="default", provider="anthropic", model="claude-sonnet-5", stage="classifier",
        reason="classifier:default", input_tokens=1_000_000, output_tokens=0, cost=2.0,
    )
    await _insert(
        app, request_id="old", ts=int(datetime(2026, 6, 30, tzinfo=UTC).timestamp()),
        pool="legacy", provider="old", model="old-model", stage="rule",
        reason="old", input_tokens=99_000_000, output_tokens=0, cost=100.0,
    )
    await app.state.db.connection.commit()
    app.state.dashboard = DashboardService(
        RequestsRepository(app.state.db.connection),
        DashboardConfig(
            budget_usd=10.0,
            baseline_model="claude-sonnet-5",
            recent_request_limit=3,
            fallback_event_limit=1,
        ),
        test_config.prices,
        clock=lambda: now,
    )

    response = await client.get("/dashboard", headers=auth_headers)

    assert response.status_code == 200
    page = response.text
    assert "$1.000000" in page
    assert "$6.000000" in page
    assert "$8.000000" in page
    assert "80.0% used" in page
    assert "$13.500000" in page  # baseline from current configured token rates
    assert "$5.500000" in page   # estimated savings versus stored actual $8
    assert "33.3%" in page and "66.7%" in page
    assert "deepseek/deepseek-chat" in page
    assert "exclude internal classifier probe calls" in page
    assert "Cooldown trips are not separately recorded" in page
    assert "old-model" not in page  # recent limit excludes oldest; monthly views exclude it


async def test_m5_empty_dashboard_has_explicit_empty_states(client, auth_headers):
    response = await client.get("/dashboard", headers=auth_headers)

    assert response.status_code == 200
    assert "No requests recorded." in response.text
    assert "No fallback events recorded." in response.text
    assert "Not configured" in response.text
