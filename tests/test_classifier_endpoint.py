import httpx
import pytest
import pytest_asyncio
import respx
from httpx import ASGITransport, AsyncClient

from app.core.config import (
    AppConfig, ClassifierConfig, PoolEntry, PriceEntry, ProviderConfig,
    RuleConfig, RuleWhen,
)
from app.main import create_app

VALID_KEY = "test-key-123"
DEEPSEEK_URL = "https://api.deepseek.com/v1/chat/completions"
ANTHROPIC_URL = "https://api.anthropic.com/v1/chat/completions"
OPENAI_URL = "https://api.openai.com/v1/chat/completions"


def _auth():
    return {"Authorization": f"Bearer {VALID_KEY}"}


def _providers():
    return {
        "deepseek": ProviderConfig(base_url="https://api.deepseek.com/v1", api_key="sk-ds"),
        "anthropic": ProviderConfig(base_url="https://api.anthropic.com/v1", api_key="sk-an"),
        "openai": ProviderConfig(base_url="https://api.openai.com/v1", api_key="sk-oa"),
    }


def _cfg(tmp_path, name, *, classifier, pools=None):
    return AppConfig(
        api_key=VALID_KEY,
        db_path=str(tmp_path / f"{name}.db"),
        default_pool="default",
        providers=_providers(),
        pools=pools or {
            "cheap": [PoolEntry(provider="deepseek", model="deepseek-chat")],
            "default": [PoolEntry(provider="anthropic", model="claude-sonnet-5")],
        },
        prices={
            "deepseek-chat": PriceEntry(input_per_1m=0.27, output_per_1m=1.10),
            "claude-sonnet-5": PriceEntry(input_per_1m=3.0, output_per_1m=15.0),
        },
        # only "short-and-simple" exists -> a ~1000-token request reaches stage 2
        rules=[RuleConfig(name="short-and-simple",
                          when=RuleWhen(max_input_tokens=500, has_tools=False),
                          pool="cheap")],
        classifier=classifier,
    )


@pytest_asyncio.fixture
async def make_app(tmp_path):
    made = []

    async def _make(*, classifier, pools=None, name=None):
        cfg = _cfg(tmp_path, name or f"g{len(made)}", classifier=classifier, pools=pools)
        application = create_app(cfg)
        ctx = application.router.lifespan_context(application)
        await ctx.__aenter__()
        made.append((application, ctx))
        c = AsyncClient(transport=ASGITransport(app=application), base_url="http://test")
        return application, c

    yield _make
    for application, ctx in made:
        await ctx.__aexit__(None, None, None)


def _midsize_body():
    # ~1000 tokens (>500, no rule matches) -> reaches the classifier
    return {"model": "m", "messages": [{"role": "user", "content": "x" * 4000}]}


def _short_body():
    return {"model": "m", "messages": [{"role": "user", "content": "hi"}]}


def _label_response(label):
    return httpx.Response(200, json={
        "choices": [{"message": {"content": label}}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 1},
    })


async def _rows(app, cols):
    cur = await app.state.db.connection.execute(f"SELECT {cols} FROM requests")
    return await cur.fetchall()


ENABLED = ClassifierConfig(enabled=True, pool="cheap", labels=["cheap", "default"],
                           fallback_pool="default", timeout_s=5.0)


@respx.mock
async def test_label_routes_to_named_pool(make_app):
    app, client = await make_app(classifier=ENABLED)
    # probe (deepseek) returns label "default"; completion goes to anthropic
    respx.post(DEEPSEEK_URL).mock(return_value=_label_response("default"))
    an = respx.post(ANTHROPIC_URL).mock(
        return_value=httpx.Response(200, json={"id": "done"}))

    resp = await client.post("/v1/chat/completions", headers=_auth(), json=_midsize_body())
    assert resp.status_code == 200 and resp.json()["id"] == "done" and an.called

    rows = await _rows(app, "pool, provider, route_stage, route_reason")
    assert rows == [("default", "anthropic", "classifier", "classifier:label=default")]


@respx.mock
async def test_rules_first_skips_classifier(make_app):
    app, client = await make_app(classifier=ENABLED)
    ds = respx.post(DEEPSEEK_URL).mock(return_value=httpx.Response(200, json={"id": "c"}))

    resp = await client.post("/v1/chat/completions", headers=_auth(), json=_short_body())
    assert resp.status_code == 200
    assert ds.call_count == 1       # only the completion, no probe

    rows = await _rows(app, "route_stage, route_reason")
    assert rows == [("rule", "rule:short-and-simple")]


@respx.mock
async def test_garbage_degrades_to_fallback(make_app):
    app, client = await make_app(classifier=ENABLED)
    respx.post(DEEPSEEK_URL).mock(return_value=_label_response("banana"))
    respx.post(ANTHROPIC_URL).mock(return_value=httpx.Response(200, json={"id": "fb"}))

    resp = await client.post("/v1/chat/completions", headers=_auth(), json=_midsize_body())
    assert resp.status_code == 200 and resp.json()["id"] == "fb"

    rows = await _rows(app, "pool, route_stage, route_reason")
    assert rows == [("default", "classifier", "classifier:garbage->default")]


@respx.mock
async def test_probe_failure_degrades_to_fallback(make_app):
    app, client = await make_app(classifier=ENABLED)
    # probe pool (cheap = deepseek only) exhausts -> caller raises -> error degrade
    respx.post(DEEPSEEK_URL).mock(return_value=httpx.Response(500, json={"error": {"message": "down"}}))
    respx.post(ANTHROPIC_URL).mock(return_value=httpx.Response(200, json={"id": "fb"}))

    resp = await client.post("/v1/chat/completions", headers=_auth(), json=_midsize_body())
    assert resp.status_code == 200 and resp.json()["id"] == "fb"

    rows = await _rows(app, "pool, route_stage, route_reason")
    assert rows == [("default", "classifier", "classifier:error->default")]


@respx.mock
async def test_disabled_classifier_routes_to_default(make_app):
    app, client = await make_app(classifier=ClassifierConfig(enabled=False))
    an = respx.post(ANTHROPIC_URL).mock(return_value=httpx.Response(200, json={"id": "d"}))
    ds = respx.post(DEEPSEEK_URL).mock(return_value=httpx.Response(200, json={"id": "x"}))

    resp = await client.post("/v1/chat/completions", headers=_auth(), json=_midsize_body())
    assert resp.status_code == 200 and an.called and not ds.called   # no probe

    rows = await _rows(app, "pool, route_stage, route_reason")
    assert rows == [("default", "default", "no-rule-matched")]


@respx.mock
async def test_classifier_and_fallback_are_orthogonal(make_app):
    # default pool has two entries; classifier picks default, first entry 503 -> hop
    pools = {
        "cheap": [PoolEntry(provider="deepseek", model="deepseek-chat")],
        "default": [PoolEntry(provider="anthropic", model="claude-sonnet-5"),
                    PoolEntry(provider="openai", model="gpt-4o-mini")],
    }
    app, client = await make_app(classifier=ENABLED, pools=pools)
    respx.post(DEEPSEEK_URL).mock(return_value=_label_response("default"))   # probe
    respx.post(ANTHROPIC_URL).mock(return_value=httpx.Response(503, json={"error": {"message": "x"}}))
    respx.post(OPENAI_URL).mock(return_value=httpx.Response(200, json={"id": "second"}))

    resp = await client.post("/v1/chat/completions", headers=_auth(), json=_midsize_body())
    assert resp.status_code == 200 and resp.json()["id"] == "second"

    rows = await _rows(app, "pool, provider, route_stage, route_reason, fallback_from")
    pool, provider, stage, reason, fallback_from = rows[0]
    assert (pool, provider, stage) == ("default", "openai", "fallback")
    assert reason.startswith("classifier:label=default")
    assert "fallback:" in reason and "hops=1" in reason
    assert fallback_from == "anthropic/claude-sonnet-5"


@respx.mock
async def test_exactly_one_row_per_classified_request(make_app):
    app, client = await make_app(classifier=ENABLED)
    respx.post(DEEPSEEK_URL).mock(return_value=_label_response("default"))
    respx.post(ANTHROPIC_URL).mock(return_value=httpx.Response(200, json={"id": "done"}))

    await client.post("/v1/chat/completions", headers=_auth(), json=_midsize_body())
    cur = await app.state.db.connection.execute("SELECT count(*) FROM requests")
    assert (await cur.fetchone())[0] == 1        # probe added no row
