import pytest

from app.core.config import PoolEntry, PriceEntry, RuleConfig, RuleWhen
from app.core.database import Database
from app.core.errors import BadRequestError
from app.cost.tracker import CostTracker
from app.providers.base import ProviderError, ProviderResponse
from app.repositories.requests_repo import RequestsRepository
from app.routing.executor import FallbackExecutor
from app.routing.gateway import GatewayService
from app.routing.health import HealthTracker
from app.routing.pools import PoolResolver
from app.routing.router import Router
from app.routing.rules import RuleEngine

POOLS = {
    "cheap": [PoolEntry(provider="deepseek", model="deepseek-chat"),
              PoolEntry(provider="anthropic", model="claude-haiku-4-5")],
    "default": [PoolEntry(provider="anthropic", model="claude-sonnet-5")],
}
RULES = [RuleConfig(name="short", when=RuleWhen(max_input_tokens=500, has_tools=False),
                    pool="cheap")]
PRICES = {}


class FakeAdapter:
    def __init__(self, name, response=None, error=None):
        self.name = name
        self._response = response
        self._error = error
        self.calls = 0

    async def chat_completion(self, payload, model):
        self.calls += 1
        if self._error is not None:
            raise self._error
        return self._response


async def _service(tmp_path, adapters, prices=None):
    db = Database(str(tmp_path / "g.db"))
    await db.connect()
    repo = RequestsRepository(db.connection)
    pr = PoolResolver(POOLS, "default")
    router = Router(RuleEngine(RULES), pr)
    executor = FallbackExecutor(adapters, pr, HealthTracker(cooldown_seconds=60))
    svc = GatewayService(router, executor, CostTracker(prices or PRICES), repo)
    return db, repo, svc


async def test_missing_model_raises_bad_request(tmp_path):
    db, _, svc = await _service(tmp_path, {})
    try:
        with pytest.raises(BadRequestError) as ei:
            await svc.complete("r1", {"messages": []}, {})
        assert ei.value.param == "model"
    finally:
        await db.disconnect()


async def test_first_entry_success_records_no_fallback(tmp_path):
    resp = ProviderResponse(200, {"id": "x", "choices": []}, 7, 3)
    ds = FakeAdapter("deepseek", response=resp)
    db, _, svc = await _service(tmp_path, {"deepseek": ds, "anthropic": FakeAdapter("anthropic")})
    try:
        out = await svc.complete(
            "r1", {"model": "anything", "messages": [{"role": "user", "content": "hi"}],
                   "extra": 1}, {})
        assert out == {"id": "x", "choices": []}
        cur = await db.connection.execute(
            "SELECT pool, provider, model, route_stage, route_reason, status, fallback_from "
            "FROM requests WHERE id='r1'")
        assert await cur.fetchone() == (
            "cheap", "deepseek", "deepseek-chat", "rule", "rule:short", 200, None)
    finally:
        await db.disconnect()


async def test_fallback_records_served_entry_and_fallback_from(tmp_path):
    err = ProviderError("down", 503, "deepseek", retryable=True)
    resp = ProviderResponse(200, {"id": "y"}, 4, 2)
    ds = FakeAdapter("deepseek", error=err)
    an = FakeAdapter("anthropic", response=resp)
    db, _, svc = await _service(
        tmp_path, {"deepseek": ds, "anthropic": an},
        prices={"claude-haiku-4-5": PriceEntry(input_per_1m=1.0, output_per_1m=5.0)})
    try:
        out = await svc.complete(
            "r2", {"model": "m", "messages": [{"role": "user", "content": "hi"}]}, {})
        assert out == {"id": "y"}
        cur = await db.connection.execute(
            "SELECT provider, model, route_stage, fallback_from, status, cost_usd "
            "FROM requests WHERE id='r2'")
        provider, model, stage, fallback_from, status, cost = await cur.fetchone()
        assert provider == "anthropic" and model == "claude-haiku-4-5"
        assert stage == "fallback"
        assert fallback_from == "deepseek/deepseek-chat"
        assert status == 200
        # cost computed on the SERVED model (claude-haiku-4-5), from its usage
        assert round(cost, 9) == round(4 / 1e6 * 1.0 + 2 / 1e6 * 5.0, 9)
    finally:
        await db.disconnect()


async def test_exhaustion_records_last_error_and_reraises(tmp_path):
    e1 = ProviderError("down", 503, "deepseek", retryable=True)
    e2 = ProviderError("busy", 429, "anthropic", retryable=True)
    ds = FakeAdapter("deepseek", error=e1)
    an = FakeAdapter("anthropic", error=e2)
    db, _, svc = await _service(tmp_path, {"deepseek": ds, "anthropic": an})
    try:
        with pytest.raises(ProviderError) as ei:
            await svc.complete(
                "r3", {"model": "m", "messages": [{"role": "user", "content": "hi"}]}, {})
        assert ei.value.status_code == 429
        cur = await db.connection.execute(
            "SELECT status, input_tokens, cost_usd, route_stage, fallback_from "
            "FROM requests WHERE id='r3'")
        assert await cur.fetchone() == (429, 0, 0.0, "fallback", "deepseek/deepseek-chat")
    finally:
        await db.disconnect()


async def test_non_retryable_does_not_burn_chain(tmp_path):
    err = ProviderError("bad", 400, "deepseek", retryable=False,
                        error_type="invalid_request_error")
    ds = FakeAdapter("deepseek", error=err)
    an = FakeAdapter("anthropic")
    db, _, svc = await _service(tmp_path, {"deepseek": ds, "anthropic": an})
    try:
        with pytest.raises(ProviderError) as ei:
            await svc.complete(
                "r4", {"model": "m", "messages": [{"role": "user", "content": "hi"}]}, {})
        assert ei.value.status_code == 400
        assert an.calls == 0                                  # second entry untouched
        cur = await db.connection.execute(
            "SELECT status, route_stage, fallback_from FROM requests WHERE id='r4'")
        assert await cur.fetchone() == (400, "rule", None)
    finally:
        await db.disconnect()
