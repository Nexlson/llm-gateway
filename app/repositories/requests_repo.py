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


class RequestsRepository:
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
