import pytest

from app.core.config import PoolEntry, RuleConfig, RuleWhen
from app.core.database import Database
from app.core.errors import BadRequestError
from app.cost.tracker import CostTracker
from app.providers.base import ProviderError, ProviderResponse
from app.repositories.requests_repo import RequestsRepository
from app.routing.gateway import GatewayService
from app.routing.pools import PoolResolver
from app.routing.router import Router
from app.routing.rules import RuleEngine

POOLS = {
    "cheap": [PoolEntry(provider="deepseek", model="deepseek-chat")],
    "default": [PoolEntry(provider="anthropic", model="claude-sonnet-5")],
}
RULES = [RuleConfig(name="short", when=RuleWhen(max_input_tokens=500, has_tools=False),
                    pool="cheap")]


class FakeAdapter:
    def __init__(self, name, response=None, error=None):
        self.name = name
        self._response = response
        self._error = error
        self.last_payload = None
        self.last_model = None

    async def chat_completion(self, payload, model):
        self.last_payload, self.last_model = payload, model
        if self._error:
            raise self._error
        return self._response


async def _service(tmp_path, adapters):
    db = Database(str(tmp_path / "g.db"))
    await db.connect()
    repo = RequestsRepository(db.connection)
    router = Router(RuleEngine(RULES), PoolResolver(POOLS, "default"))
    svc = GatewayService(router, adapters, CostTracker({}), repo)
    return db, repo, svc


async def test_missing_model_raises_bad_request(tmp_path):
    db, _, svc = await _service(tmp_path, {})
    try:
        with pytest.raises(BadRequestError) as ei:
            await svc.complete("r1", {"messages": []}, {})
        assert ei.value.param == "model"
    finally:
        await db.disconnect()


async def test_short_request_routes_to_cheap_and_logs_row(tmp_path):
    resp = ProviderResponse(200, {"id": "x", "choices": []}, 7, 3)
    adapter = FakeAdapter("deepseek", response=resp)
    db, repo, svc = await _service(tmp_path, {"deepseek": adapter})
    try:
        out = await svc.complete(
            "r1", {"model": "anything", "messages": [{"role": "user", "content": "hi"}],
                   "extra": 1}, {})
        assert out == {"id": "x", "choices": []}          # unchanged body
        assert adapter.last_model == "deepseek-chat"       # resolved upstream model
        assert adapter.last_payload["extra"] == 1          # unknown field preserved
        cur = await db.connection.execute(
            "SELECT pool, provider, model, route_stage, route_reason, status "
            "FROM requests WHERE id='r1'")
        row = await cur.fetchone()
        assert row == ("cheap", "deepseek", "deepseek-chat", "rule", "rule:short", 200)
    finally:
        await db.disconnect()


async def test_provider_error_logs_row_and_reraises(tmp_path):
    err = ProviderError("boom", 503, "deepseek", retryable=True)
    adapter = FakeAdapter("deepseek", error=err)
    db, repo, svc = await _service(tmp_path, {"deepseek": adapter})
    try:
        with pytest.raises(ProviderError):
            await svc.complete(
                "r2", {"model": "x", "messages": [{"role": "user", "content": "hi"}]}, {})
        cur = await db.connection.execute(
            "SELECT status, input_tokens, cost_usd FROM requests WHERE id='r2'")
        assert await cur.fetchone() == (503, 0, 0.0)
    finally:
        await db.disconnect()
