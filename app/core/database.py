from __future__ import annotations

import aiosqlite

_SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
  id            TEXT PRIMARY KEY,
  ts            INTEGER NOT NULL,
  pool          TEXT NOT NULL,
  provider      TEXT NOT NULL,
  model         TEXT NOT NULL,
  route_stage   TEXT NOT NULL,
  route_reason  TEXT,
  input_tokens  INTEGER NOT NULL,
  output_tokens INTEGER NOT NULL,
  cost_usd      REAL NOT NULL,
  latency_ms    INTEGER NOT NULL,
  status        INTEGER NOT NULL,
  fallback_from TEXT
);
CREATE INDEX IF NOT EXISTS idx_requests_ts ON requests(ts);
"""


class Database:
    def __init__(self, path: str) -> None:
        self._path = path
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self._conn = await aiosqlite.connect(self._path)
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.executescript(_SCHEMA)
        await self._conn.commit()

    async def disconnect(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def connection(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database not connected")
        return self._conn
