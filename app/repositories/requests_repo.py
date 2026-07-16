from __future__ import annotations

from dataclasses import dataclass

import aiosqlite


@dataclass
class RequestRecord:
    id: str
    ts: int
    pool: str
    provider: str
    model: str
    route_stage: str
    route_reason: str | None
    input_tokens: int
    output_tokens: int
    cost_usd: float
    latency_ms: int
    status: int
    fallback_from: str | None


@dataclass(frozen=True)
class SpendWindows:
    today: float
    week: float
    month: float


@dataclass(frozen=True)
class BreakdownRow:
    key: str
    count: int
    cost: float


@dataclass(frozen=True)
class TokenCostTotals:
    input_tokens: int
    output_tokens: int
    cost: float


@dataclass(frozen=True)
class RequestListRow:
    id: str
    ts: int
    pool: str
    provider: str
    model: str
    route_stage: str
    route_reason: str | None
    input_tokens: int
    output_tokens: int
    cost_usd: float
    latency_ms: int
    status: int
    fallback_from: str | None


class RequestsRepository:
    _BREAKDOWN_COLUMNS = {"pool", "model", "provider", "route_stage"}

    def __init__(self, connection: aiosqlite.Connection) -> None:
        self._conn = connection

    async def insert(self, record: RequestRecord) -> None:
        await self._conn.execute(
            """
            INSERT INTO requests (
              id, ts, pool, provider, model, route_stage, route_reason,
              input_tokens, output_tokens, cost_usd, latency_ms, status, fallback_from
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                record.id, record.ts, record.pool, record.provider, record.model,
                record.route_stage, record.route_reason, record.input_tokens,
                record.output_tokens, record.cost_usd, record.latency_ms,
                record.status, record.fallback_from,
            ),
        )
        await self._conn.commit()

    async def spend_windows(
        self, today_start: int, week_start: int, month_start: int
    ) -> SpendWindows:
        cursor = await self._conn.execute(
            """
            SELECT
              COALESCE(SUM(CASE WHEN ts >= ? THEN cost_usd ELSE 0 END), 0),
              COALESCE(SUM(CASE WHEN ts >= ? THEN cost_usd ELSE 0 END), 0),
              COALESCE(SUM(CASE WHEN ts >= ? THEN cost_usd ELSE 0 END), 0)
            FROM requests
            """,
            (today_start, week_start, month_start),
        )
        row = await cursor.fetchone()
        assert row is not None
        return SpendWindows(today=float(row[0]), week=float(row[1]), month=float(row[2]))

    async def breakdown(self, column: str, since: int) -> list[BreakdownRow]:
        if column not in self._BREAKDOWN_COLUMNS:
            raise ValueError(f"unsupported breakdown column: {column}")
        cursor = await self._conn.execute(
            f"""
            SELECT {column}, COUNT(*), COALESCE(SUM(cost_usd), 0)
            FROM requests
            WHERE ts >= ?
            GROUP BY {column}
            ORDER BY SUM(cost_usd) DESC, {column} ASC
            """,
            (since,),
        )
        return [
            BreakdownRow(key=str(row[0]), count=int(row[1]), cost=float(row[2]))
            for row in await cursor.fetchall()
        ]

    async def token_cost_totals(self, since: int) -> TokenCostTotals:
        cursor = await self._conn.execute(
            """
            SELECT COALESCE(SUM(input_tokens), 0),
                   COALESCE(SUM(output_tokens), 0),
                   COALESCE(SUM(cost_usd), 0)
            FROM requests
            WHERE ts >= ?
            """,
            (since,),
        )
        row = await cursor.fetchone()
        assert row is not None
        return TokenCostTotals(
            input_tokens=int(row[0]), output_tokens=int(row[1]), cost=float(row[2])
        )

    async def recent_requests(self, limit: int) -> list[RequestListRow]:
        return await self._request_rows("", (), limit)

    async def fallback_events(self, limit: int) -> list[RequestListRow]:
        return await self._request_rows("WHERE fallback_from IS NOT NULL", (), limit)

    async def _request_rows(
        self, where: str, params: tuple[object, ...], limit: int
    ) -> list[RequestListRow]:
        cursor = await self._conn.execute(
            f"""
            SELECT id, ts, pool, provider, model, route_stage, route_reason,
                   input_tokens, output_tokens, cost_usd, latency_ms, status,
                   fallback_from
            FROM requests
            {where}
            ORDER BY ts DESC, id DESC
            LIMIT ?
            """,
            (*params, limit),
        )
        return [RequestListRow(*row) for row in await cursor.fetchall()]
