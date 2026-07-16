"""Independent acceptance tests for M1 passthrough proxy.

Derived from the plan's Acceptance Criteria (A1-A13) and the domain invariants,
NOT from the coder's implementation. Expected behavior comes from the spec.

Reuses the shared fixtures in conftest.py (`client`, `app`, `auth_headers`,
`test_config`) only for wiring — expectations are asserted from the spec.
"""

from __future__ import annotations

import json
import logging
import os
import time
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
from app.cost.tracker import CostTracker
from app.main import create_app

EXAMPLE = Path(__file__).resolve().parents[1] / "config.example.yaml"
VALID_KEY = "test-key-123"  # matches conftest VALID_KEY

DEEPSEEK_URL = "https://api.deepseek.com/v1/chat/completions"
ANTHROPIC_URL = "https://api.anthropic.com/v1/chat/completions"

PROTECTED = [
    ("post", "/v1/chat/completions"),
    ("get", "/v1/models"),
    ("get", "/dashboard"),
]


def _openai_error_shape(body: dict) -> None:
    assert set(body.keys()) == {"error"}, body
    err = body["error"]
    for key in ("message", "type", "param", "code"):
        assert key in err, f"missing {key} in error envelope: {err}"
    assert isinstance(err["message"], str) and err["message"], "message must be non-empty"


# --------------------------------------------------------------------------
# A1 - Auth (constant-time bearer)
# --------------------------------------------------------------------------


@pytest.mark.parametrize("method,path", PROTECTED)
async def test_a1_no_auth_returns_401_openai_shape(client, method, path):
    resp = await getattr(client, method)(path)
    assert resp.status_code == 401
    body = resp.json()
    _openai_error_shape(body)
    assert body["error"]["type"] in {"invalid_request_error", "authentication_error"}


@pytest.mark.parametrize("method,path", PROTECTED)
async def test_a1_wrong_key_returns_401_openai_shape(client, method, path):
    headers = {"Authorization": "Bearer totally-wrong-key"}
    resp = await getattr(client, method)(path, headers=headers)
    assert resp.status_code == 401
    _openai_error_shape(resp.json())


async def test_a1_correct_key_not_401(client, auth_headers):
    resp = await client.get("/v1/models", headers=auth_headers)
    assert resp.status_code != 401


async def test_a1_wrong_length_key_rejected(client):
    # constant-time compare must still reject keys of a different length.
    resp = await client.get("/v1/models", headers={"Authorization": "Bearer x"})
    assert resp.status_code == 401


async def test_a1_healthz_no_auth(client):
    resp = await client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


# --------------------------------------------------------------------------
# A2 - GET /v1/models shape
# --------------------------------------------------------------------------


async def test_a2_models_list_shape(client, auth_headers):
    resp = await client.get("/v1/models", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["object"] == "list"
    assert isinstance(data["data"], list)
    ids = [m["id"] for m in data["data"]]
    # exactly one entry per pool name in config
    assert sorted(ids) == ["cheap", "default", "large-context"]
    assert len(ids) == len(set(ids))
    for m in data["data"]:
        assert m["object"] == "model"
        assert isinstance(m["created"], int)


# --------------------------------------------------------------------------
# A3 / A4 - Chat passthrough happy path + unknown-field forwarding
# --------------------------------------------------------------------------


@respx.mock
async def test_a3_passthrough_body_unchanged_and_model_rewritten(client, auth_headers):
    provider_body = {
        "id": "resp-1",
        "object": "chat.completion",
        "model": "deepseek-chat",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "hi"}}],
        "usage": {"prompt_tokens": 7, "completion_tokens": 3},
    }
    route = respx.post(DEEPSEEK_URL).mock(return_value=httpx.Response(200, json=provider_body))

    req_body = {
        "model": "cheap",
        "messages": [{"role": "user", "content": "hi"}],
        "temperature": 0.2,
        "some_future_field": True,
    }
    resp = await client.post("/v1/chat/completions", headers=auth_headers, json=req_body)

    # response body returned unchanged
    assert resp.status_code == 200
    assert resp.json() == provider_body

    # upstream request: model rewritten to first entry of `cheap`, other fields preserved
    assert route.called
    sent = json.loads(route.calls.last.request.content)
    assert sent["model"] == "deepseek-chat"
    assert sent["temperature"] == 0.2
    assert sent["some_future_field"] is True  # A4: unknown field forwarded, not stripped
    assert sent["messages"] == req_body["messages"]

    # goes to resolved provider base_url + /chat/completions with that provider's key
    assert str(route.calls.last.request.url) == DEEPSEEK_URL
    assert route.calls.last.request.headers["authorization"] == "Bearer sk-ds"


@respx.mock
async def test_a4_unknown_field_never_422(client, auth_headers):
    respx.post(DEEPSEEK_URL).mock(return_value=httpx.Response(200, json={"id": "x"}))
    resp = await client.post(
        "/v1/chat/completions",
        headers=auth_headers,
        json={"model": "cheap", "messages": [], "brand_new_sdk_param": {"nested": 1}},
    )
    assert resp.status_code == 200


# --------------------------------------------------------------------------
# A5 - Default-pool fallthrough when no rule matches
# --------------------------------------------------------------------------
# M2 supersedes M1 model-name routing: the `model` field no longer selects a
# pool. A request that matches no rule (here: medium length, no tools, no
# x-pool hint) routes to default_pool with route_stage="default".


@respx.mock
async def test_a5_unknown_model_routes_to_default_pool(client, auth_headers, app):
    route = respx.post(ANTHROPIC_URL).mock(
        return_value=httpx.Response(200, json={"id": "z"})
    )
    medium = "x" * 4000  # ~1000 tokens: above short threshold, below long threshold
    resp = await client.post(
        "/v1/chat/completions",
        headers=auth_headers,
        json={"model": "gpt-4o-not-a-pool",
              "messages": [{"role": "user", "content": medium}]},
    )
    assert resp.status_code == 200
    assert route.called
    sent = json.loads(route.calls.last.request.content)
    # default_pool first entry is anthropic/claude-sonnet-5; model is rewritten
    assert sent["model"] == "claude-sonnet-5"

    cur = await app.state.db.connection.execute(
        "SELECT pool, provider, model, route_stage FROM requests"
    )
    row = await cur.fetchone()
    assert row == ("default", "anthropic", "claude-sonnet-5", "default")


# --------------------------------------------------------------------------
# A6 - Request logging (SQLite) + WAL + index
# --------------------------------------------------------------------------


@respx.mock
async def test_a6_one_row_columns_match_schema(client, auth_headers, app):
    respx.post(DEEPSEEK_URL).mock(
        return_value=httpx.Response(
            200, json={"id": "r", "usage": {"prompt_tokens": 4, "completion_tokens": 2}}
        )
    )
    before = int(time.time())
    await client.post(
        "/v1/chat/completions", headers=auth_headers, json={"model": "cheap", "messages": []}
    )
    after = int(time.time())

    conn = app.state.db.connection
    cur = await conn.execute(
        "SELECT id, ts, pool, provider, model, route_stage, route_reason, "
        "input_tokens, output_tokens, cost_usd, latency_ms, status, fallback_from "
        "FROM requests"
    )
    rows = await cur.fetchall()
    assert len(rows) == 1
    (
        rid, ts, pool, provider, model, route_stage, route_reason,
        in_tok, out_tok, cost, latency, status, fallback_from,
    ) = rows[0]
    assert isinstance(rid, str) and rid
    assert isinstance(ts, int) and before <= ts <= after
    assert pool == "cheap"
    assert provider == "deepseek"
    assert model == "deepseek-chat"  # resolved upstream model, not pool name
    assert route_stage == "rule"  # M2: short tool-less request matches a rule
    assert route_reason is not None
    assert (in_tok, out_tok) == (4, 2)
    # M2 ships a populated price table; cost computed from provider usage.
    assert cost == pytest.approx(4 / 1e6 * 0.27 + 2 / 1e6 * 1.10)
    assert isinstance(latency, int) and latency >= 0
    assert status == 200
    assert fallback_from is None


@respx.mock
async def test_a6_row_written_even_on_upstream_error(client, auth_headers, app):
    respx.post(DEEPSEEK_URL).mock(
        return_value=httpx.Response(500, json={"error": {"message": "down"}})
    )
    await client.post(
        "/v1/chat/completions", headers=auth_headers, json={"model": "cheap", "messages": []}
    )
    cur = await app.state.db.connection.execute("SELECT status FROM requests")
    rows = await cur.fetchall()
    assert len(rows) == 1
    assert rows[0][0] == 500


async def test_a6_wal_mode_and_index(app):
    conn = app.state.db.connection
    cur = await conn.execute("PRAGMA journal_mode")
    assert (await cur.fetchone())[0].lower() == "wal"
    cur = await conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_requests_ts'"
    )
    assert await cur.fetchone() is not None


# --------------------------------------------------------------------------
# A7 - Cost at write time
# --------------------------------------------------------------------------


def test_a7_cost_formula_and_unknown_zero():
    assert CostTracker({}).compute("anything", 1000, 1000) == 0.0
    prices = {"deepseek-chat": PriceEntry(input_per_1m=0.27, output_per_1m=1.10)}
    tracker = CostTracker(prices)
    cost = tracker.compute("deepseek-chat", 2_000_000, 500_000)
    expected = 2_000_000 / 1e6 * 0.27 + 500_000 / 1e6 * 1.10
    assert round(cost, 9) == round(expected, 9)


@pytest_asyncio.fixture
async def priced_client(tmp_path):
    """Client wired to a config that has a non-empty price table."""
    config = AppConfig(
        api_key=VALID_KEY,
        db_path=str(tmp_path / "priced.db"),
        default_pool="default",
        providers={
            "deepseek": ProviderConfig(base_url="https://api.deepseek.com/v1", api_key="sk-ds"),
            "anthropic": ProviderConfig(base_url="https://api.anthropic.com/v1", api_key="sk-an"),
        },
        pools={
            "cheap": [PoolEntry(provider="deepseek", model="deepseek-chat")],
            "default": [PoolEntry(provider="anthropic", model="claude-sonnet-5")],
        },
        prices={"deepseek-chat": PriceEntry(input_per_1m=1.0, output_per_1m=2.0)},
        # M2: routing is by rule, not by the model field. Route short requests to
        # the priced `cheap` pool so cost-at-write-time is exercised on deepseek.
        rules=[
            RuleConfig(name="short",
                       when=RuleWhen(max_input_tokens=500, has_tools=False),
                       pool="cheap"),
        ],
    )
    application = create_app(config)
    async with application.router.lifespan_context(application):
        transport = ASGITransport(app=application)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c, application


@respx.mock
async def test_a7_cost_written_at_write_time(priced_client):
    client, application = priced_client
    respx.post(DEEPSEEK_URL).mock(
        return_value=httpx.Response(
            200,
            json={"id": "r", "usage": {"prompt_tokens": 1_000_000, "completion_tokens": 1_000_000}},
        )
    )
    await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {VALID_KEY}"},
        json={"model": "cheap", "messages": []},
    )
    cur = await application.state.db.connection.execute(
        "SELECT cost_usd FROM requests"
    )
    (cost,) = await cur.fetchone()
    # 1M in * $1/M + 1M out * $2/M = 3.0
    assert round(cost, 9) == 3.0


# --------------------------------------------------------------------------
# A8 - Structured JSON log line (no bodies)
# --------------------------------------------------------------------------


class _RecordCapture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record):
        self.records.append(record)


@respx.mock
async def test_a8_single_json_request_log_no_bodies(client, auth_headers):
    respx.post(DEEPSEEK_URL).mock(
        return_value=httpx.Response(
            200, json={"id": "r", "usage": {"prompt_tokens": 4, "completion_tokens": 2}}
        )
    )
    # Attach a capture handler to the gateway logger (configured in lifespan already).
    cap = _RecordCapture()
    logger = logging.getLogger("gateway")
    logger.addHandler(cap)
    try:
        await client.post(
            "/v1/chat/completions",
            headers=auth_headers,
            json={"model": "cheap", "messages": [{"role": "user", "content": "SECRET-PROMPT"}]},
        )
    finally:
        logger.removeHandler(cap)

    request_events = [
        r for r in cap.records if getattr(r, "event", None) == "request"
    ]
    assert len(request_events) == 1
    rec = request_events[0]
    fields = {
        k: v
        for k, v in rec.__dict__.items()
        if k in {
            "request_id", "pool", "model", "route_stage", "input_tokens",
            "output_tokens", "cost_usd", "latency_ms", "status", "fallback_hops",
        }
    }
    for key in (
        "request_id", "pool", "model", "route_stage", "input_tokens",
        "output_tokens", "cost_usd", "latency_ms", "status", "fallback_hops",
    ):
        assert key in fields, f"missing log key {key}"
    assert fields["fallback_hops"] == 0

    # No prompt/completion bodies leaked anywhere in the record dict.
    blob = json.dumps({k: str(v) for k, v in rec.__dict__.items()})
    assert "SECRET-PROMPT" not in blob
    assert "messages" not in rec.__dict__


# --------------------------------------------------------------------------
# A9 - Upstream error mapping
# --------------------------------------------------------------------------


@pytest.mark.parametrize("status", [500, 400])
@respx.mock
async def test_a9_upstream_error_returned_openai_shape(client, auth_headers, app, status):
    respx.post(DEEPSEEK_URL).mock(
        return_value=httpx.Response(status, json={"error": {"message": "boom"}})
    )
    resp = await client.post(
        "/v1/chat/completions", headers=auth_headers, json={"model": "cheap", "messages": []}
    )
    assert resp.status_code == status
    _openai_error_shape(resp.json())

    cur = await app.state.db.connection.execute("SELECT status FROM requests")
    assert (await cur.fetchone())[0] == status


# --------------------------------------------------------------------------
# A10 - Missing model field
# --------------------------------------------------------------------------


@respx.mock
async def test_a10_missing_model_400_no_upstream_call(client, auth_headers):
    ds = respx.post(DEEPSEEK_URL).mock(return_value=httpx.Response(200, json={}))
    an = respx.post(ANTHROPIC_URL).mock(return_value=httpx.Response(200, json={}))
    resp = await client.post(
        "/v1/chat/completions", headers=auth_headers, json={"messages": []}
    )
    assert resp.status_code == 400
    body = resp.json()
    _openai_error_shape(body)
    assert body["error"]["type"] == "invalid_request_error"
    assert body["error"]["param"] == "model"
    assert not ds.called and not an.called  # no upstream call made


# --------------------------------------------------------------------------
# A11 - Config loading + fail-fast validation
# --------------------------------------------------------------------------

EXAMPLE_ENV = {
    "GATEWAY_API_KEY": "test-gateway-key",
    "DEEPSEEK_API_KEY": "test-deepseek-key",
    "ANTHROPIC_API_KEY": "test-anthropic-key",
}


def _set_env(monkeypatch, env):
    for k, v in env.items():
        monkeypatch.setenv(k, v)


def test_a11_loads_typed_config(monkeypatch):
    _set_env(monkeypatch, EXAMPLE_ENV)
    cfg = load_config(EXAMPLE)
    assert isinstance(cfg, AppConfig)
    assert cfg.default_pool in cfg.pools
    assert cfg.db_path
    assert set(cfg.pools.keys()) == {"cheap", "default", "large-context"}
    assert "deepseek" in cfg.providers
    assert cfg.prices != {}  # M2 example ships a populated price table


def test_a11_rejects_default_pool_not_in_pools(tmp_path, monkeypatch):
    monkeypatch.setenv("K", "x")
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        "api_key_env: K\n"
        "default_pool: missing\n"
        "db_path: g.db\n"
        "providers:\n  p:\n    base_url: http://x\n    api_key_env: K\n"
        "pools:\n  default:\n    - {provider: p, model: m}\n"
    )
    with pytest.raises(ValueError):
        load_config(bad)


def test_a11_rejects_pool_entry_unknown_provider(tmp_path, monkeypatch):
    monkeypatch.setenv("K", "x")
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        "api_key_env: K\n"
        "default_pool: default\n"
        "db_path: g.db\n"
        "providers:\n  p:\n    base_url: http://x\n    api_key_env: K\n"
        "pools:\n  default:\n    - {provider: nope, model: m}\n"
    )
    with pytest.raises(ValueError):
        load_config(bad)


# --------------------------------------------------------------------------
# A12 - Dashboard auth and HTML surface
# --------------------------------------------------------------------------


async def test_a12_dashboard_requires_auth(client):
    resp = await client.get("/dashboard")
    assert resp.status_code == 401


async def test_a12_dashboard_behind_auth(client, auth_headers):
    resp = await client.get("/dashboard", headers=auth_headers)
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "Gateway dashboard" in resp.text


# --------------------------------------------------------------------------
# A13 - Secrets from environment
# --------------------------------------------------------------------------


def test_a13_no_raw_secret_in_example_config():
    text = EXAMPLE.read_text()
    # Secrets referenced by env-var name, never inlined.
    assert "api_key_env" in text
    assert "GATEWAY_API_KEY" in text
    # A raw `api_key:` assignment must not appear in the committed example.
    for line in text.splitlines():
        stripped = line.split("#", 1)[0].strip()
        assert not stripped.startswith("api_key:"), f"raw key in example: {line}"


def test_a13_env_vars_populate_resolved_keys(monkeypatch):
    _set_env(monkeypatch, EXAMPLE_ENV)
    cfg = load_config(EXAMPLE)
    assert cfg.api_key == "test-gateway-key"
    assert cfg.providers["deepseek"].api_key == "test-deepseek-key"
    assert cfg.providers["anthropic"].api_key == "test-anthropic-key"


def test_a13_unset_env_var_fails_fast(monkeypatch):
    monkeypatch.setenv("GATEWAY_API_KEY", "test-gateway-key")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-deepseek-key")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(ValueError):
        load_config(EXAMPLE)


def test_a13_empty_env_var_fails_fast(monkeypatch):
    monkeypatch.setenv("GATEWAY_API_KEY", "test-gateway-key")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-deepseek-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")  # empty -> must fail fast
    with pytest.raises(ValueError):
        load_config(EXAMPLE)
