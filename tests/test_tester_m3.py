"""Independent tester acceptance suite for M3 fallback chain + 60s cooldown.

Expected behavior is derived from the plan's Acceptance Criteria (M3-AC-*) and the
domain invariants, NOT from the coder's implementation. Fixtures/wiring only are
reused. Cooldown time is driven by an injected fake clock — never a real sleep.
Provider HTTP is mocked with respx; no real provider APIs are hit.
"""

from __future__ import annotations

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
from app.providers.base import ProviderError, ProviderResponse
from app.routing.executor import FallbackExecutor
from app.routing.health import HealthTracker
from app.routing.pools import PoolResolver
from app.routing.router import Resolution

EXAMPLE = Path(__file__).resolve().parents[1] / "config.example.yaml"
VALID_KEY = "test-key-123"
DEEPSEEK_URL = "https://api.deepseek.com/v1/chat/completions"
ANTHROPIC_URL = "https://api.anthropic.com/v1/chat/completions"

HAIKU_PRICE_IN = 1.0
HAIKU_PRICE_OUT = 5.0


def _auth():
    return {"Authorization": f"Bearer {VALID_KEY}"}


def _short_body():
    return {"model": "m", "messages": [{"role": "user", "content": "hi"}]}


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
            "cheap": [
                PoolEntry(provider="deepseek", model="deepseek-chat"),
                PoolEntry(provider="anthropic", model="claude-haiku-4-5"),
            ],
            "default": [PoolEntry(provider="anthropic", model="claude-sonnet-5")],
        },
        prices={
            "deepseek-chat": PriceEntry(input_per_1m=0.27, output_per_1m=1.10),
            "claude-haiku-4-5": PriceEntry(input_per_1m=HAIKU_PRICE_IN, output_per_1m=HAIKU_PRICE_OUT),
        },
        rules=[RuleConfig(name="short", when=RuleWhen(max_input_tokens=500, has_tools=False), pool="cheap")],
    )


class FakeClock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def now(self) -> float:
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


async def _rows(app, cols):
    cur = await app.state.db.connection.execute(f"SELECT {cols} FROM requests")
    return await cur.fetchall()


class _Capture:
    """Attach to the 'gateway' logger and collect request log lines."""

    def __init__(self) -> None:
        self.records = []

    def __enter__(self):
        logger = logging.getLogger("gateway")

        class H(logging.Handler):
            def emit(inner, rec):  # noqa: N805
                self.records.append(rec)

        self._h = H()
        logger.addHandler(self._h)
        return self

    def __exit__(self, *a):
        logging.getLogger("gateway").removeHandler(self._h)

    @property
    def requests(self):
        return [r for r in self.records if getattr(r, "event", None) == "request"]


# ---------------------------------------------------------------------------
# Fallback chain
# ---------------------------------------------------------------------------


@respx.mock
async def test_m3_ac_f1_advance_on_retryable_5xx(app_and_client):
    app, client = await app_and_client()
    respx.post(DEEPSEEK_URL).mock(return_value=httpx.Response(503, json={"error": {"message": "down"}}))
    respx.post(ANTHROPIC_URL).mock(
        return_value=httpx.Response(
            200, json={"id": "second", "usage": {"prompt_tokens": 4, "completion_tokens": 2}}
        )
    )
    with _Capture() as cap:
        resp = await client.post("/v1/chat/completions", headers=_auth(), json=_short_body())

    assert resp.status_code == 200
    assert resp.json()["id"] == "second"

    rows = await _rows(app, "provider, model, route_stage, fallback_from, status, cost_usd")
    assert len(rows) == 1
    provider, model, stage, fallback_from, status, cost = rows[0]
    assert (provider, model, stage) == ("anthropic", "claude-haiku-4-5", "fallback")
    assert fallback_from == "deepseek/deepseek-chat"
    assert status == 200
    # M3-AC-C1: cost on the SERVED model (haiku), not the first entry's price
    assert cost == pytest.approx(4 / 1e6 * HAIKU_PRICE_IN + 2 / 1e6 * HAIKU_PRICE_OUT)

    reqs = cap.requests
    assert len(reqs) == 1  # M3-AC-C2: exactly one log line
    assert reqs[0].__dict__["fallback_hops"] == 1


@respx.mock
async def test_m3_ac_f2_exhaustion_returns_last_error_openai_shape(app_and_client):
    app, client = await app_and_client()
    respx.post(DEEPSEEK_URL).mock(return_value=httpx.Response(503, json={"error": {"message": "a"}}))
    respx.post(ANTHROPIC_URL).mock(return_value=httpx.Response(429, json={"error": {"message": "b"}}))

    with _Capture() as cap:
        resp = await client.post("/v1/chat/completions", headers=_auth(), json=_short_body())

    assert resp.status_code == 429  # last upstream status
    assert isinstance(resp.json()["error"]["message"], str)  # OpenAI error shape

    rows = await _rows(app, "status, input_tokens, output_tokens, cost_usd, route_stage, fallback_from")
    assert len(rows) == 1
    assert rows[0] == (429, 0, 0, 0.0, "fallback", "deepseek/deepseek-chat")
    assert cap.requests[0].__dict__["fallback_hops"] == 1


@respx.mock
async def test_m3_ac_f3_non_retryable_returns_immediately(app_and_client):
    app, client = await app_and_client()
    ds = respx.post(DEEPSEEK_URL).mock(return_value=httpx.Response(400, json={"error": {"message": "bad"}}))
    an = respx.post(ANTHROPIC_URL).mock(return_value=httpx.Response(200, json={"id": "z"}))

    with _Capture() as cap:
        resp = await client.post("/v1/chat/completions", headers=_auth(), json=_short_body())

    assert resp.status_code == 400
    assert isinstance(resp.json()["error"]["message"], str)
    assert ds.called and not an.called  # chain not burned

    rows = await _rows(app, "provider, status, route_stage, fallback_from")
    assert len(rows) == 1
    assert rows[0] == ("deepseek", 400, "rule", None)
    assert cap.requests[0].__dict__["fallback_hops"] == 0


@respx.mock
@pytest.mark.parametrize("status", [401, 403])
async def test_m3_ac_f4_auth_errors_non_retryable(app_and_client, status):
    app, client = await app_and_client(name=f"auth{status}")
    ds = respx.post(DEEPSEEK_URL).mock(return_value=httpx.Response(status, json={"error": {"message": "no"}}))
    an = respx.post(ANTHROPIC_URL).mock(return_value=httpx.Response(200, json={"id": "z"}))

    resp = await client.post("/v1/chat/completions", headers=_auth(), json=_short_body())

    assert resp.status_code == status
    assert ds.called and not an.called
    rows = await _rows(app, "status, route_stage, fallback_from")
    assert rows[0] == (status, "rule", None)


@respx.mock
@pytest.mark.parametrize("exc", [httpx.ConnectTimeout("boom"), httpx.ConnectError("boom")])
async def test_m3_ac_f5_timeout_and_connection_errors_retryable(app_and_client, exc):
    app, client = await app_and_client(name=f"conn{type(exc).__name__}")
    respx.post(DEEPSEEK_URL).mock(side_effect=exc)
    respx.post(ANTHROPIC_URL).mock(return_value=httpx.Response(200, json={"id": "served"}))

    resp = await client.post("/v1/chat/completions", headers=_auth(), json=_short_body())

    assert resp.status_code == 200
    assert resp.json()["id"] == "served"
    rows = await _rows(app, "provider, route_stage, fallback_from")
    assert rows[0] == ("anthropic", "fallback", "deepseek/deepseek-chat")


@respx.mock
async def test_m3_ac_f6_rate_limit_429_retryable(app_and_client):
    app, client = await app_and_client()
    respx.post(DEEPSEEK_URL).mock(return_value=httpx.Response(429, json={"error": {"message": "slow down"}}))
    respx.post(ANTHROPIC_URL).mock(return_value=httpx.Response(200, json={"id": "served"}))

    resp = await client.post("/v1/chat/completions", headers=_auth(), json=_short_body())

    assert resp.status_code == 200
    assert resp.json()["id"] == "served"
    rows = await _rows(app, "provider, route_stage, fallback_from")
    assert rows[0] == ("anthropic", "fallback", "deepseek/deepseek-chat")


# ---------------------------------------------------------------------------
# Cooldown health (endpoint-level, deterministic clock via documented seam)
# ---------------------------------------------------------------------------


@respx.mock
async def test_m3_ac_h1_h2_skip_within_window_then_free_attempt(app_and_client):
    app, client = await app_and_client()
    clock = FakeClock(1000.0)
    app.state.gateway._executor._health._clock = clock.now  # documented test seam

    ds = respx.post(DEEPSEEK_URL).mock(return_value=httpx.Response(503, json={"error": {"message": "down"}}))
    respx.post(ANTHROPIC_URL).mock(return_value=httpx.Response(200, json={"id": "z"}))

    # req1: deepseek fails retryably -> marked unhealthy -> anthropic serves
    r1 = await client.post("/v1/chat/completions", headers=_auth(), json=_short_body())
    assert r1.status_code == 200 and ds.call_count == 1

    # req2 within cooldown: deepseek skipped entirely (not re-called), served by next entry.
    # No within-request hop -> fallback_from NULL, hops 0, provider = the healthy entry.
    r2 = await client.post("/v1/chat/completions", headers=_auth(), json=_short_body())
    assert r2.status_code == 200 and ds.call_count == 1  # NOT called again

    # advance past cooldown -> one free attempt on deepseek
    clock.t = 1060.0
    r3 = await client.post("/v1/chat/completions", headers=_auth(), json=_short_body())
    assert r3.status_code == 200 and ds.call_count == 2  # attempted again

    rows = await _rows(app, "id, provider, fallback_from")
    # second request row: no hop
    assert rows[1][1] == "anthropic" and rows[1][2] is None


def test_m3_ac_h3_healthtracker_pure_and_deterministic():
    clock = FakeClock(1000.0)
    h = HealthTracker(cooldown_seconds=60, clock=clock.now)
    # unknown entry is healthy
    assert h.is_healthy("deepseek", "deepseek-chat") is True
    h.mark_unhealthy("deepseek", "deepseek-chat")
    # unhealthy for every clock value < mark + cooldown
    assert h.is_healthy("deepseek", "deepseek-chat") is False
    clock.t = 1059.999
    assert h.is_healthy("deepseek", "deepseek-chat") is False
    # healthy again at exactly mark + cooldown and beyond
    clock.t = 1060.0
    assert h.is_healthy("deepseek", "deepseek-chat") is True
    clock.t = 5000.0
    assert h.is_healthy("deepseek", "deepseek-chat") is True
    # independence across entries
    clock.t = 1000.0
    h.mark_unhealthy("deepseek", "deepseek-chat")
    assert h.is_healthy("anthropic", "claude-haiku-4-5") is True

    # determinism: identical clock sequence -> identical results
    def run():
        c = FakeClock(1000.0)
        t = HealthTracker(cooldown_seconds=60, clock=c.now)
        t.mark_unhealthy("p", "m")
        out = []
        for v in (1000.0, 1030.0, 1060.0, 1090.0):
            c.t = v
            out.append(t.is_healthy("p", "m"))
        return out

    assert run() == run() == [False, False, True, True]


@respx.mock
async def test_m3_ac_h4_non_retryable_does_not_trip_cooldown(app_and_client):
    app, client = await app_and_client()
    ds = respx.post(DEEPSEEK_URL).mock(return_value=httpx.Response(400, json={"error": {"message": "bad"}}))
    respx.post(ANTHROPIC_URL).mock(return_value=httpx.Response(200, json={"id": "z"}))

    r1 = await client.post("/v1/chat/completions", headers=_auth(), json=_short_body())
    assert r1.status_code == 400 and ds.call_count == 1
    # deepseek was NOT marked unhealthy -> it is attempted again on the next request
    r2 = await client.post("/v1/chat/completions", headers=_auth(), json=_short_body())
    assert r2.status_code == 400 and ds.call_count == 2


@respx.mock
async def test_m3_ac_h5_whole_pool_cooling_best_effort(app_and_client):
    app, client = await app_and_client()
    # pre-mark BOTH cheap entries unhealthy
    health = app.state.gateway._executor._health
    health.mark_unhealthy("deepseek", "deepseek-chat")
    health.mark_unhealthy("anthropic", "claude-haiku-4-5")

    ds = respx.post(DEEPSEEK_URL).mock(return_value=httpx.Response(200, json={"id": "ok"}))
    respx.post(ANTHROPIC_URL).mock(return_value=httpx.Response(200, json={"id": "an"}))

    resp = await client.post("/v1/chat/completions", headers=_auth(), json=_short_body())

    # best-effort: the chain is still attempted in order rather than failing w/o a call
    assert resp.status_code == 200
    assert resp.json()["id"] == "ok"
    assert ds.called
    rows = await _rows(app, "provider, status")
    assert rows[0] == ("deepseek", 200)


# ---------------------------------------------------------------------------
# No-regression: no-fallback path identical to M2; config-driven cooldown
# ---------------------------------------------------------------------------


@respx.mock
async def test_m3_ac_x1_no_fallback_path_identical_to_m2(app_and_client):
    app, client = await app_and_client()
    respx.post(DEEPSEEK_URL).mock(
        return_value=httpx.Response(200, json={"id": "ok", "usage": {"prompt_tokens": 3, "completion_tokens": 1}})
    )
    with _Capture() as cap:
        resp = await client.post("/v1/chat/completions", headers=_auth(), json=_short_body())

    assert resp.status_code == 200
    rows = await _rows(app, "provider, model, route_stage, route_reason, fallback_from")
    assert len(rows) == 1
    assert rows[0] == ("deepseek", "deepseek-chat", "rule", "rule:short", None)
    reqs = cap.requests
    assert len(reqs) == 1
    assert reqs[0].__dict__["fallback_hops"] == 0
    assert reqs[0].__dict__["fallback_from"] is None


def test_m3_ac_cfg_cooldown_default_and_example(monkeypatch):
    for k in ("GATEWAY_API_KEY", "DEEPSEEK_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.setenv(k, "k")
    cfg = load_config(EXAMPLE)
    assert cfg.cooldown_seconds == 60.0
    # default when unspecified
    assert AppConfig(
        api_key="x", default_pool="d",
        providers={"p": ProviderConfig(base_url="http://x", api_key="x")},
        pools={"d": [PoolEntry(provider="p", model="m")]},
    ).cooldown_seconds == 60.0


# ---------------------------------------------------------------------------
# Executor-level unit checks (public interface) — served entry / hop accounting
# ---------------------------------------------------------------------------

POOLS = {
    "cheap": [PoolEntry(provider="deepseek", model="deepseek-chat"),
              PoolEntry(provider="anthropic", model="claude-haiku-4-5")],
    "default": [PoolEntry(provider="anthropic", model="claude-sonnet-5")],
}


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


def _resolution(pool="cheap"):
    entry = POOLS[pool][0]
    return Resolution(pool=pool, provider=entry.provider, model=entry.model,
                      route_stage="rule", route_reason="rule:short")


def _executor(adapters, clock=None, cooldown=60):
    health = HealthTracker(cooldown_seconds=cooldown, clock=(clock.now if clock else (lambda: 0.0)))
    return FallbackExecutor(adapters, PoolResolver(POOLS, "default"), health), health


async def test_executor_first_success_no_hop_no_mark():
    resp = ProviderResponse(200, {"id": "ok"}, 5, 2)
    ds = FakeAdapter("deepseek", response=resp)
    an = FakeAdapter("anthropic")
    ex, health = _executor({"deepseek": ds, "anthropic": an})
    out = await ex.execute(_short_body(), _resolution())
    assert out.response is resp and out.error is None
    assert (out.provider, out.model) == ("deepseek", "deepseek-chat")
    assert out.fallback_from is None and out.fallback_hops == 0
    assert ds.calls == 1 and an.calls == 0
    assert health.is_healthy("deepseek", "deepseek-chat") is True


async def test_executor_retryable_marks_and_advances():
    err = ProviderError("down", 503, "deepseek", retryable=True)
    resp = ProviderResponse(200, {"id": "ok"}, 5, 2)
    ds = FakeAdapter("deepseek", error=err)
    an = FakeAdapter("anthropic", response=resp)
    ex, health = _executor({"deepseek": ds, "anthropic": an})
    out = await ex.execute(_short_body(), _resolution())
    assert (out.provider, out.model) == ("anthropic", "claude-haiku-4-5")
    assert out.fallback_from == "deepseek/deepseek-chat" and out.fallback_hops == 1
    assert health.is_healthy("deepseek", "deepseek-chat") is False


async def test_executor_non_retryable_no_mark_no_advance():
    err = ProviderError("bad", 400, "deepseek", retryable=False, error_type="invalid_request_error")
    ds = FakeAdapter("deepseek", error=err)
    an = FakeAdapter("anthropic")
    ex, health = _executor({"deepseek": ds, "anthropic": an})
    out = await ex.execute(_short_body(), _resolution())
    assert out.error is err and out.response is None
    assert out.fallback_from is None and out.fallback_hops == 0
    assert an.calls == 0
    assert health.is_healthy("deepseek", "deepseek-chat") is True


async def test_executor_cooldown_skip_is_not_a_hop():
    err = ProviderError("down", 503, "deepseek", retryable=True)
    resp = ProviderResponse(200, {"id": "ok"}, 1, 1)
    clock = FakeClock(1000.0)
    ds = FakeAdapter("deepseek", error=err)
    an = FakeAdapter("anthropic", response=resp)
    ex, _ = _executor({"deepseek": ds, "anthropic": an}, clock=clock)
    first = await ex.execute(_short_body(), _resolution())
    assert first.fallback_hops == 1 and ds.calls == 1
    second = await ex.execute(_short_body(), _resolution())
    assert (second.provider, second.model) == ("anthropic", "claude-haiku-4-5")
    assert second.fallback_from is None and second.fallback_hops == 0
    assert ds.calls == 1  # skipped, not re-tried
