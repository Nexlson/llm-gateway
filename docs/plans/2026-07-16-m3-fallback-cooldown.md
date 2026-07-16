# M3 — Fallback Chain + 60s Cooldown Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Run-id:** 20260716-m3-fallback

**Goal:** Make each pool's ordered list resilient: on a *retryable* upstream failure (5xx, timeout, connection error, 429) advance to the next `(provider, model)` entry in the chosen pool; on a *non-retryable* failure (400/401/403 and other 4xx) return immediately in OpenAI error shape without burning the chain; mark a failing entry unhealthy and skip it for a config-driven cooldown (default 60s, one free attempt afterwards); and record the fallback on the `requests` row (`fallback_from`, `route_stage`/`route_reason`) and in the structured log (`fallback_hops`).

**Architecture:** Insert a small, single-purpose chain executor between routing and provider calls. `HealthTracker` is a pure, clock-injectable cooldown store keyed by `(provider, model)` — no I/O, no `sleep`, deterministic under a fake clock. `FallbackExecutor` iterates `PoolResolver.entries(pool)` in order, skips entries currently in cooldown, calls each entry's adapter, marks retryable failures unhealthy and advances, returns immediately on non-retryable, and reports an `ExecutionOutcome` (which entry served the request, whether a hop occurred, how many hops, and the first-tried entry). `GatewayService` keeps orchestration (persist + log + return/raise) but now records the **actually-used** entry and the fallback metadata. Layering is unchanged: handler → routing services → repository; the executor never imports from `api/`.

**Tech Stack:** Python 3.11+, FastAPI, Pydantic v2 (all already present). No new dependencies. Tests: pytest, pytest-asyncio, respx, httpx.AsyncClient (existing setup); cooldown time is driven by an injected clock, never `time.sleep`.

## Global Constraints

- **Retryable vs non-retryable is already defined.** `app/providers/base.py` exposes `is_retryable(status_code)` (True for `{408, 409, 429}` and any `>= 500`) and every `ProviderError` already carries a `retryable: bool` flag (timeouts → 504 retryable, connection errors → 502 retryable, upstream 5xx/429 → retryable, 400/401/403 → non-retryable). **Do not reclassify errors** — consult `ProviderError.retryable`. The executor never inspects raw status codes to decide retryability.
- **One row per client request.** Regardless of how many entries are tried, exactly one `requests` row is inserted and exactly one JSON log line is emitted per client request.
- **Cost at write time, on the served model.** `cost_usd` is computed from the **provider-reported `usage`** of the entry that actually served the request, against the static price table, and stored on the row. Never recompute historical rows. When the chain is exhausted (no success), tokens/cost are `0`.
- **OpenAI compatibility must not regress.** Request/response bodies pass through unchanged except the `model` field (rewritten per entry). Unknown fields forwarded. All errors — including an exhausted chain — return OpenAI's error envelope. Constant-time bearer auth on `/v1/*` and `/dashboard` unchanged.
- **Cooldown is config-driven, not hardcoded.** A single `cooldown_seconds` value (default `60.0`) sourced from `config.yaml`. No half-open state machine, no rolling error windows, no latency tracking — a plain monotonic-clock cooldown.
- **Routing decision precedence unchanged.** M2 decides the *pool* (`rule` / `default`, later `classifier`); M3 iterates *within* the chosen pool. The executor never re-runs the rule engine and never changes which pool was selected.
- **Scope guard (do NOT build in M3):** classifier / stage 2 (M4), dashboard UI (M5), Docker/VPS deploy (M6), and everything in the architecture "Deferred" table (multi-key auth, Prometheus/OTel, latency-aware routing, streaming/SSE, live config reload, semantic caching, and any half-open / rolling-window health machine).
- **Tests never hit real provider APIs.** Mock outbound HTTP with respx or use in-test `FakeAdapter`s. Temp-file SQLite (not `:memory:`) so WAL applies. Cooldown timing is exercised with an injected clock — never `time.sleep`.

---

## M3 Design Decisions (documented assumptions)

The owner authorized autonomous progress (see `.claude/CLAUDE.md` → "Autonomous milestone execution"). Each item below is a chosen, documented default; none is blocking. The one genuinely owner-facing question is flagged at the end.

1. **`route_stage="fallback"` when (and only when) a hop occurred; the routing origin is preserved in `route_reason`.** The schema comment (`architecture.md`) and the domain invariant both enumerate `route_stage ∈ {rule, classifier, fallback}`, so a fallback hop must be visible in `route_stage`. To avoid losing *why* the pool was chosen, the router's original reason is prefixed into `route_reason`. Concretely:
   - **No hop** (the first attempted entry served the request, or the request failed on the first attempted entry): `route_stage` and `route_reason` are exactly what `Router.route()` produced (`rule` / `default`), `fallback_from` is `NULL`, log `fallback_hops = 0`.
   - **Hop occurred** (success or exhaustion after ≥1 retryable failure): `route_stage = "fallback"`, `route_reason = f"{original_reason}|fallback:{first_id}->{final_id}|hops={n}"` (e.g. `rule:short-and-simple|fallback:deepseek/deepseek-chat->anthropic/claude-haiku-4-5|hops=1`), `fallback_from = first_id` (the first *tried* entry, format `provider/model`), log `fallback_hops = n`.
   This satisfies the invariant literally, keeps the routing origin, and lets the M5 dashboard find fallback events by either `route_stage='fallback'` or `fallback_from IS NOT NULL`. *(Alternative considered and rejected: keep `route_stage` = routing origin and rely only on `fallback_from`. Rejected because the invariant explicitly lists `fallback` as a `route_stage` value.)*

2. **The row records the entry that actually served the request, not the pool's nominal first entry.** On a successful fallback, `provider`/`model`/`cost_usd` reflect the entry that returned the response (so cost is correct for the model that ran). In the no-fallback case this equals the router's first entry, so M1/M2 rows are unchanged.

3. **"First-tried" is per-request, and cooldown-skipping is not itself a hop.** `fallback_from` and `fallback_hops` count advances *caused by failures within this request*. If a request starts at a later entry because the primary is already in cooldown (skipped without being tried this request), that is `hops = 0`, `fallback_from = NULL` — but the served `provider`/`model` still reflect the entry used (decision #2), so cooldown-based rerouting remains visible in the row.

4. **Non-retryable errors never trip the cooldown.** A 400/401/403 (or other non-retryable) is the caller's or the deployment's problem, not a transient model-health signal, so the executor returns immediately **without** calling `mark_unhealthy`. Only retryable failures mark an entry unhealthy.

5. **Whole-pool-in-cooldown degrades to best-effort, not to a no-attempt failure.** If *every* entry in the chosen pool is currently in cooldown, the executor still attempts the full chain in order (treating cooldown as advisory when nothing healthy remains) rather than returning an error without any upstream call. This preserves architecture success-criterion #4 ("a provider outage degrades to fallback instead of taking the app down") and matches the spirit of "one free attempt". Failures during a best-effort pass re-arm the cooldown as usual. *(This is the architecture's open question "behavior when the chosen pool is entirely in cooldown"; the default here is **best-effort attempt**. See Open Questions.)*

6. **`HealthTracker` stays pure (no logging, no I/O).** Cooldown-trip visibility for M3 is carried by the per-request log line's `fallback_hops`/`fallback_from` fields (the logging invariant is "one line per request"). A dedicated cooldown-trip log/metric is deferred to M5 (dashboard) when there is a consumer for it; not built now.

7. **Cooldown boundary is inclusive of recovery.** An entry marked unhealthy at monotonic time `t` is unhealthy for `now < t + cooldown_seconds` and healthy again at `now >= t + cooldown_seconds` (the "one free attempt"). Comparisons use `time.monotonic` by default, injectable for tests.

---

## File Structure

**Create (application):**
- `app/routing/health.py` — `HealthTracker` (pure, clock-injectable cooldown store keyed by `(provider, model)`).
- `app/routing/executor.py` — `ExecutionOutcome` dataclass + `FallbackExecutor` (iterates pool entries, consults `HealthTracker`, advances on retryable, returns on non-retryable, reports the outcome).

**Modify (application):**
- `app/core/config.py` — add `AppConfig.cooldown_seconds: float = 60.0`.
- `app/routing/gateway.py` — `GatewayService(router, executor, cost, repo)`; `complete` delegates the call to the executor and records the served entry + fallback metadata; single row/log per request.
- `app/main.py` — build `HealthTracker` and `FallbackExecutor` in the lifespan; inject the executor into `GatewayService`.
- `config.example.yaml` — add `cooldown_seconds: 60` (the `cheap` pool already has two entries, so the example exercises fallback).

**Create (tests):**
- `tests/test_health.py` — `HealthTracker` unit tests (injected clock, no sleeping).
- `tests/test_executor.py` — `FallbackExecutor` unit tests with `FakeAdapter`s + injected clock.
- `tests/test_fallback_endpoint.py` — end-to-end fallback/cooldown via the chat endpoint with respx and a multi-entry pool.
- `tests/test_acceptance_m3.py` — independent acceptance tests derived from the criteria below (not from the implementation).

**Modify (tests):**
- `tests/test_gateway.py` — update `GatewayService` construction to the executor-based constructor; add gateway-level fallback/exhaustion/non-retryable row tests. (The three existing behavior tests remain and must still pass unchanged in observable outcome.)
- `tests/test_config.py` — assert `cooldown_seconds` default and example value.

**Ripple check (should pass unchanged, verify in Task 5):** `tests/test_chat_endpoint.py`, `tests/test_app_integration.py`, `tests/test_acceptance_m1.py`, `tests/test_acceptance_m2.py`, `tests/test_pools.py`, `tests/test_router.py`, `tests/test_rules.py`, `tests/test_features.py`, `tests/test_cost.py`, `tests/test_providers.py`, `tests/test_models_endpoint.py`, `tests/test_security.py`, `tests/test_dashboard.py`, `tests/test_database.py`, `tests/test_errors.py`, `tests/test_logging.py`. Only `test_gateway.py` and `test_config.py` change; `app/main.py` is the only wiring change. All M1/M2 fixtures use healthy, single-primary-entry providers, so no fallback path is triggered and their assertions are unaffected.

---

## Acceptance Criteria (for the tester — derivable without reading implementation)

Unless stated otherwise, assume a config with a **two-entry** `cheap` pool whose first entry is `deepseek/deepseek-chat` (upstream `https://api.deepseek.com/v1/chat/completions`) and second entry is `anthropic/claude-haiku-4-5` (upstream `https://api.anthropic.com/v1/chat/completions`); a single-entry `default` pool `anthropic/claude-sonnet-5`; `default_pool: default`; a rule `short-and-simple` (`max_input_tokens: 500, has_tools: false → cheap`) so a short, tool-less request routes to `cheap`; populated prices for `deepseek-chat` and `claude-haiku-4-5`; and `cooldown_seconds: 60`. "OpenAI error shape" means `body["error"]` is an object with a string `message`.

**Fallback chain**

- **M3-AC-F1 Advance on retryable 5xx.** A short request routed to `cheap` where the first entry (`deepseek`) returns **503** and the second (`anthropic`) returns **200** yields a **200** to the client carrying the **second** entry's response body. Exactly one `requests` row: `provider="anthropic"`, `model="claude-haiku-4-5"`, `route_stage="fallback"`, `fallback_from="deepseek/deepseek-chat"`, `status=200`. The stdout JSON log line for that request has `fallback_hops == 1`.
- **M3-AC-F2 Exhaustion returns the last error in OpenAI shape.** Both `cheap` entries return retryable errors (e.g. `503` then `429`). The client receives an error in OpenAI error shape with the **last** upstream status (`429`). Exactly one row: `status=429`, `input_tokens=0`, `output_tokens=0`, `cost_usd=0.0`, `route_stage="fallback"`, `fallback_from="deepseek/deepseek-chat"`. Log `fallback_hops == 1`.
- **M3-AC-F3 Non-retryable returns immediately, chain not burned.** The first `cheap` entry returns **400**. The client receives **400** in OpenAI error shape. The **second entry is never called** (assert the anthropic route was not requested). The row has `route_stage="rule"` (routing origin, not `fallback`), `fallback_from` is `NULL`, `provider="deepseek"`, `status=400`. Log `fallback_hops == 0`.
- **M3-AC-F4 Auth errors are non-retryable.** With the first entry returning **401** (and separately **403**), behavior is identical to M3-AC-F3: immediate return of that status, second entry not called, no fallback recorded.
- **M3-AC-F5 Timeouts and connection errors are retryable.** When the first entry raises a request timeout (or a connection error) and the second returns **200**, the client gets **200** from the second entry; the row shows a fallback (`route_stage="fallback"`, `fallback_from="deepseek/deepseek-chat"`, `provider="anthropic"`), log `fallback_hops == 1`.
- **M3-AC-F6 Rate limit (429) is retryable.** First entry `429`, second `200` → client gets the second entry's `200`; row shows the fallback.

**Cooldown health**

- **M3-AC-H1 A failed entry is skipped within the cooldown window.** After a request in which the first entry failed retryably (marking it unhealthy), a **subsequent** request (same running app, clock not advanced past cooldown) routed to the same pool **does not call** the first entry — it is served by the next entry. The second request's row shows `fallback_from == NULL` and `fallback_hops == 0` (it started at the healthy entry; no within-request hop), with `provider` = the next entry.
- **M3-AC-H2 One free attempt after the cooldown elapses.** Once `cooldown_seconds` has elapsed (driven by a deterministic injected clock, never real sleep), the previously-unhealthy entry is attempted again on the next request (the "one free attempt"). If it fails again it is re-marked unhealthy for another full window; if it succeeds it serves the request.
- **M3-AC-H3 `HealthTracker` is pure and deterministic.** With an injected clock: an unknown `(provider, model)` is healthy; after `mark_unhealthy` it is unhealthy for every clock value `< mark_time + cooldown_seconds`; it becomes healthy again at exactly `mark_time + cooldown_seconds` and beyond. No method sleeps or performs I/O. Identical clock sequences produce identical results.
- **M3-AC-H4 Non-retryable failures do not trip the cooldown.** An entry that returns a non-retryable error (e.g. 400) is **not** marked unhealthy: a later request still attempts that same entry.
- **M3-AC-H5 Whole-pool-cooling degrades to best-effort.** If every entry in the chosen pool is currently in cooldown, the request is still attempted against the chain (in order) rather than failing without any upstream call; a healthy response is returned if any entry now succeeds.

**Cost, storage, logging (no regression)**

- **M3-AC-C1 Cost is computed on the served model.** In a successful fallback (M3-AC-F1) with `prices["claude-haiku-4-5"]` set and the second entry reporting `usage`, `cost_usd` equals that model's price applied to the reported usage — not the first entry's price. `input_tokens`/`output_tokens` come from the served entry's `usage`.
- **M3-AC-C2 Exactly one row and one log line per client request**, regardless of the number of entries attempted.
- **M3-AC-X1 No-fallback path is byte-identical to M2.** A healthy first entry produces `route_stage` = the routing stage (`rule`/`default`), `fallback_from = NULL`, log `fallback_hops = 0`, and the served `provider`/`model` = the pool's first entry. All existing M1/M2 acceptance tests pass unchanged.
- **M3-AC-CFG Cooldown is config-driven.** `load_config` yields `cooldown_seconds == 60.0` when unspecified (default) and the value from YAML when present; `config.example.yaml` ships `cooldown_seconds: 60`.

---

## Task 1: Add `cooldown_seconds` to config + example

**Milestone:** M3.

**Files:**
- Modify: `app/core/config.py`, `config.example.yaml`
- Modify (test): `tests/test_config.py`

**Interfaces:**
- Consumes: existing `AppConfig`, `load_config`.
- Produces: `AppConfig.cooldown_seconds: float = 60.0` (new field; default 60s when omitted from YAML).

- [ ] **Step 1: Append failing tests to `tests/test_config.py`**

```python
def test_cooldown_seconds_defaults_to_60(tmp_path, monkeypatch):
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
    assert cfg.cooldown_seconds == 60.0


def test_cooldown_seconds_from_yaml(tmp_path, monkeypatch):
    monkeypatch.setenv("K", "x")
    ok = tmp_path / "ok.yaml"
    ok.write_text(
        "api_key_env: K\n"
        "cooldown_seconds: 15\n"
        "default_pool: default\n"
        "db_path: g.db\n"
        "providers:\n  p:\n    base_url: http://x\n    api_key_env: K\n"
        "pools:\n  default:\n    - {provider: p, model: m}\n"
    )
    cfg = load_config(ok)
    assert cfg.cooldown_seconds == 15.0


def test_example_config_ships_cooldown(monkeypatch):
    _set_env(monkeypatch, EXAMPLE_ENV)
    cfg = load_config(EXAMPLE)
    assert cfg.cooldown_seconds == 60.0
```

- [ ] **Step 2: Run to verify failure** — `pytest tests/test_config.py -k cooldown -v` → FAIL (`AppConfig` has no `cooldown_seconds`; example lacks it).

- [ ] **Step 3: Edit `app/core/config.py`** — add the field to `AppConfig` (after `db_path`):

```python
    db_path: str = "gateway.db"
    cooldown_seconds: float = 60.0
```

- [ ] **Step 4: Edit `config.example.yaml`** — add the cooldown line under the top-level scalars (after `db_path`):

```yaml
db_path: "gateway.db"
cooldown_seconds: 60          # seconds an unhealthy pool entry is skipped before one free retry
default_pool: "default"
```

- [ ] **Step 5: Run to verify pass** — `pytest tests/test_config.py -v` → PASS.

- [ ] **Step 6: Commit**

```bash
git add app/core/config.py config.example.yaml tests/test_config.py
git commit -m "add cooldown_seconds config value with 60s default"
```

---

## Task 2: HealthTracker — pure, clock-injectable cooldown store

**Milestone:** M3.

**Files:**
- Create: `app/routing/health.py`, `tests/test_health.py`

**Interfaces:**
- Consumes: nothing (pure). Optional injected `clock: Callable[[], float]` (default `time.monotonic`).
- Produces:
  - `app.routing.health.HealthTracker(cooldown_seconds: float, clock: Callable[[], float] = time.monotonic)`.
  - `is_healthy(provider: str, model: str) -> bool` — `True` for an unknown or recovered entry; `False` while `clock() < marked_at + cooldown_seconds`.
  - `mark_unhealthy(provider: str, model: str) -> None` — sets the entry's cooldown to `clock() + cooldown_seconds` (overwrites any prior window).
  - No logging, no I/O, no sleeping.

- [ ] **Step 1: Write the failing test** `tests/test_health.py`:

```python
from app.routing.health import HealthTracker


class FakeClock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def now(self) -> float:
        return self.t


def test_unknown_entry_is_healthy():
    h = HealthTracker(cooldown_seconds=60, clock=FakeClock().now)
    assert h.is_healthy("deepseek", "deepseek-chat") is True


def test_marked_entry_is_unhealthy_within_window():
    clock = FakeClock(1000.0)
    h = HealthTracker(cooldown_seconds=60, clock=clock.now)
    h.mark_unhealthy("deepseek", "deepseek-chat")
    assert h.is_healthy("deepseek", "deepseek-chat") is False
    clock.t = 1000.0 + 59.0
    assert h.is_healthy("deepseek", "deepseek-chat") is False


def test_entry_recovers_exactly_at_cooldown_boundary():
    clock = FakeClock(1000.0)
    h = HealthTracker(cooldown_seconds=60, clock=clock.now)
    h.mark_unhealthy("deepseek", "deepseek-chat")
    clock.t = 1000.0 + 60.0            # boundary: healthy again (the free attempt)
    assert h.is_healthy("deepseek", "deepseek-chat") is True
    clock.t = 1000.0 + 61.0
    assert h.is_healthy("deepseek", "deepseek-chat") is True


def test_remark_extends_window():
    clock = FakeClock(1000.0)
    h = HealthTracker(cooldown_seconds=60, clock=clock.now)
    h.mark_unhealthy("deepseek", "deepseek-chat")
    clock.t = 1060.0                    # would have recovered
    h.mark_unhealthy("deepseek", "deepseek-chat")   # fails its free attempt → new window
    assert h.is_healthy("deepseek", "deepseek-chat") is False
    clock.t = 1119.0
    assert h.is_healthy("deepseek", "deepseek-chat") is False
    clock.t = 1120.0
    assert h.is_healthy("deepseek", "deepseek-chat") is True


def test_entries_are_independent():
    clock = FakeClock(1000.0)
    h = HealthTracker(cooldown_seconds=60, clock=clock.now)
    h.mark_unhealthy("deepseek", "deepseek-chat")
    assert h.is_healthy("anthropic", "claude-haiku-4-5") is True
    assert h.is_healthy("deepseek", "deepseek-chat") is False
```

- [ ] **Step 2: Run to verify failure** — `pytest tests/test_health.py -v` → FAIL (module missing).

- [ ] **Step 3: Write `app/routing/health.py`**

```python
from __future__ import annotations

import time
from typing import Callable


class HealthTracker:
    """Plain monotonic-clock cooldown store keyed by (provider, model).

    A failing entry is marked unhealthy and skipped until `cooldown_seconds`
    have elapsed, after which it is healthy again (one free attempt). No
    half-open state, no rolling windows. Pure and clock-injectable so tests are
    deterministic without sleeping.
    """

    def __init__(
        self, cooldown_seconds: float, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._cooldown = cooldown_seconds
        self._clock = clock
        self._cooldown_until: dict[tuple[str, str], float] = {}

    def is_healthy(self, provider: str, model: str) -> bool:
        until = self._cooldown_until.get((provider, model))
        if until is None:
            return True
        return self._clock() >= until

    def mark_unhealthy(self, provider: str, model: str) -> None:
        self._cooldown_until[(provider, model)] = self._clock() + self._cooldown
```

- [ ] **Step 4: Run to verify pass** — `pytest tests/test_health.py -v` → PASS.

- [ ] **Step 5: Commit**

```bash
git add app/routing/health.py tests/test_health.py
git commit -m "add clock-injectable cooldown HealthTracker"
```

---

## Task 3: FallbackExecutor — iterate the chain, consult health, report the outcome

**Milestone:** M3.

**Files:**
- Create: `app/routing/executor.py`, `tests/test_executor.py`

**Interfaces:**
- Consumes: `PoolResolver.entries(pool)` (existing), `ProviderAdapter`/`ProviderResponse`/`ProviderError` (existing), `HealthTracker` (Task 2), `Resolution` (existing `app/routing/router.py`).
- Produces:
  - `app.routing.executor.ExecutionOutcome` dataclass: `provider: str`, `model: str`, `response: ProviderResponse | None`, `error: ProviderError | None`, `fallback_from: str | None`, `fallback_hops: int`. On success `response` is set and `error` is `None`; on failure `error` is set and `response` is `None`. `provider`/`model` are the entry that produced the terminal result (the successful entry, or the last-tried entry on failure). `fallback_from` is the first *tried* entry's `"provider/model"` when `fallback_hops > 0`, else `None`.
  - `app.routing.executor.FallbackExecutor(registry: dict[str, ProviderAdapter], pool_resolver: PoolResolver, health: HealthTracker)`.
  - `async execute(body: dict, resolution: Resolution) -> ExecutionOutcome` — iterates the entries of `resolution.pool` (skipping entries in cooldown; if all are cooling, attempts the whole chain best-effort in order). For each attempted entry it calls `registry[entry.provider].chat_completion(body, entry.model)`. On success it returns immediately. On a **retryable** `ProviderError` it marks the entry unhealthy and advances. On a **non-retryable** `ProviderError` it returns immediately without marking. If all attempted entries fail retryably it returns the last error. Never raises; always returns an `ExecutionOutcome`.

- [ ] **Step 1: Write the failing test** `tests/test_executor.py`:

```python
import pytest

from app.core.config import PoolEntry
from app.providers.base import ProviderError, ProviderResponse
from app.routing.executor import ExecutionOutcome, FallbackExecutor
from app.routing.health import HealthTracker
from app.routing.pools import PoolResolver
from app.routing.router import Resolution

POOLS = {
    "cheap": [PoolEntry(provider="deepseek", model="deepseek-chat"),
              PoolEntry(provider="anthropic", model="claude-haiku-4-5")],
    "default": [PoolEntry(provider="anthropic", model="claude-sonnet-5")],
}


class FakeClock:
    def __init__(self, t=1000.0):
        self.t = t

    def now(self):
        return self.t


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
    health = HealthTracker(cooldown_seconds=cooldown,
                           clock=(clock.now if clock else None) or (lambda: 0.0))
    return FallbackExecutor(adapters, PoolResolver(POOLS, "default"), health), health


async def test_first_entry_success_no_fallback():
    resp = ProviderResponse(200, {"id": "ok"}, 5, 2)
    ds = FakeAdapter("deepseek", response=resp)
    an = FakeAdapter("anthropic")
    ex, _ = _executor({"deepseek": ds, "anthropic": an})
    out = await ex.execute({"model": "m"}, _resolution())
    assert out.response is resp and out.error is None
    assert (out.provider, out.model) == ("deepseek", "deepseek-chat")
    assert out.fallback_from is None and out.fallback_hops == 0
    assert ds.calls == 1 and an.calls == 0


async def test_retryable_advances_to_second_entry():
    err = ProviderError("down", 503, "deepseek", retryable=True)
    resp = ProviderResponse(200, {"id": "ok"}, 5, 2)
    ds = FakeAdapter("deepseek", error=err)
    an = FakeAdapter("anthropic", response=resp)
    ex, health = _executor({"deepseek": ds, "anthropic": an})
    out = await ex.execute({"model": "m"}, _resolution())
    assert out.response is resp
    assert (out.provider, out.model) == ("anthropic", "claude-haiku-4-5")
    assert out.fallback_from == "deepseek/deepseek-chat"
    assert out.fallback_hops == 1
    assert ds.calls == 1 and an.calls == 1
    assert health.is_healthy("deepseek", "deepseek-chat") is False   # marked


async def test_non_retryable_returns_immediately_and_does_not_mark():
    err = ProviderError("bad", 400, "deepseek", retryable=False,
                        error_type="invalid_request_error")
    ds = FakeAdapter("deepseek", error=err)
    an = FakeAdapter("anthropic")
    ex, health = _executor({"deepseek": ds, "anthropic": an})
    out = await ex.execute({"model": "m"}, _resolution())
    assert out.response is None and out.error is err
    assert (out.provider, out.model) == ("deepseek", "deepseek-chat")
    assert out.fallback_from is None and out.fallback_hops == 0
    assert ds.calls == 1 and an.calls == 0                 # chain not burned
    assert health.is_healthy("deepseek", "deepseek-chat") is True    # NOT marked


async def test_exhaustion_returns_last_error():
    e1 = ProviderError("down", 503, "deepseek", retryable=True)
    e2 = ProviderError("busy", 429, "anthropic", retryable=True)
    ds = FakeAdapter("deepseek", error=e1)
    an = FakeAdapter("anthropic", error=e2)
    ex, _ = _executor({"deepseek": ds, "anthropic": an})
    out = await ex.execute({"model": "m"}, _resolution())
    assert out.response is None and out.error is e2       # last error
    assert (out.provider, out.model) == ("anthropic", "claude-haiku-4-5")
    assert out.fallback_from == "deepseek/deepseek-chat"
    assert out.fallback_hops == 1


async def test_cooldown_skips_unhealthy_entry_on_next_request():
    e1 = ProviderError("down", 503, "deepseek", retryable=True)
    resp = ProviderResponse(200, {"id": "ok"}, 1, 1)
    clock = FakeClock(1000.0)
    ds = FakeAdapter("deepseek", error=e1)
    an = FakeAdapter("anthropic", response=resp)
    ex, _ = _executor({"deepseek": ds, "anthropic": an}, clock=clock)

    first = await ex.execute({"model": "m"}, _resolution())     # ds fails, an serves
    assert first.fallback_hops == 1 and ds.calls == 1

    second = await ex.execute({"model": "m"}, _resolution())    # ds cooling → skipped
    assert second.response is resp
    assert (second.provider, second.model) == ("anthropic", "claude-haiku-4-5")
    assert second.fallback_from is None and second.fallback_hops == 0
    assert ds.calls == 1                                        # ds NOT called again


async def test_free_attempt_after_cooldown_elapses():
    e1 = ProviderError("down", 503, "deepseek", retryable=True)
    resp = ProviderResponse(200, {"id": "ok"}, 1, 1)
    clock = FakeClock(1000.0)
    ds = FakeAdapter("deepseek", error=e1)
    an = FakeAdapter("anthropic", response=resp)
    ex, _ = _executor({"deepseek": ds, "anthropic": an}, clock=clock)

    await ex.execute({"model": "m"}, _resolution())            # ds fails (call 1)
    clock.t = 1060.0                                           # cooldown elapsed
    await ex.execute({"model": "m"}, _resolution())           # ds free attempt (call 2)
    assert ds.calls == 2


async def test_whole_pool_cooling_best_effort():
    resp = ProviderResponse(200, {"id": "ok"}, 1, 1)
    clock = FakeClock(1000.0)
    # both entries succeed now, but both are pre-marked unhealthy
    ds = FakeAdapter("deepseek", response=resp)
    an = FakeAdapter("anthropic", response=resp)
    ex, health = _executor({"deepseek": ds, "anthropic": an}, clock=clock)
    health.mark_unhealthy("deepseek", "deepseek-chat")
    health.mark_unhealthy("anthropic", "claude-haiku-4-5")
    out = await ex.execute({"model": "m"}, _resolution())      # best-effort, not a no-op
    assert out.response is resp
    assert (out.provider, out.model) == ("deepseek", "deepseek-chat")
    assert ds.calls == 1
```

- [ ] **Step 2: Run to verify failure** — `pytest tests/test_executor.py -v` → FAIL (module missing).

- [ ] **Step 3: Write `app/routing/executor.py`**

```python
from __future__ import annotations

from dataclasses import dataclass

from app.providers.base import ProviderAdapter, ProviderError, ProviderResponse
from app.routing.health import HealthTracker
from app.routing.pools import PoolResolver
from app.routing.router import Resolution


@dataclass
class ExecutionOutcome:
    provider: str
    model: str
    response: ProviderResponse | None
    error: ProviderError | None
    fallback_from: str | None
    fallback_hops: int


def _entry_id(provider: str, model: str) -> str:
    return f"{provider}/{model}"


class FallbackExecutor:
    """Runs the ordered pool chain, skipping entries in cooldown and advancing
    past retryable failures. Returns an ExecutionOutcome; never raises."""

    def __init__(
        self,
        registry: dict[str, ProviderAdapter],
        pool_resolver: PoolResolver,
        health: HealthTracker,
    ) -> None:
        self._registry = registry
        self._pools = pool_resolver
        self._health = health

    async def execute(self, body: dict, resolution: Resolution) -> ExecutionOutcome:
        entries = self._pools.entries(resolution.pool)
        attempts = [
            e for e in entries if self._health.is_healthy(e.provider, e.model)
        ]
        if not attempts:
            attempts = list(entries)   # whole pool cooling → best-effort (decision #5)

        first_id = _entry_id(attempts[0].provider, attempts[0].model)
        last_error: ProviderError | None = None

        for idx, entry in enumerate(attempts):
            adapter = self._registry[entry.provider]
            try:
                resp = await adapter.chat_completion(body, entry.model)
            except ProviderError as exc:
                if not exc.retryable:
                    return ExecutionOutcome(
                        provider=entry.provider, model=entry.model,
                        response=None, error=exc,
                        fallback_from=(first_id if idx > 0 else None),
                        fallback_hops=idx,
                    )
                self._health.mark_unhealthy(entry.provider, entry.model)
                last_error = exc
                continue
            return ExecutionOutcome(
                provider=entry.provider, model=entry.model,
                response=resp, error=None,
                fallback_from=(first_id if idx > 0 else None),
                fallback_hops=idx,
            )

        last = attempts[-1]
        return ExecutionOutcome(
            provider=last.provider, model=last.model,
            response=None, error=last_error,
            fallback_from=(first_id if len(attempts) > 1 else None),
            fallback_hops=len(attempts) - 1,
        )
```

- [ ] **Step 4: Run to verify pass** — `pytest tests/test_executor.py -v` → PASS.

- [ ] **Step 5: Commit**

```bash
git add app/routing/executor.py tests/test_executor.py
git commit -m "add fallback chain executor with cooldown-aware iteration"
```

---

## Task 4: Wire the executor into GatewayService + record fallback metadata

**Milestone:** M3.

**Files:**
- Modify: `app/routing/gateway.py`
- Modify (test): `tests/test_gateway.py`

**Interfaces:**
- Consumes: `Router`/`Resolution` (existing), `FallbackExecutor`/`ExecutionOutcome` (Task 3), `CostTracker`, `RequestsRepository`/`RequestRecord`, `BadRequestError`, `log_request` (all existing).
- Produces:
  - `app.routing.gateway.GatewayService(router: Router, executor: FallbackExecutor, cost: CostTracker, repo: RequestsRepository)` — **the `registry` argument is removed** (now owned by the executor).
  - `complete(request_id: str, body: dict, headers: dict[str, str]) -> dict` — raises `BadRequestError(param="model")` if `body` has no `model`; routes to a pool; delegates the chain to `executor.execute(body, resolution)`; writes exactly one row and one log line reflecting the **served entry** and fallback metadata; returns the served response body on success or re-raises the `ExecutionOutcome.error` on failure.
  - Recording rules (see decision #1/#2): served `provider`/`model`/`cost` come from the outcome; when `fallback_hops > 0`, `route_stage="fallback"`, `route_reason` is prefixed with the router's reason and suffixed with the hop detail, and `fallback_from` is set; otherwise `route_stage`/`route_reason` are the router's and `fallback_from` is `NULL`. Log line carries `fallback_hops` and `fallback_from`.

- [ ] **Step 1: Rewrite `tests/test_gateway.py`** — new executor-based construction plus fallback tests. Full file:

```python
import pytest

from app.core.config import PoolEntry, RuleConfig, RuleWhen
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
        prices={"claude-haiku-4-5": __import__("app.core.config", fromlist=["PriceEntry"]).PriceEntry(
            input_per_1m=1.0, output_per_1m=5.0)})
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
```

Note: the `PriceEntry` inline import in `test_fallback_records_served_entry_and_fallback_from` is intentionally explicit; if preferred, add `from app.core.config import PriceEntry` at the top and use it directly — the assertion is what matters.

- [ ] **Step 2: Run to verify failure** — `pytest tests/test_gateway.py -v` → FAIL (`GatewayService` still takes `registry`).

- [ ] **Step 3: Replace `app/routing/gateway.py`**

```python
from __future__ import annotations

import time

from app.core.errors import BadRequestError
from app.core.logging import log_request
from app.cost.tracker import CostTracker
from app.repositories.requests_repo import RequestRecord, RequestsRepository
from app.routing.executor import ExecutionOutcome, FallbackExecutor
from app.routing.router import Resolution, Router


class GatewayService:
    def __init__(
        self, router: Router, executor: FallbackExecutor,
        cost: CostTracker, repo: RequestsRepository,
    ) -> None:
        self._router = router
        self._executor = executor
        self._cost = cost
        self._repo = repo

    async def complete(self, request_id: str, body: dict, headers: dict[str, str]) -> dict:
        requested = body.get("model")
        if not requested:
            raise BadRequestError("Missing required field 'model'", param="model")

        resolution = self._router.route(body, headers)
        started = time.monotonic()
        outcome = await self._executor.execute(body, resolution)
        latency_ms = int((time.monotonic() - started) * 1000)

        if outcome.response is not None:
            await self._record(request_id, resolution, outcome,
                               status=outcome.response.status_code,
                               input_tokens=outcome.response.input_tokens,
                               output_tokens=outcome.response.output_tokens,
                               latency_ms=latency_ms)
            return outcome.response.body

        assert outcome.error is not None
        await self._record(request_id, resolution, outcome,
                           status=outcome.error.status_code,
                           input_tokens=0, output_tokens=0, latency_ms=latency_ms)
        raise outcome.error

    async def _record(
        self, request_id: str, resolution: Resolution, outcome: ExecutionOutcome,
        status: int, input_tokens: int, output_tokens: int, latency_ms: int,
    ) -> None:
        if outcome.fallback_hops > 0:
            route_stage = "fallback"
            final_id = f"{outcome.provider}/{outcome.model}"
            route_reason = (
                f"{resolution.route_reason}"
                f"|fallback:{outcome.fallback_from}->{final_id}"
                f"|hops={outcome.fallback_hops}"
            )
        else:
            route_stage = resolution.route_stage
            route_reason = resolution.route_reason

        cost = self._cost.compute(outcome.model, input_tokens, output_tokens)
        record = RequestRecord(
            id=request_id, ts=int(time.time()), pool=resolution.pool,
            provider=outcome.provider, model=outcome.model,
            route_stage=route_stage, route_reason=route_reason,
            input_tokens=input_tokens, output_tokens=output_tokens, cost_usd=cost,
            latency_ms=latency_ms, status=status, fallback_from=outcome.fallback_from,
        )
        await self._repo.insert(record)
        log_request({
            "request_id": request_id, "pool": resolution.pool,
            "provider": outcome.provider, "model": outcome.model,
            "route_stage": route_stage, "route_reason": route_reason,
            "input_tokens": input_tokens, "output_tokens": output_tokens,
            "cost_usd": cost, "latency_ms": latency_ms, "status": status,
            "fallback_hops": outcome.fallback_hops, "fallback_from": outcome.fallback_from,
        })
```

- [ ] **Step 4: Run to verify pass** — `pytest tests/test_gateway.py -v` → PASS.

- [ ] **Step 5: Commit**

```bash
git add app/routing/gateway.py tests/test_gateway.py
git commit -m "record served entry and fallback metadata in gateway service"
```

---

## Task 5: Lifespan wiring + endpoint fallback test + full suite green

**Milestone:** M3.

**Files:**
- Modify: `app/main.py`
- Create (test): `tests/test_fallback_endpoint.py`

**Interfaces:**
- Consumes: `HealthTracker` (Task 2), `FallbackExecutor` (Task 3), `GatewayService` (Task 4), existing `PoolResolver`, `Router`, `RuleEngine`, `build_registry`.
- Produces: `create_app` builds one long-lived `HealthTracker` (so cooldowns persist across requests) and a `FallbackExecutor`, and injects the executor into `GatewayService`. No public signature change to `create_app`.

- [ ] **Step 1: Edit `app/main.py`** — add imports:

```python
from app.routing.executor import FallbackExecutor
from app.routing.health import HealthTracker
```

Replace the executor/gateway construction inside `lifespan` (the block that currently ends with `app.state.gateway = GatewayService(router, registry, cost, repo)`):

```python
        registry = build_registry(config, http_client)
        rule_engine = RuleEngine(config.rules)
        pool_resolver = PoolResolver(config.pools, config.default_pool)
        router = Router(rule_engine, pool_resolver)
        health = HealthTracker(cooldown_seconds=config.cooldown_seconds)
        executor = FallbackExecutor(registry, pool_resolver, health)
        cost = CostTracker(config.prices)
        repo = RequestsRepository(db.connection)
        app.state.config = config
        app.state.db = db
        app.state.http_client = http_client
        app.state.gateway = GatewayService(router, executor, cost, repo)
```

- [ ] **Step 2: Write `tests/test_fallback_endpoint.py`** — end-to-end fallback + cooldown through the chat endpoint. Uses a purpose-built app with a two-entry `cheap` pool and a fake monotonic clock injected into the app's `HealthTracker` so cooldown is deterministic.

```python
import json

import httpx
import pytest
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

    # request 1: deepseek fails → marked unhealthy → anthropic serves
    r1 = await client.post("/v1/chat/completions", headers=_auth(), json=_short_body())
    assert r1.status_code == 200 and ds.call_count == 1

    # request 2 within cooldown: deepseek skipped (not called again)
    r2 = await client.post("/v1/chat/completions", headers=_auth(), json=_short_body())
    assert r2.status_code == 200 and ds.call_count == 1     # unchanged → skipped

    # advance past cooldown: deepseek gets its one free attempt
    clock.t = 1060.0
    r3 = await client.post("/v1/chat/completions", headers=_auth(), json=_short_body())
    assert r3.status_code == 200 and ds.call_count == 2     # attempted again
```

Note on the clock injection: the test reaches into `app.state.gateway._executor._health` to swap the clock deterministically. This is a test-only seam justified by the "no real sleeping" constraint; production uses the default `time.monotonic`. If a cleaner seam is preferred, expose the `HealthTracker` on `app.state` in Task 5 Step 1 (`app.state.health = health`) and swap the clock there instead — the assertion behavior is identical.

- [ ] **Step 3: Run the full suite** — `pytest -v` → all PASS. Confirm the ripple set (`test_chat_endpoint.py`, `test_app_integration.py`, `test_acceptance_m1.py`, `test_acceptance_m2.py`) is green unchanged.

- [ ] **Step 4: Commit**

```bash
git add app/main.py tests/test_fallback_endpoint.py
git commit -m "wire fallback executor and cooldown health into the app"
```

---

## Task 6: Independent M3 acceptance tests

**Milestone:** M3.

**Files:**
- Create: `tests/test_acceptance_m3.py`

**Interfaces:**
- Consumes: the public HTTP surface + `HealthTracker`/`FallbackExecutor` behavior. Derived from the Acceptance Criteria above, NOT from the implementation.

- [ ] **Step 1: Write `tests/test_acceptance_m3.py`** — one test per M3-AC. Reuse the `app_and_client` factory pattern and `FakeClock` from Task 5 (copy them into this file so the acceptance suite is self-contained), then assert each criterion:
  - **M3-AC-F1** — deepseek 503 → anthropic 200; assert 200 body from anthropic; row `(provider, model, route_stage, fallback_from, status) == ("anthropic", "claude-haiku-4-5", "fallback", "deepseek/deepseek-chat", 200)`.
  - **M3-AC-F2** — 503 then 429; assert client status 429 in OpenAI shape; row `status=429`, tokens/cost `0`, `route_stage="fallback"`, `fallback_from="deepseek/deepseek-chat"`.
  - **M3-AC-F3** — first entry 400; assert client 400, anthropic route `not called`, row `(status, route_stage, fallback_from) == (400, "rule", None)`.
  - **M3-AC-F4** — parametrize first-entry status over `[401, 403]`; identical shape to F3 (immediate, second entry not called, no fallback recorded).
  - **M3-AC-F5** — first entry raises a transport timeout (respx `side_effect=httpx.ConnectTimeout("boom")`) then anthropic 200; assert 200 from anthropic and `route_stage="fallback"`.
  - **M3-AC-F6** — first entry 429 then anthropic 200; assert fallback served.
  - **M3-AC-H1 / H2** — inject `FakeClock`, run three requests as in `test_endpoint_cooldown_skips_then_free_attempt`; assert skip within window and one free attempt after `cooldown_seconds`.
  - **M3-AC-H3** — direct `HealthTracker` unit assertions with an injected clock (unknown healthy; unhealthy within window; healthy at boundary; no sleep).
  - **M3-AC-H4** — first entry 400 twice across two requests; assert deepseek is attempted on the second request (not marked unhealthy by a non-retryable).
  - **M3-AC-H5** — pre-mark both `cheap` entries unhealthy via `app.state.gateway._executor._health.mark_unhealthy(...)`, then deepseek returns 200; assert the request still succeeds (best-effort) and deepseek was called.
  - **M3-AC-C1** — covered by the F1 cost assertion (served model's price).
  - **M3-AC-C2** — any fallback/exhaustion test asserts exactly one row via `SELECT count(*)`.
  - **M3-AC-X1** — a healthy-first-entry request: row `route_stage="rule"`, `fallback_from IS NULL`, `provider="deepseek"`; captured log line (attach a `logging.Handler` to the `"gateway"` logger as in `test_acceptance_m2.py::test_ac_r9`) has `fallback_hops == 0`.
  - **M3-AC-CFG** — `load_config(config.example.yaml)` yields `cooldown_seconds == 60.0` (set the three example env vars as in `test_acceptance_m2.py`).

- [ ] **Step 2: Run** — `pytest tests/test_acceptance_m3.py -v` → all PASS.

- [ ] **Step 3: Run the entire suite once more** — `pytest -v` → all PASS.

- [ ] **Step 4: Commit**

```bash
git add tests/test_acceptance_m3.py
git commit -m "add independent M3 fallback and cooldown acceptance tests"
```

---

## Self-Review (completed by planner)

- **Spec coverage:** (1) Fallback chain advancing on retryable failures → Task 3 (`FallbackExecutor`), Tasks 4–5 (row/endpoint), M3-AC-F1/F5/F6. (2) Exhaustion returns the last error in OpenAI shape → executor + gateway re-raise + registered handler; M3-AC-F2. (3) Non-retryable returns immediately without burning the chain → executor early-return, no mark; M3-AC-F3/F4/H4. (4) 60s cooldown, one free attempt, monotonic clock, injectable → Task 2 (`HealthTracker`), Task 1 (config), M3-AC-H1/H2/H3. (5) Record fallback in the row (`fallback_from`, `route_stage`/`route_reason`) and surface `fallback_hops` in logs → Task 4; M3-AC-F1/F2/X1. Scope guard: no classifier, no dashboard, no half-open/rolling-window machine.
- **Reuses existing seams:** `PoolResolver.entries(pool)` (built in M2 "so M3's fallback chain iterates it without rework"), `ProviderError.retryable` + `is_retryable` (seeded in M1), and the `fallback_from` column (present since M1, NULL until now). No schema change, no new dependency, no new column.
- **Type consistency:** `HealthTracker(cooldown_seconds, clock)`, `FallbackExecutor(registry, pool_resolver, health)`, `ExecutionOutcome(provider, model, response, error, fallback_from, fallback_hops)`, `GatewayService(router, executor, cost, repo)`, and `complete(request_id, body, headers)` are used identically across producing and consuming tasks and tests. `registry` moves from `GatewayService` into `FallbackExecutor`; the only two construction sites (`app/main.py`, `tests/test_gateway.py`) are both updated (verified: no other `GatewayService(` call sites exist).
- **Invariant checks:** exactly one row + one log line per request (single `_record` call per `complete`); cost computed at write time on the *served* model from provider `usage`; historical rows untouched; OpenAI error shape on exhaustion (re-raise `ProviderError` → existing handler); unknown fields still forwarded and body unchanged (executor passes `body` untouched, adapter rewrites only `model`); constant-time auth unchanged. Routing precedence preserved (executor never re-routes).
- **Placeholder scan:** every code step contains concrete code; Task 6 enumerates each acceptance test explicitly against a named criterion. No TBD/TODO.
- **Ripple verification is an explicit step** (Task 5 Step 3 / Task 6 Step 3): the M1/M2 acceptance suites and endpoint/integration tests must remain green, since all their fixtures use healthy single-primary-entry providers and therefore never enter the fallback path.

---

## Open Questions

- **Blocking? No — defaulted (decision #5).** *Behavior when the entire chosen pool is in cooldown.* This plan defaults to **best-effort**: attempt the full chain in order rather than fail without an upstream call, preserving availability (architecture success-criterion #4). The alternative — return a synthetic `503 "all upstreams cooling"` in OpenAI shape without any call — is defensible if the owner would rather fail fast than pay latency on known-bad models. Easy to switch later (one branch in `FallbackExecutor.execute`). Flagged for the owner's awareness; not blocking autonomous progress.
- **Non-blocking (decision #1).** *Whether a fallback hop should overwrite `route_stage` with `"fallback"`.* Chosen: yes, with the routing origin preserved inside `route_reason`, because the domain invariant and schema enumerate `fallback` as a `route_stage` value. If the owner later prefers `route_stage` to always denote the pool-selection stage (so M4's `classifier` origin survives a hop), switch to relying solely on the `fallback_from` column — a localized change in `GatewayService._record`.
