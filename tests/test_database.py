import time

from app.core.database import Database
from app.repositories.requests_repo import RequestRecord, RequestsRepository


async def test_wal_mode_and_index(tmp_path):
    db = Database(str(tmp_path / "g.db"))
    await db.connect()
    try:
        cur = await db.connection.execute("PRAGMA journal_mode")
        assert (await cur.fetchone())[0].lower() == "wal"
        cur = await db.connection.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_requests_ts'"
        )
        assert await cur.fetchone() is not None
    finally:
        await db.disconnect()


async def test_insert_row(tmp_path):
    db = Database(str(tmp_path / "g.db"))
    await db.connect()
    try:
        repo = RequestsRepository(db.connection)
        rec = RequestRecord(
            id="req-1", ts=int(time.time()), pool="cheap", provider="deepseek",
            model="deepseek-chat", route_stage="passthrough",
            route_reason="m1-passthrough:pool=cheap", input_tokens=10,
            output_tokens=5, cost_usd=0.0, latency_ms=42, status=200,
            fallback_from=None,
        )
        await repo.insert(rec)
        cur = await db.connection.execute(
            "SELECT pool, model, route_stage, status FROM requests WHERE id='req-1'"
        )
        row = await cur.fetchone()
        assert row == ("cheap", "deepseek-chat", "passthrough", 200)
    finally:
        await db.disconnect()
