# M2 — YAML Rule Engine + Pools + Cost Table Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the M1 passthrough proxy into a rule-routed gateway: a deterministic stage-1 YAML rule engine chooses a **pool** from request features (token thresholds, tool presence, header hints, system-prompt regex); the pool's first entry is called; real `cost_usd` is computed at write time from a populated price table; and every row records `route_stage`/`route_reason` describing the decision.

**Architecture:** Insert a pure, unit-testable routing layer between the handler and the provider call. `extract_features(body, headers)` produces a `RequestFeatures` value object. `RuleEngine.match(features)` evaluates config rules in order (first match wins) and returns a pool name. `Router` composes feature extraction + rule engine + `PoolResolver` (pool name → ordered entries) into a `Resolution`. `GatewayService` calls `Router.route(...)` instead of the M1 `PoolResolver.resolve(model)`, then calls the resolved provider exactly as before. Layering stays: handler → routing service → repository; routing is a pure function of (features, config).

**Tech Stack:** Python 3.11+, FastAPI, Pydantic v2, PyYAML (all already present). No new dependencies. Token counting uses a documented character-based heuristic (no tokenizer dependency added). Tests: pytest, pytest-asyncio, respx, httpx.AsyncClient (existing setup).

## Global Constraints

- **Rules before anything else, deterministic.** Stage 1 is pure YAML matching evaluated **in order, first match wins**. The classifier (M4) does not exist yet; there is no model call in the routing path. Routing must never perform I/O and must never fail the request — an unresolvable hint degrades safely to `default_pool`.
- **Config-driven, no hardcoding.** Rules, pools, and prices come only from `config.yaml` (loaded once at boot in `core/config.py`). No model names, thresholds, or prices in code. Update `config.example.yaml`; secrets stay env-var-sourced (unchanged from M1 — never commit real keys).
- **Cost at write time, never recomputed.** `cost_usd` is computed from the **provider-reported `usage`** tokens against the static price table and stored on the row. Historical rows are never recomputed when prices change. The routing token *estimate* is a separate quantity and must NOT be used for billing.
- **OpenAI compatibility must not regress.** Request/response bodies pass through unchanged except the `model` field (rewritten to the resolved upstream model). Unknown fields forwarded. Errors in OpenAI error shape. Constant-time bearer auth on `/v1/*` and `/dashboard`.
- **Ordered-pool shape preserved.** Pools remain ordered lists; M2 uses only the first entry, but `PoolResolver` exposes the full ordered list so M3's fallback chain iterates it without rework.
- **Scope guard (do NOT build in M2):** classifier / stage 2 (M4), fallback chain + 60s cooldown (M3), dashboard UI (M5), Docker/VPS deploy (M6), and everything in the architecture "Deferred" table (multi-key auth, Prometheus/OTel, latency routing, streaming/SSE, live config reload, semantic caching).
- **Tests never hit real provider APIs.** Mock outbound HTTP with respx. Temp-file SQLite (not `:memory:`) so WAL applies.

---

## M2 Design Decisions (documented assumptions)

These resolve ambiguities the task flagged. The owner authorized autonomous progress, so each is a chosen, documented default — none is blocking.

1. **Incoming `model` no longer selects the pool.** In M1 the request `model` field was treated as the pool name. In M2 the **rule engine** chooses the pool from request features; the `model` field is forwarded (rewritten to the resolved upstream model) but does **not** drive routing. Explicit pool selection now goes through the **`x-pool` header** (the architecture's `explicit-hint` mechanism), not the `model` field. This is exactly the shift the task instructs ("instead of treating the incoming `model` as the pool name"). `model` remains **required** for OpenAI compatibility (a missing `model` still returns 400, as OpenAI itself does) — it is just not the routing key.

2. **Token counting is a documented heuristic, not a tokenizer.** No tokenizer dependency exists and the task says keep it minimal and swappable. `estimate_tokens(text)` uses **~4 characters per token** (`(len(text) + 3) // 4`) over the concatenated message text. It is used **only** for rule thresholds (`max_input_tokens` / `min_input_tokens`), never for cost. It is isolated in one function so a real tokenizer can replace it later without touching the rule engine. **Flagged**: thresholds in `config.yaml` are approximate under this heuristic; tune them against real traffic (that is the point of recording `route_reason`).

3. **`route_stage` value for the no-rule-match case is `"default"`.** The schema comment lists `rule | classifier | fallback`. A request that matches no rule cannot be `classifier` (M4 not built) or `fallback` (M3 not built), so M2 writes the documented value **`route_stage="default"`** with `route_reason="no-rule-matched"`. The column is `TEXT` (unconstrained), so this is schema-compatible; M4 replaces the no-match path with the classifier. A matched rule writes `route_stage="rule"`.

4. **Unsafe hints degrade to `default_pool`, never fail the request.** If a rule resolves to a pool name that is not defined (e.g. an `x-pool: bogus` header via the templated `explicit-hint` rule), the router routes to `default_pool` and records a documented reason (`rule:<name>:unknown-pool:<value>`). This honors the invariant that routing is a hint that must never fail a request.

5. **Explicit hints are header-only in V1.** The architecture mentions "header/metadata". M2 implements **header** hints (`when: { header: "x-pool" }` + templated `pool: "{{ header.x-pool }}"`). A body `metadata` object is **not** a matchable source in M2 (kept out to stay minimal). Flagged as a deliberate narrowing; easy to add later as another `when` key.

6. **Pool templating supports exactly `{{ header.<name>}}` as the whole value.** Only a pure-template pool value is supported (KISS) — e.g. `pool: "{{ header.x-pool }}"`. Mixed literal+template strings are treated as literals. Header names are matched case-insensitively.

7. **Empty `when: {}` (or omitted) is a catch-all** that matches every request — useful as a final default rule. An empty `rules:` list means every request routes to `default_pool` (`route_stage="default"`).

8. **Example prices are illustrative, not authoritative.** `config.example.yaml` ships plausible per-1M-token rates for the example models so cost is exercised end-to-end. They are **not secrets** and operators must verify them against live provider pricing. Populating them does not change any code — M1 already computes cost from this table.

---

## File Structure

**Create (application):**
- `app/routing/features.py` — `RequestFeatures` value object + `estimate_tokens` + `extract_features`.
- `app/routing/rules.py` — `RuleMatch` + `RuleEngine` (pure stage-1 matcher).
- `app/routing/router.py` — `Resolution` + `Router` (features + rules + pools → resolution).

**Modify (application):**
- `app/core/config.py` — add `RuleWhen`, `RuleConfig`, `AppConfig.rules`; extend `_validate` to check rule literal pools + compile regexes at boot.
- `app/routing/pools.py` — refactor `PoolResolver` to a pool-name → ordered-entries accessor; remove the M1 `resolve()`/`Resolution` (now owned by `router.py`).
- `app/routing/gateway.py` — `GatewayService` takes a `Router`; `complete(request_id, body, headers)`; import `Resolution` from `router.py`.
- `app/api/v1/endpoints/chat.py` — pass `dict(request.headers)` into `gateway.complete`.
- `app/main.py` — build `RuleEngine`, `PoolResolver`, `Router` in lifespan; inject `Router` into `GatewayService`.
- `config.example.yaml` — add a `rules:` block and populate `prices:`.

**Create (tests):**
- `tests/test_features.py`, `tests/test_rules.py`, `tests/test_router.py`

**Modify (tests):**
- `tests/conftest.py` — add `rules` (+ populated `prices`) to `test_config`.
- `tests/test_pools.py` — rewrite for the new `PoolResolver` interface.
- `tests/test_gateway.py` — new `GatewayService(router, ...)` construction + `complete(..., headers)`.
- `tests/test_config.py` — assert `rules` parse and validate; assert example `prices` non-empty.
- `tests/test_chat_endpoint.py` — assert rule-based routing (model no longer selects pool) + non-zero cost row.
- `tests/test_app_integration.py` — end-to-end rule routing + cost.

---

## Acceptance Criteria (for the tester — derivable without reading implementation)

Assume a config with pools `cheap` (first entry `deepseek/deepseek-chat`), `default` (first entry `anthropic/claude-sonnet-5`), `large-context` (first entry `anthropic/claude-sonnet-5`); `default_pool: default`; populated `prices` including `deepseek-chat` and `claude-sonnet-5`; and rules, in order:
1. `explicit-hint` — `when: {header: "x-pool"}`, `pool: "{{ header.x-pool }}"`
2. `has-tools` — `when: {has_tools: true}`, `pool: default`
3. `long-context` — `when: {min_input_tokens: 20000}`, `pool: large-context`
4. `short-and-simple` — `when: {max_input_tokens: 500, has_tools: false}`, `pool: cheap`

**Routing**

- **AC-R1 First match wins, in order.** A short, tool-less request whose `x-pool` header is absent matches `short-and-simple` → pool `cheap`. Adding header `x-pool: default` makes `explicit-hint` win instead → pool `default` (rule 1 precedes rule 4).
- **AC-R2 Token threshold (max).** A tool-less request with total message text estimated at ≤ 500 tokens routes to `cheap`; the same request grown well past 500 tokens (but under 20000) matches no threshold rule and routes to `default_pool` with `route_stage="default"`.
- **AC-R3 Token threshold (min).** A tool-less request with message text estimated ≥ 20000 tokens matches `long-context` → pool `large-context`.
- **AC-R4 Tool presence.** A request containing a non-empty `tools` (or `functions`) array matches `has-tools` → pool `default`, even when short. The same request without tools does not match `has-tools`.
- **AC-R5 Explicit header hint.** Header `x-pool: cheap` (any casing of the header name) routes to `cheap` with `route_stage="rule"` and `route_reason` containing `explicit-hint`.
- **AC-R6 Unsafe hint degrades safely.** Header `x-pool: does-not-exist` does **not** fail the request: it routes to `default_pool`, `route_stage="default"`, and `route_reason` records the rejected pool name (contains `unknown-pool` and `does-not-exist`). Response is still 200 given a healthy mocked provider.
- **AC-R7 System-prompt regex.** With an added rule `when: {system_regex: "(?i)translate"}` → `pool: cheap`, a request whose `system` message contains "Translate" routes to `cheap`; a request without that word does not match it.
- **AC-R8 No rule matches → default.** With an empty `rules:` list, every request routes to `default_pool` with `route_stage="default"` and `route_reason="no-rule-matched"`.
- **AC-R9 Decision persisted.** After any routed request, the `requests` row stores `route_stage` ∈ {`rule`, `default`} and a non-empty human-readable `route_reason` naming the matched rule (or `no-rule-matched`). The stdout JSON log line for that request carries the same `route_stage`.
- **AC-R10 Routing is pure and deterministic.** `extract_features` + `RuleEngine.match` produce identical results for identical `(body, headers)` and perform no I/O and no model call. The router never raises for any body/header combination that is a JSON object.

**Cost**

- **AC-C1 Real cost at write time.** With `prices["deepseek-chat"] = {input_per_1m: 0.27, output_per_1m: 1.10}` and a mocked provider returning `usage: {prompt_tokens: 1_000_000, completion_tokens: 1_000_000}`, the stored `cost_usd` equals `0.27 + 1.10 = 1.37` (within float tolerance).
- **AC-C2 Cost uses provider usage, not the routing estimate.** The stored `cost_usd` and `input_tokens`/`output_tokens` derive from the provider `usage`, independent of the heuristic token estimate used for thresholds. A request routed by an estimated-token threshold still stores the provider's reported token counts.
- **AC-C3 Unknown model → 0.0.** A resolved model with no `prices` entry stores `cost_usd = 0.0` (no error).
- **AC-C4 Prices exercised in the committed example.** `config.example.yaml` ships a non-empty `prices` map; `load_config` on the example yields `cfg.prices` non-empty.

**Compatibility (must not regress from M1)**

- **AC-X1 Body unchanged, model rewritten.** The client receives the provider response body byte-for-content unchanged. The request forwarded upstream has its `model` rewritten to the resolved pool's first-entry model, with all other fields (including unknown ones and `temperature`) preserved.
- **AC-X2 Missing `model` → 400.** A body lacking `model` returns 400 in OpenAI error shape (`type` == `invalid_request_error`, `param` == `model`); no upstream call and no row.  (Routing is by rules, but `model` is still required for OpenAI compatibility.)
- **AC-X3 Auth unchanged.** `/v1/chat/completions`, `/v1/models`, `/dashboard` require the bearer key (401 without it); `/healthz` does not.
- **AC-X4 Config validation.** `load_config` raises if a rule's literal `pool` (non-templated) is not a defined pool, or if a rule's `system_regex` fails to compile. Templated pools (`{{ header.* }}`) are not statically validated (resolved safely at runtime per AC-R6).

---

## Task 1: Config models for rules + populated example prices

**Milestone:** M2.

**Files:**
- Modify: `app/core/config.py`, `config.example.yaml`
- Modify (test): `tests/test_config.py`

**Interfaces:**
- Consumes: existing `AppConfig`, `PriceEntry`, `load_config`, `_validate`.
- Produces:
  - `app.core.config.RuleWhen(BaseModel)` with optional fields `max_input_tokens: int | None = None`, `min_input_tokens: int | None = None`, `has_tools: bool | None = None`, `header: str | None = None`, `system_regex: str | None = None`.
  - `app.core.config.RuleConfig(BaseModel)` with `name: str`, `when: RuleWhen = Field(default_factory=RuleWhen)`, `pool: str`.
  - `app.core.config.AppConfig.rules: list[RuleConfig] = Field(default_factory=list)` (new field; order preserved as written).
  - `load_config` now also: rejects a rule whose literal (non-templated) `pool` is not a defined pool; rejects a rule whose `system_regex` does not compile.

- [ ] **Step 1: Add failing tests to `tests/test_config.py`** (append):

```python
import re

def test_loads_rules_in_order(monkeypatch):
    _set_env(monkeypatch, EXAMPLE_ENV)
    cfg = load_config(EXAMPLE)
    names = [r.name for r in cfg.rules]
    assert names[0] == "explicit-hint"          # explicit hint evaluated first
    assert "short-and-simple" in names
    # prices are now populated (M2)
    assert cfg.prices != {}
    assert "deepseek-chat" in cfg.prices


def test_rejects_rule_literal_pool_unknown(tmp_path, monkeypatch):
    monkeypatch.setenv("K", "x")
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        "api_key_env: K\n"
        "default_pool: default\n"
        "db_path: g.db\n"
        "providers:\n  p:\n    base_url: http://x\n    api_key_env: K\n"
        "pools:\n  default:\n    - {provider: p, model: m}\n"
        "rules:\n  - name: bad\n    when: {max_input_tokens: 10}\n    pool: nope\n"
    )
    with pytest.raises(ValueError):
        load_config(bad)


def test_rejects_rule_bad_regex(tmp_path, monkeypatch):
    monkeypatch.setenv("K", "x")
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        "api_key_env: K\n"
        "default_pool: default\n"
        "db_path: g.db\n"
        "providers:\n  p:\n    base_url: http://x\n    api_key_env: K\n"
        "pools:\n  default:\n    - {provider: p, model: m}\n"
        "rules:\n  - name: bad\n    when: {system_regex: '('}\n    pool: default\n"
    )
    with pytest.raises(ValueError):
        load_config(bad)


def test_allows_templated_pool_without_static_pool_check(tmp_path, monkeypatch):
    monkeypatch.setenv("K", "x")
    ok = tmp_path / "ok.yaml"
    ok.write_text(
        "api_key_env: K\n"
        "default_pool: default\n"
        "db_path: g.db\n"
        "providers:\n  p:\n    base_url: http://x\n    api_key_env: K\n"
        "pools:\n  default:\n    - {provider: p, model: m}\n"
        "rules:\n  - name: hint\n    when: {header: x-pool}\n    pool: '{{ header.x-pool }}'\n"
    )
    cfg = load_config(ok)             # must not raise despite templated pool
    assert cfg.rules[0].pool == "{{ header.x-pool }}"
```

- [ ] **Step 2: Run to verify failure** — `pytest tests/test_config.py -v` → the new tests FAIL (`AppConfig` has no `rules`; prices empty).

- [ ] **Step 3: Edit `app/core/config.py`** — add the models and validation. Insert after `PriceEntry`:

```python
class RuleWhen(BaseModel):
    max_input_tokens: int | None = None
    min_input_tokens: int | None = None
    has_tools: bool | None = None
    header: str | None = None
    system_regex: str | None = None


class RuleConfig(BaseModel):
    name: str
    when: "RuleWhen" = Field(default_factory=lambda: RuleWhen())
    pool: str
```

Add to `AppConfig` (new field, after `prices`):

```python
    rules: list[RuleConfig] = Field(default_factory=list)
```

Extend `_validate` — append inside the function:

```python
    _TEMPLATE_ONLY = re.compile(r"^\s*\{\{\s*header\.[A-Za-z0-9_-]+\s*\}\}\s*$")
    for rule in cfg.rules:
        if rule.when.system_regex is not None:
            try:
                re.compile(rule.when.system_regex)
            except re.error as exc:
                raise ValueError(
                    f"rule '{rule.name}' has invalid system_regex: {exc}"
                ) from exc
        if not _TEMPLATE_ONLY.match(rule.pool) and rule.pool not in cfg.pools:
            raise ValueError(
                f"rule '{rule.name}' references unknown pool '{rule.pool}'"
            )
```

Add `import re` at the top of the module (after `import os`).

- [ ] **Step 4: Edit `config.example.yaml`** — replace the `prices: {}` line and append a `rules:` block:

```yaml
# Illustrative rates (NOT secrets, NOT authoritative) — verify against live provider
# pricing before production. cost_usd is computed at write time from provider usage.
prices:
  deepseek-chat:    { input_per_1m: 0.27, output_per_1m: 1.10 }
  claude-haiku-4-5: { input_per_1m: 1.00, output_per_1m: 5.00 }
  claude-sonnet-5:  { input_per_1m: 3.00, output_per_1m: 15.00 }

# Stage-1 deterministic rules, evaluated in order — first match wins.
# Token thresholds use a ~4-chars/token estimate (routing only, not billing).
rules:
  - name: explicit-hint            # caller forces a pool via the x-pool header
    when: { header: "x-pool" }
    pool: "{{ header.x-pool }}"
  - name: has-tools                # tool/function calling → capable default pool
    when: { has_tools: true }
    pool: default
  - name: long-context            # very long inputs → large-context pool
    when: { min_input_tokens: 20000 }
    pool: large-context
  - name: short-and-simple         # short, tool-less → cheap pool
    when: { max_input_tokens: 500, has_tools: false }
    pool: cheap
```

- [ ] **Step 5: Run to verify pass** — `pytest tests/test_config.py -v` → PASS.

- [ ] **Step 6: Commit**

```bash
git add app/core/config.py config.example.yaml tests/test_config.py
git commit -m "add rule config models, validation, and populated example prices"
```

---

## Task 2: Request feature extraction + token estimate

**Milestone:** M2.

**Files:**
- Create: `app/routing/features.py`, `tests/test_features.py`

**Interfaces:**
- Consumes: nothing (pure).
- Produces:
  - `app.routing.features.RequestFeatures` dataclass: `input_tokens: int`, `has_tools: bool`, `system_prompt: str`, `headers: dict[str, str]` (header names lowercased).
  - `app.routing.features.estimate_tokens(text: str) -> int` — `(len(text) + 3) // 4`; `0` for empty. Routing-only approximation; swappable.
  - `app.routing.features.extract_features(body: dict, headers: dict[str, str]) -> RequestFeatures` — sums estimated tokens over all message text; `has_tools` true when `body["tools"]` or `body["functions"]` is non-empty; `system_prompt` is the newline-joined text of all `role == "system"` messages; header names are lowercased.

- [ ] **Step 1: Write the failing test** `tests/test_features.py`:

```python
from app.routing.features import RequestFeatures, estimate_tokens, extract_features


def test_estimate_tokens_heuristic():
    assert estimate_tokens("") == 0
    assert estimate_tokens("abcd") == 1          # 4 chars ≈ 1 token
    assert estimate_tokens("abcde") == 2         # ceil(5/4)


def test_extract_no_tools_short():
    f = extract_features(
        {"model": "x", "messages": [{"role": "user", "content": "hello"}]},
        headers={},
    )
    assert isinstance(f, RequestFeatures)
    assert f.has_tools is False
    assert f.input_tokens == estimate_tokens("hello")
    assert f.system_prompt == ""


def test_extract_detects_tools_and_functions():
    assert extract_features({"tools": [{"type": "function"}]}, {}).has_tools is True
    assert extract_features({"functions": [{"name": "f"}]}, {}).has_tools is True
    assert extract_features({"tools": []}, {}).has_tools is False


def test_extract_system_prompt_and_headers_lowercased():
    f = extract_features(
        {"messages": [
            {"role": "system", "content": "Translate to French"},
            {"role": "user", "content": "hi"},
        ]},
        headers={"X-Pool": "cheap"},
    )
    assert f.system_prompt == "Translate to French"
    assert f.headers == {"x-pool": "cheap"}


def test_extract_handles_list_content_parts():
    f = extract_features(
        {"messages": [{"role": "user",
                       "content": [{"type": "text", "text": "abcd"},
                                   {"type": "image_url", "image_url": {"url": "x"}}]}]},
        headers={},
    )
    assert f.input_tokens == estimate_tokens("abcd")
```

- [ ] **Step 2: Run to verify failure** — `pytest tests/test_features.py -v` → FAIL (module missing).

- [ ] **Step 3: Write `app/routing/features.py`**

```python
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class RequestFeatures:
    input_tokens: int
    has_tools: bool
    system_prompt: str
    headers: dict[str, str]


def estimate_tokens(text: str) -> int:
    # Routing-only heuristic (~4 chars/token). NOT used for billing — cost comes
    # from provider `usage`. Isolated here so a real tokenizer can replace it.
    if not text:
        return 0
    return (len(text) + 3) // 4


def _content_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            p["text"]
            for p in content
            if isinstance(p, dict) and isinstance(p.get("text"), str)
        ]
        return " ".join(parts)
    return ""


def extract_features(body: dict, headers: dict[str, str]) -> RequestFeatures:
    messages = body.get("messages") or []
    all_text: list[str] = []
    system_text: list[str] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        text = _content_text(message.get("content"))
        all_text.append(text)
        if message.get("role") == "system":
            system_text.append(text)

    has_tools = bool(body.get("tools")) or bool(body.get("functions"))
    return RequestFeatures(
        input_tokens=estimate_tokens("".join(all_text)),
        has_tools=has_tools,
        system_prompt="\n".join(system_text),
        headers={k.lower(): v for k, v in headers.items()},
    )
```

- [ ] **Step 4: Run to verify pass** — `pytest tests/test_features.py -v` → PASS.

- [ ] **Step 5: Commit**

```bash
git add app/routing/features.py tests/test_features.py
git commit -m "add request feature extraction and routing token estimate"
```

---

## Task 3: Stage-1 rule engine

**Milestone:** M2.

**Files:**
- Create: `app/routing/rules.py`, `tests/test_rules.py`

**Interfaces:**
- Consumes: `RuleConfig`, `RuleWhen` (Task 1); `RequestFeatures` (Task 2).
- Produces:
  - `app.routing.rules.RuleMatch` dataclass: `pool: str`, `rule_name: str`.
  - `app.routing.rules.RuleEngine(rules: list[RuleConfig])` — compiles any `system_regex` at construction. Method `match(features: RequestFeatures) -> RuleMatch | None` returns the first rule whose `when` fully matches (AND over specified conditions; unspecified conditions ignored), resolving the pool template. A rule with a `{{ header.<name> }}` pool whose header is absent/empty is **skipped** (evaluation continues). Returns `None` when no rule matches.
  - Match semantics: `max_input_tokens` matches when `input_tokens <= value`; `min_input_tokens` matches when `input_tokens >= value`; `has_tools` matches when equal; `header` matches when that (case-insensitive) header is present and non-empty; `system_regex` matches when `re.search` finds it in `system_prompt`.

- [ ] **Step 1: Write the failing test** `tests/test_rules.py`:

```python
from app.core.config import RuleConfig, RuleWhen
from app.routing.features import RequestFeatures
from app.routing.rules import RuleEngine, RuleMatch


def _features(tokens=100, tools=False, system="", headers=None):
    return RequestFeatures(tokens, tools, system, headers or {})


HINT = RuleConfig(name="explicit-hint", when=RuleWhen(header="x-pool"),
                  pool="{{ header.x-pool }}")
TOOLS = RuleConfig(name="has-tools", when=RuleWhen(has_tools=True), pool="default")
LONG = RuleConfig(name="long-context", when=RuleWhen(min_input_tokens=20000),
                  pool="large-context")
SHORT = RuleConfig(name="short-and-simple",
                   when=RuleWhen(max_input_tokens=500, has_tools=False), pool="cheap")
RULES = [HINT, TOOLS, LONG, SHORT]


def test_first_match_wins_short():
    m = RuleEngine(RULES).match(_features(tokens=100))
    assert m == RuleMatch(pool="cheap", rule_name="short-and-simple")


def test_header_hint_beats_later_rules():
    m = RuleEngine(RULES).match(_features(tokens=100, headers={"x-pool": "default"}))
    assert m == RuleMatch(pool="default", rule_name="explicit-hint")


def test_tools_route_to_default_even_when_short():
    m = RuleEngine(RULES).match(_features(tokens=100, tools=True))
    assert m == RuleMatch(pool="default", rule_name="has-tools")


def test_long_context_min_threshold():
    m = RuleEngine(RULES).match(_features(tokens=25000))
    assert m == RuleMatch(pool="large-context", rule_name="long-context")


def test_no_rule_matches_returns_none():
    # medium length, no tools, no header: matches none of the threshold rules
    assert RuleEngine(RULES).match(_features(tokens=5000)) is None


def test_template_header_absent_skips_rule():
    # explicit-hint's when(header) fails when header missing, so it never fires;
    # even a templated pool never yields an empty pool name.
    m = RuleEngine([HINT, SHORT]).match(_features(tokens=100))
    assert m == RuleMatch(pool="cheap", rule_name="short-and-simple")


def test_system_regex_match():
    rule = RuleConfig(name="translate", when=RuleWhen(system_regex="(?i)translate"),
                      pool="cheap")
    engine = RuleEngine([rule])
    assert engine.match(_features(system="Please Translate this")).pool == "cheap"
    assert engine.match(_features(system="summarize this")) is None


def test_empty_when_is_catch_all():
    rule = RuleConfig(name="catch", when=RuleWhen(), pool="default")
    assert RuleEngine([rule]).match(_features(tokens=99999)).pool == "default"
```

- [ ] **Step 2: Run to verify failure** — `pytest tests/test_rules.py -v` → FAIL (module missing).

- [ ] **Step 3: Write `app/routing/rules.py`**

```python
from __future__ import annotations

import re
from dataclasses import dataclass

from app.core.config import RuleConfig, RuleWhen
from app.routing.features import RequestFeatures

_TEMPLATE = re.compile(r"^\s*\{\{\s*header\.([A-Za-z0-9_-]+)\s*\}\}\s*$")


@dataclass
class RuleMatch:
    pool: str
    rule_name: str


class RuleEngine:
    def __init__(self, rules: list[RuleConfig]) -> None:
        self._rules = rules
        # Precompile system_regex per rule (index-aligned; None where unset).
        self._regex: list[re.Pattern[str] | None] = [
            re.compile(r.when.system_regex) if r.when.system_regex is not None else None
            for r in rules
        ]

    def match(self, features: RequestFeatures) -> RuleMatch | None:
        for rule, pattern in zip(self._rules, self._regex):
            if not self._when_matches(rule.when, pattern, features):
                continue
            pool = self._resolve_pool(rule.pool, features)
            if pool is None:
                continue  # templated header absent/empty → try later rules
            return RuleMatch(pool=pool, rule_name=rule.name)
        return None

    @staticmethod
    def _when_matches(
        when: RuleWhen, pattern: re.Pattern[str] | None, f: RequestFeatures
    ) -> bool:
        if when.max_input_tokens is not None and f.input_tokens > when.max_input_tokens:
            return False
        if when.min_input_tokens is not None and f.input_tokens < when.min_input_tokens:
            return False
        if when.has_tools is not None and f.has_tools != when.has_tools:
            return False
        if when.header is not None and not f.headers.get(when.header.lower()):
            return False
        if pattern is not None and not pattern.search(f.system_prompt):
            return False
        return True

    @staticmethod
    def _resolve_pool(pool: str, f: RequestFeatures) -> str | None:
        m = _TEMPLATE.match(pool)
        if m is None:
            return pool  # literal pool name
        value = f.headers.get(m.group(1).lower())
        return value or None
```

- [ ] **Step 4: Run to verify pass** — `pytest tests/test_rules.py -v` → PASS.

- [ ] **Step 5: Commit**

```bash
git add app/routing/rules.py tests/test_rules.py
git commit -m "add stage-1 YAML rule engine"
```

---

## Task 4: Refactor PoolResolver to a pool-name → ordered-entries accessor

**Milestone:** M2 (seed for M3 fallback iteration).

**Files:**
- Modify: `app/routing/pools.py`
- Modify (test): `tests/test_pools.py` (rewrite)

**Interfaces:**
- Consumes: `PoolEntry` (config).
- Produces:
  - `app.routing.pools.PoolResolver(pools: dict[str, list[PoolEntry]], default_pool: str)`.
  - Property `default_pool -> str`.
  - `has_pool(name: str) -> bool`.
  - `entries(name: str) -> list[PoolEntry]` — the full ordered list (KeyError if unknown; callers guard with `has_pool`). This is the ordered-list seam M3 iterates.
  - `first_entry(name: str) -> PoolEntry` — `entries(name)[0]`.
- **Removed:** the M1 `resolve(requested_model)` method and the `Resolution` dataclass (now owned by `router.py`, Task 5). `Resolution` importers move to `app.routing.router`.

- [ ] **Step 1: Rewrite `tests/test_pools.py`**

```python
import pytest

from app.core.config import PoolEntry
from app.routing.pools import PoolResolver

POOLS = {
    "cheap": [PoolEntry(provider="deepseek", model="deepseek-chat"),
              PoolEntry(provider="anthropic", model="claude-haiku-4-5")],
    "default": [PoolEntry(provider="anthropic", model="claude-sonnet-5")],
}


def test_has_pool():
    r = PoolResolver(POOLS, "default")
    assert r.has_pool("cheap") is True
    assert r.has_pool("nope") is False
    assert r.default_pool == "default"


def test_entries_preserve_order():
    entries = PoolResolver(POOLS, "default").entries("cheap")
    assert [(e.provider, e.model) for e in entries] == [
        ("deepseek", "deepseek-chat"), ("anthropic", "claude-haiku-4-5")]


def test_first_entry():
    e = PoolResolver(POOLS, "default").first_entry("cheap")
    assert (e.provider, e.model) == ("deepseek", "deepseek-chat")
```

- [ ] **Step 2: Run to verify failure** — `pytest tests/test_pools.py -v` → FAIL (old interface).

- [ ] **Step 3: Replace `app/routing/pools.py`**

```python
from __future__ import annotations

from app.core.config import PoolEntry


class PoolResolver:
    """Maps a pool name to its ordered list of entries.

    M2 uses only the first entry; the full ordered list is exposed so M3's
    fallback chain can iterate it without changing this interface.
    """

    def __init__(self, pools: dict[str, list[PoolEntry]], default_pool: str) -> None:
        self._pools = pools
        self._default_pool = default_pool

    @property
    def default_pool(self) -> str:
        return self._default_pool

    def has_pool(self, name: str) -> bool:
        return name in self._pools

    def entries(self, name: str) -> list[PoolEntry]:
        return self._pools[name]

    def first_entry(self, name: str) -> PoolEntry:
        return self._pools[name][0]
```

- [ ] **Step 4: Run to verify pass** — `pytest tests/test_pools.py -v` → PASS. (Other suites still import the removed `Resolution`/`resolve` and will fail until Tasks 5–6; that is expected and resolved there.)

- [ ] **Step 5: Commit**

```bash
git add app/routing/pools.py tests/test_pools.py
git commit -m "refactor PoolResolver to ordered-entries accessor"
```

---

## Task 5: Router — compose features + rules + pools into a Resolution

**Milestone:** M2.

**Files:**
- Create: `app/routing/router.py`, `tests/test_router.py`

**Interfaces:**
- Consumes: `RuleEngine`/`RuleMatch` (Task 3), `PoolResolver` (Task 4), `extract_features` (Task 2).
- Produces:
  - `app.routing.router.Resolution` dataclass: `pool: str`, `provider: str`, `model: str`, `route_stage: str`, `route_reason: str`. (Moved here from `pools.py`; `gateway.py` imports it from here.)
  - `app.routing.router.Router(rule_engine: RuleEngine, pool_resolver: PoolResolver)`.
  - `route(body: dict, headers: dict[str, str]) -> Resolution` — extracts features, matches a rule, and resolves the pool's first entry. Behavior:
    - rule matches and its pool exists → `route_stage="rule"`, `route_reason=f"rule:{rule_name}"`, pool = matched pool.
    - rule matches but its (templated) pool does **not** exist → `route_stage="default"`, `route_reason=f"rule:{rule_name}:unknown-pool:{matched_pool}"`, pool = `default_pool`.
    - no rule matches → `route_stage="default"`, `route_reason="no-rule-matched"`, pool = `default_pool`.
  - Never raises for any JSON-object body/headers; never performs I/O.

- [ ] **Step 1: Write the failing test** `tests/test_router.py`:

```python
from app.core.config import PoolEntry, RuleConfig, RuleWhen
from app.routing.pools import PoolResolver
from app.routing.router import Resolution, Router
from app.routing.rules import RuleEngine

POOLS = {
    "cheap": [PoolEntry(provider="deepseek", model="deepseek-chat")],
    "default": [PoolEntry(provider="anthropic", model="claude-sonnet-5")],
    "large-context": [PoolEntry(provider="anthropic", model="claude-sonnet-5")],
}
RULES = [
    RuleConfig(name="explicit-hint", when=RuleWhen(header="x-pool"),
               pool="{{ header.x-pool }}"),
    RuleConfig(name="has-tools", when=RuleWhen(has_tools=True), pool="default"),
    RuleConfig(name="long-context", when=RuleWhen(min_input_tokens=20000),
               pool="large-context"),
    RuleConfig(name="short-and-simple",
               when=RuleWhen(max_input_tokens=500, has_tools=False), pool="cheap"),
]


def _router():
    return Router(RuleEngine(RULES), PoolResolver(POOLS, "default"))


def test_short_request_routes_to_cheap_via_rule():
    r = _router().route({"messages": [{"role": "user", "content": "hi"}]}, {})
    assert r == Resolution(pool="cheap", provider="deepseek", model="deepseek-chat",
                           route_stage="rule", route_reason="rule:short-and-simple")


def test_header_hint_wins():
    r = _router().route({"messages": [{"role": "user", "content": "hi"}]},
                        {"x-pool": "cheap"})
    assert r.pool == "cheap" and r.route_stage == "rule"
    assert r.route_reason == "rule:explicit-hint"


def test_unknown_hint_degrades_to_default():
    r = _router().route({"messages": [{"role": "user", "content": "hi"}]},
                        {"x-pool": "bogus"})
    assert r.pool == "default" and r.provider == "anthropic"
    assert r.route_stage == "default"
    assert "unknown-pool" in r.route_reason and "bogus" in r.route_reason


def test_no_match_routes_to_default():
    long_text = "x" * 40000       # ~10000 tokens: above 500, below 20000 → no rule
    r = _router().route({"messages": [{"role": "user", "content": long_text}]}, {})
    assert r.pool == "default"
    assert r.route_stage == "default" and r.route_reason == "no-rule-matched"


def test_empty_rules_all_default():
    r = Router(RuleEngine([]), PoolResolver(POOLS, "default")).route(
        {"messages": []}, {})
    assert r.pool == "default" and r.route_reason == "no-rule-matched"
```

- [ ] **Step 2: Run to verify failure** — `pytest tests/test_router.py -v` → FAIL (module missing).

- [ ] **Step 3: Write `app/routing/router.py`**

```python
from __future__ import annotations

from dataclasses import dataclass

from app.routing.features import extract_features
from app.routing.pools import PoolResolver
from app.routing.rules import RuleEngine


@dataclass
class Resolution:
    pool: str
    provider: str
    model: str
    route_stage: str
    route_reason: str


class Router:
    def __init__(self, rule_engine: RuleEngine, pool_resolver: PoolResolver) -> None:
        self._rules = rule_engine
        self._pools = pool_resolver

    def route(self, body: dict, headers: dict[str, str]) -> Resolution:
        features = extract_features(body, headers)
        match = self._rules.match(features)

        if match is not None and self._pools.has_pool(match.pool):
            pool, stage, reason = match.pool, "rule", f"rule:{match.rule_name}"
        elif match is not None:
            pool, stage = self._pools.default_pool, "default"
            reason = f"rule:{match.rule_name}:unknown-pool:{match.pool}"
        else:
            pool, stage, reason = self._pools.default_pool, "default", "no-rule-matched"

        entry = self._pools.first_entry(pool)
        return Resolution(
            pool=pool, provider=entry.provider, model=entry.model,
            route_stage=stage, route_reason=reason,
        )
```

- [ ] **Step 4: Run to verify pass** — `pytest tests/test_router.py -v` → PASS.

- [ ] **Step 5: Commit**

```bash
git add app/routing/router.py tests/test_router.py
git commit -m "add router composing rules, features, and pools"
```

---

## Task 6: Wire Router into GatewayService + pass headers from the endpoint

**Milestone:** M2.

**Files:**
- Modify: `app/routing/gateway.py`, `app/api/v1/endpoints/chat.py`
- Modify (test): `tests/test_gateway.py` (update construction + `complete` signature)

**Interfaces:**
- Consumes: `Router`/`Resolution` (Task 5), `CostTracker`, `RequestsRepository`, `ProviderAdapter`/`ProviderError`, `BadRequestError`, `log_request` (all existing).
- Produces:
  - `app.routing.gateway.GatewayService(router: Router, registry: dict[str, ProviderAdapter], cost: CostTracker, repo: RequestsRepository)`.
  - `complete(request_id: str, body: dict, headers: dict[str, str]) -> dict` — raises `BadRequestError(param="model")` if `body` has no `model`; otherwise `resolution = router.route(body, headers)`, calls the resolved provider adapter, records the row + log line (with `resolution.route_stage`/`route_reason`), returns the upstream body. `ProviderError` still records a row (status=error, tokens=0, cost=0) and re-raises. `_record` is unchanged except it now receives a `Resolution` carrying real `route_stage`/`route_reason`.

- [ ] **Step 1: Update `tests/test_gateway.py`** — replace the `POOLS`/`_service` setup and construction so the service is built with a `Router`; keep the three behavior tests. New top of file:

```python
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
```

- [ ] **Step 2: Run to verify failure** — `pytest tests/test_gateway.py -v` → FAIL (`GatewayService` still takes a resolver; `complete` takes 2 args).

- [ ] **Step 3: Edit `app/routing/gateway.py`** — change imports and constructor and `complete`. Full file:

```python
from __future__ import annotations

import time

from app.core.errors import BadRequestError
from app.core.logging import log_request
from app.cost.tracker import CostTracker
from app.providers.base import ProviderAdapter, ProviderError
from app.repositories.requests_repo import RequestRecord, RequestsRepository
from app.routing.router import Resolution, Router


class GatewayService:
    def __init__(
        self, router: Router, registry: dict[str, ProviderAdapter],
        cost: CostTracker, repo: RequestsRepository,
    ) -> None:
        self._router = router
        self._registry = registry
        self._cost = cost
        self._repo = repo

    async def complete(self, request_id: str, body: dict, headers: dict[str, str]) -> dict:
        requested = body.get("model")
        if not requested:
            raise BadRequestError("Missing required field 'model'", param="model")

        resolution = self._router.route(body, headers)
        adapter = self._registry[resolution.provider]
        started = time.monotonic()
        try:
            resp = await adapter.chat_completion(body, resolution.model)
        except ProviderError as exc:
            await self._record(request_id, resolution, exc.status_code, 0, 0,
                               int((time.monotonic() - started) * 1000))
            raise
        latency_ms = int((time.monotonic() - started) * 1000)
        await self._record(request_id, resolution, resp.status_code,
                           resp.input_tokens, resp.output_tokens, latency_ms)
        return resp.body

    async def _record(
        self, request_id: str, resolution: Resolution, status: int,
        input_tokens: int, output_tokens: int, latency_ms: int,
    ) -> None:
        cost = self._cost.compute(resolution.model, input_tokens, output_tokens)
        record = RequestRecord(
            id=request_id, ts=int(time.time()), pool=resolution.pool,
            provider=resolution.provider, model=resolution.model,
            route_stage=resolution.route_stage, route_reason=resolution.route_reason,
            input_tokens=input_tokens, output_tokens=output_tokens, cost_usd=cost,
            latency_ms=latency_ms, status=status, fallback_from=None,
        )
        await self._repo.insert(record)
        log_request({
            "request_id": request_id, "pool": resolution.pool,
            "model": resolution.model, "route_stage": resolution.route_stage,
            "input_tokens": input_tokens, "output_tokens": output_tokens,
            "cost_usd": cost, "latency_ms": latency_ms, "status": status,
            "fallback_hops": 0,
        })
```

- [ ] **Step 4: Edit `app/api/v1/endpoints/chat.py`** — pass headers. Change the `complete` call:

```python
    request_id = uuid4().hex
    result = await gateway.complete(request_id, body, dict(request.headers))
    return JSONResponse(content=result, status_code=200)
```

(No other change; `dict(request.headers)` yields lowercased header names, which `extract_features` re-normalizes.)

- [ ] **Step 5: Run to verify pass** — `pytest tests/test_gateway.py -v` → PASS.

- [ ] **Step 6: Commit**

```bash
git add app/routing/gateway.py app/api/v1/endpoints/chat.py tests/test_gateway.py
git commit -m "route requests via rule engine and record real route stage/reason"
```

---

## Task 7: Lifespan wiring + conftest + endpoint/integration tests (full suite green)

**Milestone:** M2.

**Files:**
- Modify: `app/main.py`
- Modify (test): `tests/conftest.py`, `tests/test_chat_endpoint.py`, `tests/test_app_integration.py`

**Interfaces:**
- Consumes: `RuleEngine` (Task 3), `PoolResolver` (Task 4), `Router` (Task 5), `GatewayService` (Task 6).
- Produces: `create_app` builds the routing stack in lifespan and injects `Router` into `GatewayService`. No public signature change to `create_app`.

- [ ] **Step 1: Edit `app/main.py`** — update imports and the lifespan body. Add imports:

```python
from app.routing.rules import RuleEngine
from app.routing.router import Router
```

Replace the three lines that built `resolver`/`gateway` inside `lifespan`:

```python
        registry = build_registry(config, http_client)
        rule_engine = RuleEngine(config.rules)
        pool_resolver = PoolResolver(config.pools, config.default_pool)
        router = Router(rule_engine, pool_resolver)
        cost = CostTracker(config.prices)
        repo = RequestsRepository(db.connection)
        app.state.config = config
        app.state.db = db
        app.state.http_client = http_client
        app.state.gateway = GatewayService(router, registry, cost, repo)
```

(`PoolResolver` is already imported in `app/main.py` from M1; keep that import.)

- [ ] **Step 2: Edit `tests/conftest.py`** — give `test_config` M2 rules + prices so endpoint/integration tests exercise routing. Update the `AppConfig(...)` in the `test_config` fixture:

```python
    from app.core.config import RuleConfig, RuleWhen  # local import for clarity
    return AppConfig(
        api_key=VALID_KEY,
        db_path=str(tmp_path / "g.db"),
        default_pool="default",
        providers={
            "deepseek": ProviderConfig(base_url="https://api.deepseek.com/v1", api_key="sk-ds"),
            "anthropic": ProviderConfig(base_url="https://api.anthropic.com/v1", api_key="sk-an"),
        },
        pools={
            "cheap": [PoolEntry(provider="deepseek", model="deepseek-chat")],
            "default": [PoolEntry(provider="anthropic", model="claude-sonnet-5")],
            "large-context": [PoolEntry(provider="anthropic", model="claude-sonnet-5")],
        },
        prices={
            "deepseek-chat": PriceEntry(input_per_1m=0.27, output_per_1m=1.10),
            "claude-sonnet-5": PriceEntry(input_per_1m=3.00, output_per_1m=15.00),
        },
        rules=[
            RuleConfig(name="explicit-hint", when=RuleWhen(header="x-pool"),
                       pool="{{ header.x-pool }}"),
            RuleConfig(name="has-tools", when=RuleWhen(has_tools=True), pool="default"),
            RuleConfig(name="long-context", when=RuleWhen(min_input_tokens=20000),
                       pool="large-context"),
            RuleConfig(name="short-and-simple",
                       when=RuleWhen(max_input_tokens=500, has_tools=False), pool="cheap"),
        ],
    )
```

- [ ] **Step 3: Update `tests/test_chat_endpoint.py`** — under M2 routing, a short tool-less request routes to `cheap` (deepseek) by rule, not by the `model` field. Replace the two body-dependent tests and add cost + routing assertions:

```python
import json

import httpx
import respx


async def test_requires_auth(client):
    resp = await client.post("/v1/chat/completions", json={"model": "x"})
    assert resp.status_code == 401


async def test_missing_model_is_400(client, auth_headers):
    resp = await client.post("/v1/chat/completions", json={"messages": []},
                             headers=auth_headers)
    assert resp.status_code == 400
    body = resp.json()
    assert body["error"]["type"] == "invalid_request_error"
    assert body["error"]["param"] == "model"


@respx.mock
async def test_short_request_routed_to_cheap_and_model_rewritten(client, auth_headers):
    route = respx.post("https://api.deepseek.com/v1/chat/completions").mock(
        return_value=httpx.Response(
            200, json={"id": "resp-1", "choices": [{"message": {"content": "hi"}}],
                       "usage": {"prompt_tokens": 7, "completion_tokens": 3}})
    )
    resp = await client.post(
        "/v1/chat/completions", headers=auth_headers,
        json={"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}],
              "temperature": 0.2, "future_field": True},
    )
    assert resp.status_code == 200
    assert resp.json()["id"] == "resp-1"                 # body unchanged
    sent = json.loads(route.calls.last.request.content)
    assert sent["model"] == "deepseek-chat"              # rewritten to resolved model
    assert sent["temperature"] == 0.2                    # preserved
    assert sent["future_field"] is True                  # unknown forwarded


@respx.mock
async def test_header_hint_overrides_routing(client, auth_headers):
    respx.post("https://api.anthropic.com/v1/chat/completions").mock(
        return_value=httpx.Response(200, json={"id": "z"}))
    resp = await client.post(
        "/v1/chat/completions",
        headers={**auth_headers, "x-pool": "default"},
        json={"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 200                        # routed to default (anthropic)


@respx.mock
async def test_writes_row_with_route_and_cost(client, auth_headers, app):
    respx.post("https://api.deepseek.com/v1/chat/completions").mock(
        return_value=httpx.Response(
            200, json={"id": "r", "usage": {"prompt_tokens": 1_000_000,
                                            "completion_tokens": 1_000_000}}))
    await client.post("/v1/chat/completions", headers=auth_headers,
                      json={"model": "x", "messages": [{"role": "user", "content": "hi"}]})
    cur = await app.state.db.connection.execute(
        "SELECT pool, provider, model, route_stage, route_reason, cost_usd, status "
        "FROM requests")
    rows = await cur.fetchall()
    assert len(rows) == 1
    pool, provider, model, stage, reason, cost, status = rows[0]
    assert (pool, provider, model, stage) == ("cheap", "deepseek", "deepseek-chat", "rule")
    assert reason == "rule:short-and-simple"
    assert round(cost, 6) == round(0.27 + 1.10, 6)       # cost from provider usage
    assert status == 200


@respx.mock
async def test_upstream_500_returns_openai_error(client, auth_headers):
    respx.post("https://api.deepseek.com/v1/chat/completions").mock(
        return_value=httpx.Response(500, json={"error": {"message": "down"}}))
    resp = await client.post(
        "/v1/chat/completions", headers=auth_headers,
        json={"model": "x", "messages": [{"role": "user", "content": "hi"}]})
    assert resp.status_code == 500
    assert "error" in resp.json()
```

- [ ] **Step 4: Update `tests/test_app_integration.py`** — assert rule routing + default fallthrough + cost end to end:

```python
import httpx
import respx


async def test_end_to_end_rule_routing(client, auth_headers, app):
    models = await client.get("/v1/models", headers=auth_headers)
    assert {m["id"] for m in models.json()["data"]} == {"cheap", "default", "large-context"}

    # A medium request (no tools, no hint, > 500 and < 20000 tokens) → default pool.
    medium = "x" * 4000  # ~1000 tokens
    with respx.mock:
        respx.post("https://api.anthropic.com/v1/chat/completions").mock(
            return_value=httpx.Response(
                200, json={"id": "z", "usage": {"prompt_tokens": 1, "completion_tokens": 1}}))
        resp = await client.post(
            "/v1/chat/completions", headers=auth_headers,
            json={"model": "gpt-4o", "messages": [{"role": "user", "content": medium}]})
    assert resp.status_code == 200

    cur = await app.state.db.connection.execute(
        "SELECT pool, provider, route_stage, route_reason FROM requests")
    row = await cur.fetchone()
    assert row[0] == "default" and row[1] == "anthropic"
    assert row[2] == "default" and row[3] == "no-rule-matched"
```

- [ ] **Step 5: Run the full suite** — `pytest -v` → all PASS.

- [ ] **Step 6: Commit**

```bash
git add app/main.py tests/conftest.py tests/test_chat_endpoint.py tests/test_app_integration.py
git commit -m "wire rule-based router into app and update endpoint/integration tests"
```

---

## Self-Review (completed by planner)

- **Spec coverage:** (1) stage-1 rule engine with all five `when` conditions (`max/min_input_tokens`, `has_tools`, header hint + templated pool, `system_regex`) → Tasks 1–3; (2) router chooses pool via rules, ordered-pool shape preserved for M3 → Tasks 4–5; (3) populated cost table computing real `cost_usd` from provider `usage` at write time → Task 1 (prices) + existing `CostTracker`, exercised in Task 7; (4) `route_stage`/`route_reason` capture (`rule` / `default`) persisted and logged → Tasks 5–6. Scope guard: no classifier, fallback, cooldown, or dashboard built.
- **Type consistency:** `RequestFeatures`, `RuleMatch`, `Resolution`, `Router.route(body, headers)`, `PoolResolver.{has_pool,entries,first_entry,default_pool}`, `GatewayService(router, registry, cost, repo)`, and `complete(request_id, body, headers)` are used identically across producing and consuming tasks. `Resolution` moved from `pools.py` to `router.py`; all importers (`gateway.py`, tests) updated.
- **Placeholder scan:** every code step contains concrete code; no TBD/TODO.
- **Invariant checks:** routing performs no I/O and never raises for a JSON-object body (unknown hint degrades to `default_pool`); rules evaluated in order, first match wins; cost uses provider `usage` not the routing estimate, computed at write time, historical rows untouched; unknown fields still forwarded, response body unchanged, model rewritten; bearer auth unchanged.
- **Forward-compat check:** `PoolResolver.entries()` exposes the full ordered list for M3's fallback iteration; `route_stage` remains free-text so M4 can add `classifier` and M3 can add `fallback`; the `Router`/`RuleEngine`/`extract_features` seam lets M4 slot the classifier in after `RuleEngine.match` returns `None` without touching stage-1.
