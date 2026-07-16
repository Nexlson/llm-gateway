"""Independent M4 acceptance tests, derived from the plan's Acceptance Criteria
(not from the implementation). Self-contained: helpers are copied in so the suite
does not depend on other test modules."""
import asyncio
import logging
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
import respx
from httpx import ASGITransport, AsyncClient

from app.core.config import (
    AppConfig, ClassifierConfig, PoolEntry, PriceEntry, ProviderConfig,
    RuleConfig, RuleWhen, load_config,
)
from app.main import create_app
from app.providers.base import ProviderError, ProviderResponse
from app.routing.classifier import Classifier, ClassifierDecision
from app.routing.classifier_caller import ExecutorProbeCaller
from app.routing.executor import ExecutionOutcome
from app.routing.features import RequestFeatures

VALID_KEY = "test-key-123"
DEEPSEEK_URL = "https://api.deepseek.com/v1/chat/completions"
ANTHROPIC_URL = "https://api.anthropic.com/v1/chat/completions"
OPENAI_URL = "https://api.openai.com/v1/chat/completions"

EXAMPLE = Path(__file__).resolve().parents[1] / "config.example.yaml"
EXAMPLE_ENV = {
    "GATEWAY_API_KEY": "test-gateway-key",
    "DEEPSEEK_API_KEY": "test-deepseek-key",
    "ANTHROPIC_API_KEY": "test-anthropic-key",
}


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
    # ~1000 tokens (>500, no rule matches) -> reaches stage 2
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


# --------------------------------------------------------------------------- Config

def test_ac_cfg1_classifier_defaults_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("K", "x")
    ok = tmp_path / "ok.yaml"
    ok.write_text(
        "api_key_env: K\n"
        "default_pool: default\n"
        "db_path: g.db\n"
        "providers:\n  p:\n    base_url: http://x\n    api_key_env: K\n"
        "pools:\n  default:\n    - {provider: p, model: m}\n"
    )
    cfg = load_config(ok)
    assert cfg.classifier.enabled is False


@respx.mock
async def test_ac_cfg1_disabled_app_routes_no_match_to_default(make_app):
    app, client = await make_app(classifier=ClassifierConfig(enabled=False))
    respx.post(ANTHROPIC_URL).mock(return_value=httpx.Response(200, json={"id": "d"}))
    respx.post(DEEPSEEK_URL).mock(return_value=httpx.Response(200, json={"id": "x"}))
    resp = await client.post("/v1/chat/completions", headers=_auth(), json=_midsize_body())
    assert resp.status_code == 200
    rows = await _rows(app, "pool, route_stage, route_reason")
    assert rows == [("default", "default", "no-rule-matched")]


def test_ac_cfg2_example_ships_enabled_classifier(monkeypatch):
    for k, v in EXAMPLE_ENV.items():
        monkeypatch.setenv(k, v)
    cfg = load_config(EXAMPLE)
    assert cfg.classifier.enabled is True
    assert cfg.classifier.pool == "cheap"
    assert cfg.classifier.labels == ["cheap", "default"]
    assert cfg.classifier.fallback_pool == "default"


def _cfg_yaml(block: str) -> str:
    return (
        "api_key_env: K\n"
        "default_pool: default\n"
        "db_path: g.db\n"
        "providers:\n  p:\n    base_url: http://x\n    api_key_env: K\n"
        "pools:\n"
        "  default:\n    - {provider: p, model: m}\n"
        "  cheap:\n    - {provider: p, model: c}\n"
        + block
    )


BAD_BLOCKS = [
    "classifier:\n  enabled: true\n  pool: nope\n  labels: [cheap, default]\n  fallback_pool: default\n",
    "classifier:\n  enabled: true\n  pool: cheap\n  labels: [cheap, default]\n  fallback_pool: nope\n",
    "classifier:\n  enabled: true\n  pool: cheap\n  labels: [cheap, nope]\n  fallback_pool: default\n",
    "classifier:\n  enabled: true\n  pool: cheap\n  labels: []\n  fallback_pool: default\n",
]


@pytest.mark.parametrize("block", BAD_BLOCKS)
def test_ac_cfg3_enabled_validation_rejects_bad_config(tmp_path, monkeypatch, block):
    monkeypatch.setenv("K", "x")
    bad = tmp_path / "bad.yaml"
    bad.write_text(_cfg_yaml(block))
    with pytest.raises(ValueError):
        load_config(bad)


def test_ac_cfg3_disabled_skips_validation(tmp_path, monkeypatch):
    monkeypatch.setenv("K", "x")
    ok = tmp_path / "ok.yaml"
    ok.write_text(_cfg_yaml(
        "classifier:\n  enabled: false\n  pool: nope\n  labels: [nope]\n  fallback_pool: nope\n"))
    cfg = load_config(ok)
    assert cfg.classifier.enabled is False


# ---------------------------------------------------------------- Routing (enabled)

@respx.mock
async def test_ac_r1_label_path(make_app):
    app, client = await make_app(classifier=ENABLED)
    respx.post(DEEPSEEK_URL).mock(return_value=_label_response("default"))
    respx.post(ANTHROPIC_URL).mock(return_value=httpx.Response(200, json={"id": "done"}))
    resp = await client.post("/v1/chat/completions", headers=_auth(), json=_midsize_body())
    assert resp.status_code == 200
    rows = await _rows(app, "pool, provider, model, route_stage, route_reason, status")
    assert rows == [("default", "anthropic", "claude-sonnet-5", "classifier",
                     "classifier:label=default", 200)]


@respx.mock
async def test_ac_r2_rules_first_skips_classifier(make_app):
    app, client = await make_app(classifier=ENABLED)
    ds = respx.post(DEEPSEEK_URL).mock(return_value=httpx.Response(200, json={"id": "c"}))
    resp = await client.post("/v1/chat/completions", headers=_auth(), json=_short_body())
    assert resp.status_code == 200
    assert ds.call_count == 1        # completion only, no probe
    rows = await _rows(app, "route_stage, route_reason")
    assert rows == [("rule", "rule:short-and-simple")]


@respx.mock
async def test_ac_r3_garbage_to_fallback(make_app):
    app, client = await make_app(classifier=ENABLED)
    respx.post(DEEPSEEK_URL).mock(return_value=_label_response("banana"))
    an = respx.post(ANTHROPIC_URL).mock(return_value=httpx.Response(200, json={"id": "fb"}))
    resp = await client.post("/v1/chat/completions", headers=_auth(), json=_midsize_body())
    assert resp.status_code == 200 and an.called
    rows = await _rows(app, "pool, route_stage, route_reason")
    assert rows == [("default", "classifier", "classifier:garbage->default")]


@respx.mock
async def test_ac_r4_error_to_fallback(make_app):
    app, client = await make_app(classifier=ENABLED)
    respx.post(DEEPSEEK_URL).mock(
        return_value=httpx.Response(500, json={"error": {"message": "down"}}))
    an = respx.post(ANTHROPIC_URL).mock(return_value=httpx.Response(200, json={"id": "fb"}))
    resp = await client.post("/v1/chat/completions", headers=_auth(), json=_midsize_body())
    assert resp.status_code == 200 and an.called
    rows = await _rows(app, "pool, route_stage, route_reason")
    assert rows == [("default", "classifier", "classifier:error->default")]


async def test_ac_r5_timeout_to_fallback_unit():
    async def caller(probe):
        raise asyncio.TimeoutError
    clf = Classifier(caller=caller, labels=["cheap", "default"], fallback_pool="default")
    d = await clf.classify(RequestFeatures(input_tokens=0, has_tools=False,
                                           system_prompt="", headers={}, last_user_text="q"))
    assert d == ClassifierDecision(pool="default", reason="classifier:timeout->default")


@respx.mock
async def test_ac_r6_disabled_preserves_m2_m3(make_app):
    app, client = await make_app(classifier=ClassifierConfig(enabled=False))
    an = respx.post(ANTHROPIC_URL).mock(return_value=httpx.Response(200, json={"id": "d"}))
    ds = respx.post(DEEPSEEK_URL).mock(return_value=httpx.Response(200, json={"id": "x"}))
    resp = await client.post("/v1/chat/completions", headers=_auth(), json=_midsize_body())
    assert resp.status_code == 200 and an.called and not ds.called
    rows = await _rows(app, "pool, route_stage, route_reason")
    assert rows == [("default", "default", "no-rule-matched")]


@respx.mock
async def test_ac_r7_classifier_and_fallback_orthogonal(make_app):
    pools = {
        "cheap": [PoolEntry(provider="deepseek", model="deepseek-chat")],
        "default": [PoolEntry(provider="anthropic", model="claude-sonnet-5"),
                    PoolEntry(provider="openai", model="gpt-4o-mini")],
    }
    app, client = await make_app(classifier=ENABLED, pools=pools)
    respx.post(DEEPSEEK_URL).mock(return_value=_label_response("default"))
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
@pytest.mark.parametrize("probe_response", [
    _label_response("banana"),                                   # garbage
    httpx.Response(500, json={"error": {"message": "down"}}),    # error
])
async def test_ac_r8_classifier_failure_never_fails_request(make_app, probe_response):
    app, client = await make_app(classifier=ENABLED)
    respx.post(DEEPSEEK_URL).mock(return_value=probe_response)
    respx.post(ANTHROPIC_URL).mock(return_value=httpx.Response(200, json={"id": "fb"}))
    resp = await client.post("/v1/chat/completions", headers=_auth(), json=_midsize_body())
    assert resp.status_code == 200 and resp.json()["id"] == "fb"


# ------------------------------------------------------------- Classifier unit (U)

def _features(system="", last_user=""):
    return RequestFeatures(input_tokens=0, has_tools=False, system_prompt=system,
                           headers={}, last_user_text=last_user)


def _unit_classifier(caller, **kw):
    return Classifier(caller=caller, labels=["cheap", "default"],
                      fallback_pool="default", **kw)


async def test_ac_u1_valid_label():
    async def caller(probe):
        return " default\n"
    d = await _unit_classifier(caller).classify(_features(last_user="q"))
    assert d == ClassifierDecision(pool="default", reason="classifier:label=default")


async def test_ac_u2_timeout_reason():
    async def caller(probe):
        raise asyncio.TimeoutError
    d = await _unit_classifier(caller).classify(_features(last_user="q"))
    assert d == ClassifierDecision(pool="default", reason="classifier:timeout->default")


async def test_ac_u3_error_reason():
    async def caller(probe):
        raise RuntimeError("boom")
    d = await _unit_classifier(caller).classify(_features(last_user="q"))
    assert d == ClassifierDecision(pool="default", reason="classifier:error->default")


@pytest.mark.parametrize("output", ["banana", "", None])
async def test_ac_u4_garbage_reason(output):
    async def caller(probe):
        return output
    d = await _unit_classifier(caller).classify(_features(last_user="q"))
    assert d == ClassifierDecision(pool="default", reason="classifier:garbage->default")


async def test_ac_u5_hard_timeout_bounded():
    async def caller(probe):
        await asyncio.sleep(5)
        return "cheap"
    loop = asyncio.get_event_loop()
    start = loop.time()
    d = await _unit_classifier(caller, timeout_s=0.05).classify(_features(last_user="q"))
    assert d.reason == "classifier:timeout->default"
    assert loop.time() - start < 1.0


async def test_ac_u6_truncated_and_constrained_probe():
    captured = {}

    async def caller(probe):
        captured["probe"] = probe
        return "default"

    await _unit_classifier(caller, max_probe_chars=2000, max_output_tokens=8).classify(
        _features(system="S" * 5000, last_user="U" * 5000))
    probe = captured["probe"]
    assert [m["role"] for m in probe["messages"]] == ["system", "user"]
    assert probe["max_tokens"] == 8
    content = probe["messages"][1]["content"]
    assert "S" * 2000 in content and "S" * 2001 not in content
    assert "U" * 2000 in content and "U" * 2001 not in content


# ---------------------------------------------------------------- Caller unit (P)

class _FakePoolResolver:
    def first_entry(self, name):
        return PoolEntry(provider="deepseek", model="deepseek-chat")


class _FakeExecutor:
    def __init__(self, outcome):
        self._outcome = outcome
        self.calls = []

    async def execute(self, body, resolution):
        self.calls.append((body, resolution))
        return self._outcome


def _outcome(response=None, error=None):
    return ExecutionOutcome(provider="deepseek", model="deepseek-chat",
                            response=response, error=error,
                            fallback_from=None, fallback_hops=0)


async def test_ac_p1_extracts_text_and_targets_pool():
    resp = ProviderResponse(200, {"choices": [{"message": {"content": "cheap"}}]}, 1, 1)
    ex = _FakeExecutor(_outcome(response=resp))
    caller = ExecutorProbeCaller(ex, _FakePoolResolver(), "cheap")
    out = await caller({"messages": []})
    assert out == "cheap"
    assert ex.calls[0][1].pool == "cheap"


async def test_ac_p2_raises_on_exhaustion():
    err = ProviderError("down", 503, "deepseek", retryable=True)
    ex = _FakeExecutor(_outcome(response=None, error=err))
    caller = ExecutorProbeCaller(ex, _FakePoolResolver(), "cheap")
    with pytest.raises(RuntimeError):
        await caller({"messages": []})


async def test_ac_p3_raises_on_malformed_body():
    resp = ProviderResponse(200, {"choices": []}, 1, 1)
    ex = _FakeExecutor(_outcome(response=resp))
    caller = ExecutorProbeCaller(ex, _FakePoolResolver(), "cheap")
    with pytest.raises((KeyError, IndexError, TypeError)):
        await caller({"messages": []})


# ------------------------------------------------------------- No regression (X)

@respx.mock
async def test_ac_x1_one_row_one_log_line(make_app):
    app, client = await make_app(classifier=ENABLED)
    respx.post(DEEPSEEK_URL).mock(return_value=_label_response("default"))
    respx.post(ANTHROPIC_URL).mock(return_value=httpx.Response(200, json={"id": "done"}))

    cap = []

    class H(logging.Handler):
        def emit(self, rec):
            cap.append(rec)

    handler = H()
    logging.getLogger("gateway").addHandler(handler)
    try:
        await client.post("/v1/chat/completions", headers=_auth(), json=_midsize_body())
    finally:
        logging.getLogger("gateway").removeHandler(handler)

    cur = await app.state.db.connection.execute("SELECT count(*) FROM requests")
    assert (await cur.fetchone())[0] == 1

    events = [r for r in cap if getattr(r, "event", None) == "request"]
    assert len(events) == 1
    assert events[0].__dict__["route_stage"] == "classifier"
