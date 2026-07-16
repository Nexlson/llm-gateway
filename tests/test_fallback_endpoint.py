import httpx
import pytest_asyncio
import respx
from httpx import ASGITransport, AsyncClient

from app.core.config import AppConfig, PoolEntry, PriceEntry, ProviderConfig, RuleConfig, RuleWhen
from app.main import create_app

VALID_KEY = "test-key-123"
DEEPSEEK_URL = "https://api.deepseek.com/v1/chat/completions"
ANTHROPIC_URL = "https://api.anthropic.com/v1/chat/completions"


def _auth():
    return {"Authorization": f"Bearer {VALID_KEY}"}


def _cfg(tmp_path, name):
    return AppConfig(
        api_key=VALID_KEY,
        db_path=str(tmp_path / f"{name}.db"),
        cooldown_seconds=60,
        default_pool="default",
        providers={
            "deepseek": ProviderConfig(base_url="https://api.deepseek.com/v1", api_key="sk-ds"),
            "anthropic": ProviderConfig(base_url="https://api.anthropic.com/v1", api_key="sk-an"),
        },
        pools={
            "cheap": [PoolEntry(provider="deepseek", model="deepseek-chat"),
                      PoolEntry(provider="anthropic", model="claude-haiku-4-5")],
            "default": [PoolEntry(provider="anthropic", model="claude-sonnet-5")],
        },
        prices={
            "deepseek-chat": PriceEntry(input_per_1m=0.27, output_per_1m=1.10),
            "claude-haiku-4-5": PriceEntry(input_per_1m=1.0, output_per_1m=5.0),
        },
        rules=[RuleConfig(name="short",
                          when=RuleWhen(max_input_tokens=500, has_tools=False), pool="cheap")],
    )


class FakeClock:
    def __init__(self, t=1000.0):
        self.t = t

    def now(self):
        return self.t


@pytest_asyncio.fixture
async def app_and_client(tmp_path):
    made = []

    async def _make(name="g"):
        application = create_app(_cfg(tmp_path, name))
        ctx = application.router.lifespan_context(application)
        await ctx.__aenter__()
        made.append((application, ctx))
        c = AsyncClient(transport=ASGITransport(app=application), base_url="http://test")
        return application, c

    yield _make
    for application, ctx in made:
        await ctx.__aexit__(None, None, None)


def _short_body():
    return {"model": "m", "messages": [{"role": "user", "content": "hi"}]}


@respx.mock
async def test_endpoint_fallback_on_5xx(app_and_client):
    app, client = await app_and_client()
    respx.post(DEEPSEEK_URL).mock(return_value=httpx.Response(503, json={"error": {"message": "down"}}))
    respx.post(ANTHROPIC_URL).mock(
        return_value=httpx.Response(
            200, json={"id": "second", "usage": {"prompt_tokens": 4, "completion_tokens": 2}}))

    resp = await client.post("/v1/chat/completions", headers=_auth(), json=_short_body())
    assert resp.status_code == 200
    assert resp.json()["id"] == "second"

    cur = await app.state.db.connection.execute(
        "SELECT provider, model, route_stage, fallback_from, status, cost_usd FROM requests")
    rows = await cur.fetchall()
    assert len(rows) == 1
    provider, model, stage, fallback_from, status, cost = rows[0]
    assert (provider, model, stage) == ("anthropic", "claude-haiku-4-5", "fallback")
    assert fallback_from == "deepseek/deepseek-chat" and status == 200
    assert round(cost, 9) == round(4 / 1e6 * 1.0 + 2 / 1e6 * 5.0, 9)   # served model's price


@respx.mock
async def test_endpoint_exhaustion_returns_openai_error(app_and_client):
    app, client = await app_and_client()
    respx.post(DEEPSEEK_URL).mock(return_value=httpx.Response(503, json={"error": {"message": "a"}}))
    respx.post(ANTHROPIC_URL).mock(return_value=httpx.Response(429, json={"error": {"message": "b"}}))

    resp = await client.post("/v1/chat/completions", headers=_auth(), json=_short_body())
    assert resp.status_code == 429
    assert isinstance(resp.json()["error"]["message"], str)

    cur = await app.state.db.connection.execute(
        "SELECT status, route_stage, fallback_from FROM requests")
    assert await cur.fetchone() == (429, "fallback", "deepseek/deepseek-chat")


@respx.mock
async def test_endpoint_non_retryable_returns_immediately(app_and_client):
    app, client = await app_and_client()
    ds = respx.post(DEEPSEEK_URL).mock(return_value=httpx.Response(400, json={"error": {"message": "bad"}}))
    an = respx.post(ANTHROPIC_URL).mock(return_value=httpx.Response(200, json={"id": "z"}))

    resp = await client.post("/v1/chat/completions", headers=_auth(), json=_short_body())
    assert resp.status_code == 400
    assert ds.called and not an.called                     # chain not burned

    cur = await app.state.db.connection.execute(
        "SELECT status, route_stage, fallback_from FROM requests")
    assert await cur.fetchone() == (400, "rule", None)


@respx.mock
async def test_endpoint_cooldown_skips_then_free_attempt(app_and_client):
    app, client = await app_and_client()
    # inject a deterministic clock into the app's live HealthTracker
    clock = FakeClock(1000.0)
    app.state.gateway._executor._health._clock = clock.now

    ds = respx.post(DEEPSEEK_URL).mock(return_value=httpx.Response(503, json={"error": {"message": "down"}}))
    respx.post(ANTHROPIC_URL).mock(return_value=httpx.Response(200, json={"id": "z"}))

    # request 1: deepseek fails -> marked unhealthy -> anthropic serves
    r1 = await client.post("/v1/chat/completions", headers=_auth(), json=_short_body())
    assert r1.status_code == 200 and ds.call_count == 1

    # request 2 within cooldown: deepseek skipped (not called again)
    r2 = await client.post("/v1/chat/completions", headers=_auth(), json=_short_body())
    assert r2.status_code == 200 and ds.call_count == 1     # unchanged -> skipped

    # advance past cooldown: deepseek gets its one free attempt
    clock.t = 1060.0
    r3 = await client.post("/v1/chat/completions", headers=_auth(), json=_short_body())
    assert r3.status_code == 200 and ds.call_count == 2     # attempted again
