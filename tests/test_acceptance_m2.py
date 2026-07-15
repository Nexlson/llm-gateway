"""Independent acceptance tests for M2 rule engine + pools + cost table.

Derived from the plan's Acceptance Criteria (AC-R*, AC-C*, AC-X*) and the domain
invariants, NOT from the coder's implementation. Expected behavior is asserted
from the spec; fixtures are reused only for wiring.
"""

from __future__ import annotations

import json
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

EXAMPLE = Path(__file__).resolve().parents[1] / "config.example.yaml"
VALID_KEY = "test-key-123"
DEEPSEEK_URL = "https://api.deepseek.com/v1/chat/completions"
ANTHROPIC_URL = "https://api.anthropic.com/v1/chat/completions"


def _auth():
    return {"Authorization": f"Bearer {VALID_KEY}"}


async def _row(app, cols):
    cur = await app.state.db.connection.execute(f"SELECT {cols} FROM requests")
    return await cur.fetchall()


@pytest_asyncio.fixture
async def custom_app(tmp_path):
    """Factory building an app with caller-supplied pools/prices/rules."""

    apps = []

    async def _make(pools, prices, rules, default_pool="default", providers=None):
        providers = providers or {
            "deepseek": ProviderConfig(base_url="https://api.deepseek.com/v1", api_key="sk-ds"),
            "anthropic": ProviderConfig(base_url="https://api.anthropic.com/v1", api_key="sk-an"),
        }
        cfg = AppConfig(
            api_key=VALID_KEY,
            db_path=str(tmp_path / f"g{len(apps)}.db"),
            default_pool=default_pool,
            providers=providers,
            pools=pools,
            prices=prices,
            rules=rules,
        )
        application = create_app(cfg)
        ctx = application.router.lifespan_context(application)
        await ctx.__aenter__()
        apps.append((application, ctx))
        transport = ASGITransport(app=application)
        c = AsyncClient(transport=transport, base_url="http://test")
        return application, c

    yield _make

    for application, ctx in apps:
        await ctx.__aexit__(None, None, None)


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------


@respx.mock
async def test_ac_r1_first_match_wins_and_header_overrides(client, app):
    ds = respx.post(DEEPSEEK_URL).mock(return_value=httpx.Response(200, json={"id": "a"}))
    an = respx.post(ANTHROPIC_URL).mock(return_value=httpx.Response(200, json={"id": "b"}))
    body = {"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]}

    # short, tool-less, no x-pool header -> short-and-simple -> cheap (deepseek)
    r1 = await client.post("/v1/chat/completions", headers=_auth(), json=body)
    assert r1.status_code == 200 and ds.called

    # add x-pool: default -> explicit-hint (rule 1) wins over short-and-simple (rule 4)
    r2 = await client.post(
        "/v1/chat/completions", headers={**_auth(), "x-pool": "default"}, json=body
    )
    assert r2.status_code == 200 and an.called

    rows = await _row(app, "pool, route_reason")
    assert rows[0][0] == "cheap" and "short-and-simple" in rows[0][1]
    assert rows[1][0] == "default" and "explicit-hint" in rows[1][1]


@respx.mock
async def test_ac_r2_max_token_threshold_and_overflow_to_default(client, app):
    respx.post(DEEPSEEK_URL).mock(return_value=httpx.Response(200, json={"id": "a"}))
    respx.post(ANTHROPIC_URL).mock(return_value=httpx.Response(200, json={"id": "b"}))

    # <= 500 tokens -> cheap
    short = await client.post(
        "/v1/chat/completions", headers=_auth(),
        json={"model": "m", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert short.status_code == 200
    # ~1000 tokens (>500, <20000) -> matches no threshold rule -> default_pool/default
    big = "x" * 4000
    over = await client.post(
        "/v1/chat/completions", headers=_auth(),
        json={"model": "m", "messages": [{"role": "user", "content": big}]},
    )
    assert over.status_code == 200

    rows = await _row(app, "pool, route_stage, route_reason")
    assert rows[0] == ("cheap", "rule", "rule:short-and-simple")
    assert rows[1] == ("default", "default", "no-rule-matched")


@respx.mock
async def test_ac_r3_min_token_threshold_routes_large_context(client, app):
    respx.post(ANTHROPIC_URL).mock(return_value=httpx.Response(200, json={"id": "b"}))
    huge = "x" * 100000  # ~25000 tokens >= 20000
    resp = await client.post(
        "/v1/chat/completions", headers=_auth(),
        json={"model": "m", "messages": [{"role": "user", "content": huge}]},
    )
    assert resp.status_code == 200
    rows = await _row(app, "pool, route_stage, route_reason")
    assert rows[0] == ("large-context", "rule", "rule:long-context")


@respx.mock
async def test_ac_r4_tool_presence_routes_to_default(client, app):
    respx.post(ANTHROPIC_URL).mock(return_value=httpx.Response(200, json={"id": "b"}))
    # short but has tools -> has-tools rule -> default (beats short-and-simple)
    resp = await client.post(
        "/v1/chat/completions", headers=_auth(),
        json={"model": "m", "messages": [{"role": "user", "content": "hi"}],
              "tools": [{"type": "function", "function": {"name": "f"}}]},
    )
    assert resp.status_code == 200
    rows = await _row(app, "pool, route_stage, route_reason")
    assert rows[0] == ("default", "rule", "rule:has-tools")


@respx.mock
async def test_ac_r5_explicit_header_hint_case_insensitive(client, app):
    respx.post(DEEPSEEK_URL).mock(return_value=httpx.Response(200, json={"id": "a"}))
    # header name in mixed case must still match
    resp = await client.post(
        "/v1/chat/completions", headers={**_auth(), "X-Pool": "cheap"},
        json={"model": "m", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 200
    rows = await _row(app, "pool, route_stage, route_reason")
    assert rows[0][0] == "cheap"
    assert rows[0][1] == "rule"
    assert "explicit-hint" in rows[0][2]


@respx.mock
async def test_ac_r6_unsafe_hint_degrades_to_default(client, app):
    # x-pool: bogus resolves to an undefined pool -> must NOT fail; -> default_pool
    an = respx.post(ANTHROPIC_URL).mock(return_value=httpx.Response(200, json={"id": "b"}))
    ds = respx.post(DEEPSEEK_URL).mock(return_value=httpx.Response(200, json={"id": "a"}))
    resp = await client.post(
        "/v1/chat/completions", headers={**_auth(), "x-pool": "does-not-exist"},
        json={"model": "m", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 200  # request still succeeds against healthy provider
    assert an.called and not ds.called
    rows = await _row(app, "pool, route_stage, route_reason")
    assert rows[0][0] == "default"
    assert rows[0][1] == "default"
    assert "unknown-pool" in rows[0][2] and "does-not-exist" in rows[0][2]


@respx.mock
async def test_ac_r7_system_regex(custom_app):
    pools = {
        "cheap": [PoolEntry(provider="deepseek", model="deepseek-chat")],
        "default": [PoolEntry(provider="anthropic", model="claude-sonnet-5")],
    }
    prices = {
        "deepseek-chat": PriceEntry(input_per_1m=0.27, output_per_1m=1.10),
        "claude-sonnet-5": PriceEntry(input_per_1m=3.0, output_per_1m=15.0),
    }
    rules = [RuleConfig(name="translate", when=RuleWhen(system_regex="(?i)translate"),
                        pool="cheap")]
    app, client = await custom_app(pools, prices, rules)
    respx.post(DEEPSEEK_URL).mock(return_value=httpx.Response(200, json={"id": "a"}))
    respx.post(ANTHROPIC_URL).mock(return_value=httpx.Response(200, json={"id": "b"}))

    # system contains "Translate" -> matches -> cheap
    m = await client.post(
        "/v1/chat/completions", headers=_auth(),
        json={"model": "m", "messages": [
            {"role": "system", "content": "Please Translate this"},
            {"role": "user", "content": "hi"}]},
    )
    assert m.status_code == 200
    # no translate word -> no rule -> default
    nm = await client.post(
        "/v1/chat/completions", headers=_auth(),
        json={"model": "m", "messages": [
            {"role": "system", "content": "summarize this"},
            {"role": "user", "content": "hi"}]},
    )
    assert nm.status_code == 200

    rows = await _row(app, "pool, route_stage, route_reason")
    assert rows[0] == ("cheap", "rule", "rule:translate")
    assert rows[1] == ("default", "default", "no-rule-matched")


@respx.mock
async def test_ac_r8_empty_rules_all_default(custom_app):
    pools = {
        "cheap": [PoolEntry(provider="deepseek", model="deepseek-chat")],
        "default": [PoolEntry(provider="anthropic", model="claude-sonnet-5")],
    }
    prices = {"claude-sonnet-5": PriceEntry(input_per_1m=3.0, output_per_1m=15.0)}
    app, client = await custom_app(pools, prices, rules=[])
    respx.post(ANTHROPIC_URL).mock(return_value=httpx.Response(200, json={"id": "b"}))
    resp = await client.post(
        "/v1/chat/completions", headers=_auth(),
        json={"model": "m", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 200
    rows = await _row(app, "pool, route_stage, route_reason")
    assert rows[0] == ("default", "default", "no-rule-matched")


@respx.mock
async def test_ac_r9_decision_persisted_and_logged(client, app):
    respx.post(DEEPSEEK_URL).mock(
        return_value=httpx.Response(
            200, json={"id": "r", "usage": {"prompt_tokens": 4, "completion_tokens": 2}})
    )
    cap = []

    class H(logging.Handler):
        def emit(self, rec):
            cap.append(rec)

    handler = H()
    logging.getLogger("gateway").addHandler(handler)
    try:
        await client.post(
            "/v1/chat/completions", headers=_auth(),
            json={"model": "m", "messages": [{"role": "user", "content": "hi"}]},
        )
    finally:
        logging.getLogger("gateway").removeHandler(handler)

    rows = await _row(app, "route_stage, route_reason")
    stage, reason = rows[0]
    assert stage in {"rule", "default"}
    assert reason and isinstance(reason, str)
    assert reason == "rule:short-and-simple"

    events = [r for r in cap if getattr(r, "event", None) == "request"]
    assert len(events) == 1
    assert events[0].__dict__["route_stage"] == stage


async def test_ac_r10_routing_pure_and_never_raises():
    # Pure unit-level check: identical inputs -> identical results, no I/O.
    from app.core.config import PoolEntry as PE
    from app.routing.features import extract_features
    from app.routing.pools import PoolResolver
    from app.routing.router import Router
    from app.routing.rules import RuleEngine

    rules = [
        RuleConfig(name="explicit-hint", when=RuleWhen(header="x-pool"),
                   pool="{{ header.x-pool }}"),
        RuleConfig(name="short", when=RuleWhen(max_input_tokens=500, has_tools=False),
                   pool="cheap"),
    ]
    pools = {
        "cheap": [PE(provider="deepseek", model="deepseek-chat")],
        "default": [PE(provider="anthropic", model="claude-sonnet-5")],
    }
    engine = RuleEngine(rules)
    body = {"model": "m", "messages": [{"role": "user", "content": "hi"}]}
    f1 = extract_features(body, {})
    f2 = extract_features(body, {})
    assert engine.match(f1) == engine.match(f2)

    router = Router(engine, PoolResolver(pools, "default"))
    # arbitrary/odd bodies must never raise
    for b in [{}, {"messages": None}, {"messages": [1, 2, 3]},
              {"tools": "notalist"}, {"messages": [{"role": "system"}]}]:
        res = router.route(b, {"x-pool": ""})
        assert res.pool in pools


# ---------------------------------------------------------------------------
# Cost
# ---------------------------------------------------------------------------


@respx.mock
async def test_ac_c1_real_cost_at_write_time(client, app):
    respx.post(DEEPSEEK_URL).mock(
        return_value=httpx.Response(
            200, json={"id": "r", "usage": {"prompt_tokens": 1_000_000,
                                            "completion_tokens": 1_000_000}})
    )
    await client.post(
        "/v1/chat/completions", headers=_auth(),
        json={"model": "m", "messages": [{"role": "user", "content": "hi"}]},
    )
    rows = await _row(app, "provider, cost_usd")
    assert rows[0][0] == "deepseek"
    assert rows[0][1] == pytest.approx(0.27 + 1.10)  # 1M in * .27/M + 1M out * 1.10/M


@respx.mock
async def test_ac_c2_cost_uses_provider_usage_not_estimate(client, app):
    # Short request (est ~1 token) routes by estimate to cheap, but stored tokens/cost
    # come from the provider's reported usage, independent of the estimate.
    respx.post(DEEPSEEK_URL).mock(
        return_value=httpx.Response(
            200, json={"id": "r", "usage": {"prompt_tokens": 500_000,
                                            "completion_tokens": 250_000}})
    )
    await client.post(
        "/v1/chat/completions", headers=_auth(),
        json={"model": "m", "messages": [{"role": "user", "content": "hi"}]},
    )
    rows = await _row(app, "input_tokens, output_tokens, cost_usd")
    in_tok, out_tok, cost = rows[0]
    assert (in_tok, out_tok) == (500_000, 250_000)
    assert cost == pytest.approx(0.5 * 0.27 + 0.25 * 1.10)


@respx.mock
async def test_ac_c3_unknown_model_cost_zero(custom_app):
    pools = {
        "cheap": [PoolEntry(provider="deepseek", model="unpriced-model")],
        "default": [PoolEntry(provider="anthropic", model="claude-sonnet-5")],
    }
    prices = {}  # nothing priced
    rules = [RuleConfig(name="short", when=RuleWhen(max_input_tokens=500, has_tools=False),
                        pool="cheap")]
    app, client = await custom_app(pools, prices, rules)
    respx.post(DEEPSEEK_URL).mock(
        return_value=httpx.Response(
            200, json={"id": "r", "usage": {"prompt_tokens": 100, "completion_tokens": 50}})
    )
    resp = await client.post(
        "/v1/chat/completions", headers=_auth(),
        json={"model": "m", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 200
    rows = await _row(app, "model, cost_usd")
    assert rows[0] == ("unpriced-model", 0.0)


def test_ac_c4_example_prices_non_empty(monkeypatch):
    for k, v in {
        "GATEWAY_API_KEY": "k", "DEEPSEEK_API_KEY": "k", "ANTHROPIC_API_KEY": "k",
    }.items():
        monkeypatch.setenv(k, v)
    cfg = load_config(EXAMPLE)
    assert cfg.prices != {}
    assert "deepseek-chat" in cfg.prices


# ---------------------------------------------------------------------------
# Compatibility
# ---------------------------------------------------------------------------


@respx.mock
async def test_ac_x1_body_unchanged_model_rewritten(client):
    provider_body = {"id": "resp-1", "object": "chat.completion",
                     "choices": [{"message": {"content": "hi"}}]}
    route = respx.post(DEEPSEEK_URL).mock(
        return_value=httpx.Response(200, json=provider_body))
    resp = await client.post(
        "/v1/chat/completions", headers=_auth(),
        json={"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}],
              "temperature": 0.2, "future_field": {"nested": 1}},
    )
    assert resp.status_code == 200
    assert resp.json() == provider_body  # response body byte-for-content unchanged
    sent = json.loads(route.calls.last.request.content)
    assert sent["model"] == "deepseek-chat"          # rewritten to resolved model
    assert sent["temperature"] == 0.2                 # preserved
    assert sent["future_field"] == {"nested": 1}      # unknown forwarded
    assert sent["messages"] == [{"role": "user", "content": "hi"}]


@respx.mock
async def test_ac_x2_missing_model_400_no_upstream(client):
    ds = respx.post(DEEPSEEK_URL).mock(return_value=httpx.Response(200, json={}))
    an = respx.post(ANTHROPIC_URL).mock(return_value=httpx.Response(200, json={}))
    resp = await client.post(
        "/v1/chat/completions", headers=_auth(), json={"messages": []})
    assert resp.status_code == 400
    body = resp.json()
    assert body["error"]["type"] == "invalid_request_error"
    assert body["error"]["param"] == "model"
    assert not ds.called and not an.called


@respx.mock
async def test_ac_x2_missing_model_writes_no_row(client, app):
    respx.post(DEEPSEEK_URL).mock(return_value=httpx.Response(200, json={}))
    await client.post("/v1/chat/completions", headers=_auth(), json={"messages": []})
    rows = await _row(app, "id")
    assert rows == []


@pytest.mark.parametrize("path", ["/v1/chat/completions", "/v1/models", "/dashboard"])
async def test_ac_x3_auth_required(client, path):
    if path == "/v1/chat/completions":
        resp = await client.post(path, json={"model": "m"})
    else:
        resp = await client.get(path)
    assert resp.status_code == 401


async def test_ac_x3_healthz_no_auth(client):
    resp = await client.get("/healthz")
    assert resp.status_code == 200


@respx.mock
async def test_ac_x_errors_openai_shape_on_upstream_5xx(client):
    respx.post(DEEPSEEK_URL).mock(
        return_value=httpx.Response(500, json={"error": {"message": "down"}}))
    resp = await client.post(
        "/v1/chat/completions", headers=_auth(),
        json={"model": "m", "messages": [{"role": "user", "content": "hi"}]})
    assert resp.status_code == 500
    assert "error" in resp.json()
    assert isinstance(resp.json()["error"].get("message"), str)


# ---------------------------------------------------------------------------
# AC-X4 config validation
# ---------------------------------------------------------------------------


def _base_yaml() -> str:
    return (
        "api_key_env: K\n"
        "default_pool: default\n"
        "db_path: g.db\n"
        "providers:\n  p:\n    base_url: http://x\n    api_key_env: K\n"
        "pools:\n  default:\n    - {provider: p, model: m}\n"
    )


def test_ac_x4_rejects_unknown_literal_pool(tmp_path, monkeypatch):
    monkeypatch.setenv("K", "x")
    bad = tmp_path / "bad.yaml"
    bad.write_text(_base_yaml() +
                   "rules:\n  - name: bad\n    when: {max_input_tokens: 10}\n    pool: nope\n")
    with pytest.raises(ValueError):
        load_config(bad)


def test_ac_x4_rejects_bad_regex(tmp_path, monkeypatch):
    monkeypatch.setenv("K", "x")
    bad = tmp_path / "bad.yaml"
    bad.write_text(_base_yaml() +
                   "rules:\n  - name: bad\n    when: {system_regex: '('}\n    pool: default\n")
    with pytest.raises(ValueError):
        load_config(bad)


def test_ac_x4_allows_templated_pool(tmp_path, monkeypatch):
    monkeypatch.setenv("K", "x")
    ok = tmp_path / "ok.yaml"
    ok.write_text(_base_yaml() +
                  "rules:\n  - name: hint\n    when: {header: x-pool}\n"
                  "    pool: '{{ header.x-pool }}'\n")
    cfg = load_config(ok)  # must not raise despite templated pool
    assert cfg.rules[0].pool == "{{ header.x-pool }}"
