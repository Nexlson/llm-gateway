"""Independent M3 acceptance tests, derived from the plan's Acceptance Criteria.

Each test maps to a single M3-AC-* criterion and asserts observable behavior at
the public HTTP surface (plus direct HealthTracker/config assertions where the
criterion is a pure-unit one). Nothing here reads the implementation internals
except the documented test-only clock seam.
"""

import logging
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
import respx
from httpx import ASGITransport, AsyncClient

from app.core.config import (
    AppConfig,
    PoolEntry,
    PriceEntry,
    ProviderConfig,
    RuleConfig,
    RuleWhen,
    load_config,
)
from app.main import create_app
from app.routing.health import HealthTracker

VALID_KEY = "test-key-123"
DEEPSEEK_URL = "https://api.deepseek.com/v1/chat/completions"
ANTHROPIC_URL = "https://api.anthropic.com/v1/chat/completions"
EXAMPLE = Path(__file__).resolve().parents[1] / "config.example.yaml"


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
        rules=[RuleConfig(name="short-and-simple",
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


async def _rows(app, columns):
    cur = await app.state.db.connection.execute(f"SELECT {columns} FROM requests")
    return await cur.fetchall()


# ---------------------------------------------------------------- Fallback chain


@respx.mock
async def test_ac_f1_advance_on_retryable_5xx(app_and_client):
    app, client = await app_and_client()
    respx.post(DEEPSEEK_URL).mock(return_value=httpx.Response(503, json={"error": {"message": "down"}}))
    respx.post(ANTHROPIC_URL).mock(
        return_value=httpx.Response(
            200, json={"id": "second", "usage": {"prompt_tokens": 4, "completion_tokens": 2}}))

    cap = []

    class H(logging.Handler):
        def emit(self, rec):
            cap.append(rec)

    handler = H()
    logging.getLogger("gateway").addHandler(handler)
    try:
        resp = await client.post("/v1/chat/completions", headers=_auth(), json=_short_body())
    finally:
        logging.getLogger("gateway").removeHandler(handler)

    assert resp.status_code == 200
    assert resp.json()["id"] == "second"

    rows = await _rows(app, "provider, model, route_stage, fallback_from, status")
    assert len(rows) == 1
    assert rows[0] == ("anthropic", "claude-haiku-4-5", "fallback", "deepseek/deepseek-chat", 200)

    events = [r for r in cap if getattr(r, "event", None) == "request"]
    assert len(events) == 1
    assert events[0].fallback_hops == 1


@respx.mock
async def test_ac_f2_exhaustion_returns_last_error_openai_shape(app_and_client):
    app, client = await app_and_client()
    respx.post(DEEPSEEK_URL).mock(return_value=httpx.Response(503, json={"error": {"message": "a"}}))
    respx.post(ANTHROPIC_URL).mock(return_value=httpx.Response(429, json={"error": {"message": "b"}}))

    cap = []

    class H(logging.Handler):
        def emit(self, rec):
            cap.append(rec)

    handler = H()
    logging.getLogger("gateway").addHandler(handler)
    try:
        resp = await client.post("/v1/chat/completions", headers=_auth(), json=_short_body())
    finally:
        logging.getLogger("gateway").removeHandler(handler)

    assert resp.status_code == 429
    assert isinstance(resp.json()["error"]["message"], str)

    rows = await _rows(app, "status, input_tokens, output_tokens, cost_usd, route_stage, fallback_from")
    assert rows[0] == (429, 0, 0, 0.0, "fallback", "deepseek/deepseek-chat")

    events = [r for r in cap if getattr(r, "event", None) == "request"]
    assert events[0].fallback_hops == 1


@respx.mock
async def test_ac_f3_non_retryable_returns_immediately(app_and_client):
    app, client = await app_and_client()
    ds = respx.post(DEEPSEEK_URL).mock(return_value=httpx.Response(400, json={"error": {"message": "bad"}}))
    an = respx.post(ANTHROPIC_URL).mock(return_value=httpx.Response(200, json={"id": "z"}))

    cap = []

    class H(logging.Handler):
        def emit(self, rec):
            cap.append(rec)

    handler = H()
    logging.getLogger("gateway").addHandler(handler)
    try:
        resp = await client.post("/v1/chat/completions", headers=_auth(), json=_short_body())
    finally:
        logging.getLogger("gateway").removeHandler(handler)

    assert resp.status_code == 400
    assert ds.called and not an.called                      # chain not burned

    rows = await _rows(app, "provider, status, route_stage, fallback_from")
    assert rows[0] == ("deepseek", 400, "rule", None)

    events = [r for r in cap if getattr(r, "event", None) == "request"]
    assert events[0].fallback_hops == 0


@pytest.mark.parametrize("status", [401, 403])
@respx.mock
async def test_ac_f4_auth_errors_are_non_retryable(app_and_client, status):
    app, client = await app_and_client(name=f"auth{status}")
    ds = respx.post(DEEPSEEK_URL).mock(return_value=httpx.Response(status, json={"error": {"message": "no"}}))
    an = respx.post(ANTHROPIC_URL).mock(return_value=httpx.Response(200, json={"id": "z"}))

    resp = await client.post("/v1/chat/completions", headers=_auth(), json=_short_body())
    assert resp.status_code == status
    assert ds.called and not an.called                      # second entry not called

    rows = await _rows(app, "status, route_stage, fallback_from")
    assert rows[0] == (status, "rule", None)


@respx.mock
async def test_ac_f5_timeout_and_connection_errors_are_retryable(app_and_client):
    app, client = await app_and_client()
    respx.post(DEEPSEEK_URL).mock(side_effect=httpx.ConnectTimeout("boom"))
    respx.post(ANTHROPIC_URL).mock(return_value=httpx.Response(200, json={"id": "z"}))

    resp = await client.post("/v1/chat/completions", headers=_auth(), json=_short_body())
    assert resp.status_code == 200
    assert resp.json()["id"] == "z"

    rows = await _rows(app, "provider, route_stage, fallback_from")
    assert rows[0] == ("anthropic", "fallback", "deepseek/deepseek-chat")


@respx.mock
async def test_ac_f6_rate_limit_429_is_retryable(app_and_client):
    app, client = await app_and_client()
    respx.post(DEEPSEEK_URL).mock(return_value=httpx.Response(429, json={"error": {"message": "slow"}}))
    respx.post(ANTHROPIC_URL).mock(return_value=httpx.Response(200, json={"id": "z"}))

    resp = await client.post("/v1/chat/completions", headers=_auth(), json=_short_body())
    assert resp.status_code == 200

    rows = await _rows(app, "provider, model, route_stage, fallback_from")
    assert rows[0] == ("anthropic", "claude-haiku-4-5", "fallback", "deepseek/deepseek-chat")


# ---------------------------------------------------------------- Cooldown health


@respx.mock
async def test_ac_h1_h2_skip_within_window_then_free_attempt(app_and_client):
    app, client = await app_and_client()
    clock = FakeClock(1000.0)
    app.state.gateway._executor._health._clock = clock.now

    ds = respx.post(DEEPSEEK_URL).mock(return_value=httpx.Response(503, json={"error": {"message": "down"}}))
    respx.post(ANTHROPIC_URL).mock(return_value=httpx.Response(200, json={"id": "z"}))

    # request 1: deepseek fails -> marked unhealthy -> anthropic serves
    r1 = await client.post("/v1/chat/completions", headers=_auth(), json=_short_body())
    assert r1.status_code == 200 and ds.call_count == 1

    # request 2 within cooldown: deepseek skipped; served by next entry, no within-request hop
    r2 = await client.post("/v1/chat/completions", headers=_auth(), json=_short_body())
    assert r2.status_code == 200 and ds.call_count == 1

    rows = await _rows(app, "provider, fallback_from")
    # second row: served by anthropic, no fallback_from (started at healthy entry)
    assert rows[1] == ("anthropic", None)

    # H2: advance past cooldown -> deepseek gets one free attempt
    clock.t = 1060.0
    r3 = await client.post("/v1/chat/completions", headers=_auth(), json=_short_body())
    assert r3.status_code == 200 and ds.call_count == 2


def test_ac_h3_health_tracker_pure_and_deterministic():
    clock = FakeClock(1000.0)
    h = HealthTracker(cooldown_seconds=60, clock=clock.now)
    # unknown entry is healthy
    assert h.is_healthy("deepseek", "deepseek-chat") is True
    # unhealthy within the window
    h.mark_unhealthy("deepseek", "deepseek-chat")
    assert h.is_healthy("deepseek", "deepseek-chat") is False
    clock.t = 1059.0
    assert h.is_healthy("deepseek", "deepseek-chat") is False
    # healthy again at exactly the boundary and beyond
    clock.t = 1060.0
    assert h.is_healthy("deepseek", "deepseek-chat") is True
    clock.t = 1061.0
    assert h.is_healthy("deepseek", "deepseek-chat") is True
    # identical clock sequences produce identical results (no hidden state/IO)
    clock2 = FakeClock(1000.0)
    h2 = HealthTracker(cooldown_seconds=60, clock=clock2.now)
    h2.mark_unhealthy("deepseek", "deepseek-chat")
    assert h2.is_healthy("deepseek", "deepseek-chat") is False


@respx.mock
async def test_ac_h4_non_retryable_does_not_trip_cooldown(app_and_client):
    app, client = await app_and_client()
    clock = FakeClock(1000.0)
    app.state.gateway._executor._health._clock = clock.now

    ds = respx.post(DEEPSEEK_URL).mock(return_value=httpx.Response(400, json={"error": {"message": "bad"}}))
    respx.post(ANTHROPIC_URL).mock(return_value=httpx.Response(200, json={"id": "z"}))

    r1 = await client.post("/v1/chat/completions", headers=_auth(), json=_short_body())
    assert r1.status_code == 400 and ds.call_count == 1
    # clock NOT advanced: if 400 had tripped cooldown, deepseek would be skipped
    r2 = await client.post("/v1/chat/completions", headers=_auth(), json=_short_body())
    assert r2.status_code == 400 and ds.call_count == 2     # attempted again


@respx.mock
async def test_ac_h5_whole_pool_cooling_best_effort(app_and_client):
    app, client = await app_and_client()
    health = app.state.gateway._executor._health
    health.mark_unhealthy("deepseek", "deepseek-chat")
    health.mark_unhealthy("anthropic", "claude-haiku-4-5")

    ds = respx.post(DEEPSEEK_URL).mock(return_value=httpx.Response(200, json={"id": "z"}))
    an = respx.post(ANTHROPIC_URL).mock(return_value=httpx.Response(200, json={"id": "z"}))

    resp = await client.post("/v1/chat/completions", headers=_auth(), json=_short_body())
    assert resp.status_code == 200                          # not a no-attempt failure
    assert ds.called and not an.called                      # best-effort: first entry tried


# ---------------------------------------------------------------- Cost / storage / logging


@respx.mock
async def test_ac_c2_exactly_one_row_per_request(app_and_client):
    app, client = await app_and_client()
    respx.post(DEEPSEEK_URL).mock(return_value=httpx.Response(503, json={"error": {"message": "a"}}))
    respx.post(ANTHROPIC_URL).mock(return_value=httpx.Response(429, json={"error": {"message": "b"}}))

    await client.post("/v1/chat/completions", headers=_auth(), json=_short_body())

    cur = await app.state.db.connection.execute("SELECT count(*) FROM requests")
    assert (await cur.fetchone())[0] == 1


@respx.mock
async def test_ac_x1_no_fallback_path_identical_to_m2(app_and_client):
    app, client = await app_and_client()
    respx.post(DEEPSEEK_URL).mock(
        return_value=httpx.Response(
            200, json={"id": "ok", "usage": {"prompt_tokens": 4, "completion_tokens": 2}}))

    cap = []

    class H(logging.Handler):
        def emit(self, rec):
            cap.append(rec)

    handler = H()
    logging.getLogger("gateway").addHandler(handler)
    try:
        resp = await client.post("/v1/chat/completions", headers=_auth(), json=_short_body())
    finally:
        logging.getLogger("gateway").removeHandler(handler)

    assert resp.status_code == 200
    rows = await _rows(app, "provider, model, route_stage, fallback_from")
    assert rows[0] == ("deepseek", "deepseek-chat", "rule", None)

    events = [r for r in cap if getattr(r, "event", None) == "request"]
    assert events[0].fallback_hops == 0


def test_ac_cfg_cooldown_config_driven(monkeypatch):
    for k, v in {
        "GATEWAY_API_KEY": "k", "DEEPSEEK_API_KEY": "k", "ANTHROPIC_API_KEY": "k",
        "GROK_API_KEY": "k", "OPEN_AI_API_KEY": "k",
    }.items():
        monkeypatch.setenv(k, v)
    cfg = load_config(EXAMPLE)
    assert cfg.cooldown_seconds == 60.0
