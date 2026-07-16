# M4 — Classifier Stage + Routing-Reason Capture Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Run-id:** 20260716-m4-classifier

**Goal:** Add a stage-2 classifier that decides a pool for requests the stage-1 YAML rules do not resolve. When rules produce no match and the classifier is enabled, a cheap model sees a truncated view of the request and returns a pool label; on timeout, error, or unparseable output the request degrades to a configured `fallback_pool`. Every classifier decision is recorded as `route_stage="classifier"` with an explanatory `route_reason`. When the classifier is disabled (the default), behavior is byte-identical to M2/M3 (no-match → `default_pool`, `route_stage="default"`).

**Architecture:** A small, single-purpose, injectable `Classifier` (`app/routing/classifier.py`) is consulted by the `Router` **only on stage-1 no-match**. The classifier depends on an injected async *caller* (`ClassifierCaller`) and never talks to a provider directly — this keeps it fully unit-testable without real model calls. The production caller (`app/routing/classifier_caller.py`) runs the probe against the classifier's configured cheap pool through the **existing M3 `FallbackExecutor`**, so the classifier's own call can fall back within its cheap pool; its outcome is reduced to a single label string. A hard timeout (`asyncio.wait_for`) bounds the call without blocking the event loop, and **any** exception/timeout/garbage degrades to `fallback_pool` — a classifier failure never fails the client request. `Router.route` becomes `async` (it now may await the classifier); the `GatewayService` awaits it. The existing gateway recording logic is unchanged: a within-request provider hop still overwrites `route_stage` with `"fallback"` while preserving the classifier origin inside `route_reason` (classifier stage and fallback hop are orthogonal — documented below).

**Tech Stack:** Python 3.11+, FastAPI, Pydantic v2 (all already present). No new dependencies. Tests: pytest (`asyncio_mode = "auto"`), pytest-asyncio, respx, `httpx.AsyncClient` (existing setup). Classifier timeout/garbage/error paths are exercised with an injected fake caller — no real model calls, no real sleeping except one bounded (`~50 ms`) test that proves `asyncio.wait_for` is wired.

## Global Constraints

- **Rules before classifier, always.** Stage 1 (`RuleEngine`) is authoritative and unchanged. The classifier runs **only** when `RuleEngine.match` returns `None` *and* the classifier is enabled. A request that matches any rule must never invoke the classifier (assert the probe upstream was not called).
- **The classifier is a hint, never a hard dependency.** A hard timeout bounds the probe. On timeout, on any exception, or on output not in the configured `labels`, route to `classifier.fallback_pool`. A classifier failure must **never** propagate as a client error and must **never** fail the request.
- **`enabled: false` is the default and preserves M2/M3 exactly.** When the classifier is disabled (or unconfigured), the no-match path routes to `default_pool` with `route_stage="default"`, `route_reason="no-rule-matched"` — unchanged from M2. Test fixtures (`tests/conftest.py`) leave the classifier unset, so all existing endpoint/acceptance suites are unaffected.
- **Config-driven; no hardcoded model names, prices, or thresholds.** `pool`, `labels`, `fallback_pool`, `timeout_s`, and the truncation cap come from `config.yaml`. Secrets stay env-var-sourced (no change to secret handling). The only string literal defaulted in code is the classifier's instruction sentence (a prompt, not a model/price/threshold).
- **One row per client request.** The classifier probe runs through the executor directly (via the caller) and does **not** insert a `requests` row or emit a request log line. Exactly one row and one JSON log line are written per client request, by `GatewayService`, as in M3.
- **No schema change.** `route_stage` is already a free-text `TEXT` column enumerating `rule | classifier | fallback` (plus M2's `default`). Storing `"classifier"` needs no migration.
- **OpenAI compatibility must not regress.** Request/response bodies pass through unchanged except the `model` field. Unknown fields forwarded. Errors in OpenAI shape. Constant-time bearer auth on `/v1/*` and `/dashboard` unchanged.
- **Scope guard (do NOT build in M4):** dashboard UI (M5), Docker/VPS deploy (M6), and everything in the architecture "Deferred" table — in particular **no decision/semantic caching of classifier results** (the architecture's open question on caching is explicitly left OUT), no latency-aware routing, no streaming, no live reload, no multi-key auth.
- **Tests never hit real provider APIs.** Mock outbound HTTP with respx or inject fakes. Temp-file SQLite (not `:memory:`). No real sleeping except the single bounded `asyncio.wait_for` wiring test.

---

## M4 Design Decisions (documented assumptions)

The owner authorized autonomous progress (see `.claude/CLAUDE.md` → "Autonomous milestone execution"). Each item below is a chosen, documented default; none is blocking. Genuinely owner-facing items are flagged in **Open Questions**.

1. **Truncation (resolves the architecture "full prompt vs truncated slice" open question): a truncated slice.** The classifier sees a two-part view built from the request's **system prompt** and its **last user message**, each truncated to `classifier.max_probe_chars` characters (default **2000**). Rationale: cheap and bounded, per the architecture's "truncated view" language; the system prompt carries task intent and the last user turn carries the concrete ask, which together are the strongest cheap signal. Character-based truncation (not token-based) reuses the existing 4-chars/token estimation philosophy and needs no tokenizer. The cap is config-driven so it can be tuned from real traffic.
2. **The classifier's output is a single label equal to a pool name, parsed by exact match after `strip()`.** The instruction tells the model to reply with exactly one label and nothing else. Parsing is strict: `raw.strip()` must equal one of the configured `labels` (which are validated at config load to be real pool names). Anything else — extra prose, a wrong label, empty, non-string — is **garbage** → `fallback_pool`. (KISS: no fuzzy matching, no "first token that looks like a label"; a strict contract is easier to reason about and to tune the prompt against.)
3. **Distinct `route_reason` strings for each degrade path**, so real traffic is diagnosable: `classifier:label=<label>` on success, `classifier:timeout-><fallback_pool>` on hard timeout, `classifier:error-><fallback_pool>` on any other exception (including the probe pool being exhausted), `classifier:garbage-><fallback_pool>` on unparseable/wrong output.
4. **Classifier stage and fallback hop are orthogonal; the M3 recording rule is preserved unchanged.** `GatewayService._record` already sets `route_stage="fallback"` **iff** a within-request provider hop occurred, prefixing the router's original `route_reason` into the stored reason. So:
   - Classifier picks pool X and X's first entry serves → `route_stage="classifier"`, `route_reason="classifier:label=X"`.
   - Classifier picks pool X and the completion hops inside X → `route_stage="fallback"`, `route_reason="classifier:label=X|fallback:<first>-><final>|hops=n"`, `fallback_from` set. The classifier origin survives inside `route_reason`. **No change to `GatewayService._record` is required** — it already composes reasons generically from `resolution.route_stage`/`resolution.route_reason`.
5. **The classifier consults the executor, so its own probe can fall back within the cheap pool.** The production caller builds a `Resolution` for `classifier.pool` and calls the shared `FallbackExecutor`. If the probe pool is exhausted (no success), the caller raises → the classifier degrades to `fallback_pool` (`classifier:error`). This reuses the M3 provider path exactly as required; the probe shares the same `HealthTracker`, so a model failing during a probe is cooled for subsequent real traffic too.
6. **The probe is an internal meta-call and is NOT recorded in the `requests` table or its cost.** V1 records the client-facing request only (one row per client request). The extra probe token cost is unaccounted in M4 — a known, documented limitation; wiring probe cost into the ledger/dashboard is deferred to M5 when there is a consumer for it. (Non-blocking; flagged in Open Questions.)
7. **`Router.route` becomes `async`.** Consulting the classifier requires awaiting an I/O call, and the constraint is explicit that "the Router consults the classifier". Making `route` async is the faithful, correctly-layered choice (routing logic stays in `routing/`, not the gateway). The change is mechanical for callers: `GatewayService.complete` awaits it, and two existing sync call-sites in tests gain `await`. Behavior with the classifier disabled (`classifier=None`) is identical to M2/M3.
8. **Behavior when the classifier's chosen pool is entirely in cooldown is inherited from M3, not re-decided here.** The classifier only selects a *pool name*; the actual completion goes through the M3 executor, which already degrades a fully-cooling pool to best-effort. M4 adds nothing here. (This is a *different* architecture open question from the classifier's own probe pool; both are covered by M3's best-effort rule.)

---

## File Structure

**Create (application):**
- `app/routing/classifier.py` — `ClassifierDecision` dataclass, `ClassifierCaller` type alias, and `Classifier` (builds the truncated probe, calls the injected caller under a hard timeout, parses strictly, degrades to `fallback_pool`). Depends only on `app.routing.features` — no provider/executor imports (keeps it a pure, injectable unit and avoids an import cycle with `Router`).
- `app/routing/classifier_caller.py` — `ExecutorProbeCaller` (production `ClassifierCaller`): runs the probe against `classifier.pool` via the shared `FallbackExecutor` and returns the model's raw text; raises on any failure so the `Classifier` degrades.

**Modify (application):**
- `app/core/config.py` — add `ClassifierConfig` model + `AppConfig.classifier` field (default disabled) + validation when enabled.
- `app/routing/features.py` — add `RequestFeatures.last_user_text: str = ""` and populate it in `extract_features` (additive, defaulted; existing direct constructions unaffected).
- `app/routing/router.py` — `Router.__init__(..., classifier: Classifier | None = None)`; `route` becomes `async` and consults the classifier on stage-1 no-match.
- `app/routing/gateway.py` — `complete` awaits `self._router.route(...)` (one-word change; `_record` unchanged).
- `app/main.py` — build the `Classifier` + `ExecutorProbeCaller` in the lifespan when `config.classifier.enabled`, and inject the classifier into `Router` (build the executor before the classifier; the router last).
- `config.example.yaml` — add the `classifier:` block (enabled, matching the architecture doc).

**Create (tests):**
- `tests/test_classifier.py` — `Classifier` unit tests with fake callers (label / timeout / error / garbage / truncation / bounded-`wait_for`).
- `tests/test_classifier_caller.py` — `ExecutorProbeCaller` unit tests with a fake executor (extracts content; raises on exhaustion/bad shape).
- `tests/test_classifier_endpoint.py` — end-to-end classifier routing through the chat endpoint with respx (enabled label path, garbage→fallback, error→fallback, rules-first-skips-classifier, disabled path, classifier+fallback orthogonality).
- `tests/test_acceptance_m4.py` — independent acceptance tests derived from the criteria below (not from the implementation).

**Modify (tests):**
- `tests/test_router.py` — convert the five tests to `async def` and `await router.route(...)` (route is now async); add classifier-consultation tests with a fake `Classifier`. Existing assertions preserved.
- `tests/test_acceptance_m2.py` — **mechanical only:** in `test_ac_r10_routing_pure_and_never_raises` (already an `async def`), change `res = router.route(...)` to `res = await router.route(...)`. No assertion changes.
- `tests/test_config.py` — add classifier default + example-ships-classifier + validation tests.

**Ripple check (should pass unchanged, verify in Task 6/7):** `tests/test_chat_endpoint.py`, `tests/test_app_integration.py`, `tests/test_acceptance_m1.py`, `tests/test_acceptance_m3.py`, `tests/test_fallback_endpoint.py`, `tests/test_gateway.py`, `tests/test_pools.py`, `tests/test_rules.py`, `tests/test_features.py`, `tests/test_cost.py`, `tests/test_providers.py`, `tests/test_executor.py`, `tests/test_health.py`, `tests/test_models_endpoint.py`, `tests/test_security.py`, `tests/test_dashboard.py`, `tests/test_database.py`, `tests/test_errors.py`, `tests/test_logging.py`, `tests/test_tester_m3.py`, `tests/test_acceptance_m3.py`. All of these use fixtures with the classifier **disabled** (`conftest.py` and the M-suite factories never set `classifier`), so no probe path is triggered and their assertions are unaffected. `test_features.py` constructs `RequestFeatures` — the new field is defaulted, so those constructions still succeed.

---

## Acceptance Criteria (for the tester — derivable without reading implementation)

Unless stated otherwise, assume an app whose config has: a single-entry `cheap` pool `deepseek/deepseek-chat` (upstream `https://api.deepseek.com/v1/chat/completions`); a single-entry `default` pool `anthropic/claude-sonnet-5` (upstream `https://api.anthropic.com/v1/chat/completions`); `default_pool: default`; a rule `short-and-simple` (`max_input_tokens: 500, has_tools: false → cheap`) so short tool-less requests match a rule; **no** rule that matches a mid-size (~1000-token) tool-less request, so such a request reaches stage 2; and a classifier block `{ enabled, pool: cheap, labels: [cheap, default], fallback_pool: default, timeout_s, max_probe_chars: 2000 }`. A request "reaches the classifier" only when it matches no rule. "OpenAI error shape" means `body["error"]` is an object with a string `message`. The **probe** is the classifier's own call to its `pool` (upstream = that pool's first entry, i.e. deepseek); the **completion** is the client request routed to the chosen pool.

### Config

- **M4-AC-CFG1 Classifier defaults to disabled.** `load_config` on a YAML without a `classifier:` block yields `cfg.classifier.enabled is False`. A no-rule-match request through such an app routes to `default_pool` with `route_stage="default"`, `route_reason="no-rule-matched"` (identical to M2/M3).
- **M4-AC-CFG2 Example ships an enabled classifier.** `load_config(config.example.yaml)` yields `cfg.classifier.enabled is True`, `cfg.classifier.pool == "cheap"`, `cfg.classifier.labels == ["cheap", "default"]`, `cfg.classifier.fallback_pool == "default"`.
- **M4-AC-CFG3 Enabled-classifier validation.** With `classifier.enabled: true`, `load_config` raises `ValueError` if any of: `pool` is not a defined pool; `fallback_pool` is not a defined pool; `labels` is empty; any label is not a defined pool. With `classifier.enabled: false` (or absent), none of these are validated (a disabled/empty classifier config loads fine).

### Routing — classifier enabled

- **M4-AC-R1 Label path.** A request that matches no rule, where the probe (deepseek) returns a completion whose assistant text is `"default"`, is routed to pool `default` and served by anthropic. The single `requests` row has `pool="default"`, `provider="anthropic"`, `model="claude-sonnet-5"`, `route_stage="classifier"`, `route_reason="classifier:label=default"`, `status=200`.
- **M4-AC-R2 Rules-first — classifier is skipped.** A short tool-less request (matches `short-and-simple`) is served by the cheap pool with `route_stage="rule"`, `route_reason="rule:short-and-simple"`. The probe is **never issued** (the classifier is not consulted when a rule matches — assert deepseek was called exactly once, for the completion, not twice).
- **M4-AC-R3 Garbage → fallback.** A no-rule-match request where the probe returns assistant text `"banana"` (not in `labels`) is routed to `fallback_pool` (`default`) and served by anthropic (200). Row: `route_stage="classifier"`, `route_reason="classifier:garbage->default"`, `pool="default"`. The client never sees an error.
- **M4-AC-R4 Error → fallback.** A no-rule-match request where the probe upstream (deepseek) returns `500` (so the single-entry probe pool is exhausted) is routed to `fallback_pool` (`default`) and served by anthropic (200). Row: `route_stage="classifier"`, `route_reason="classifier:error->default"`. The client never sees an error.
- **M4-AC-R5 Timeout → fallback (unit-level; see M4-AC-U2/U5).** When the classifier's call exceeds `timeout_s`, the decision is `fallback_pool` with `route_reason="classifier:timeout->default"`, and the request still succeeds against the fallback pool. (Deterministically asserted at the classifier unit level; the endpoint suite need not reproduce a real timeout.)
- **M4-AC-R6 Disabled preserves M2/M3.** With `classifier.enabled: false`, a no-rule-match request routes to `default_pool` with `route_stage="default"`, `route_reason="no-rule-matched"` — no probe is issued.
- **M4-AC-R7 Classifier + fallback are orthogonal.** Give the chosen pool two entries (e.g. `default = [anthropic/claude-sonnet-5, openai/gpt-4o-mini]`); the classifier selects `default`, its first entry returns a retryable `503`, its second returns `200`. The row has `route_stage="fallback"`, `route_reason` **starts with** `"classifier:label=default"` and contains `"fallback:"` and `"hops=1"`, and `fallback_from` is the first entry id. The classifier origin is preserved inside `route_reason`.
- **M4-AC-R8 A classifier failure never fails the request.** For each degrade path (garbage, error, timeout), given a healthy `fallback_pool`, the client receives `200` from the fallback pool. No degrade path returns a 4xx/5xx caused by the classifier itself.

### Classifier unit (deterministic, no real provider calls)

- **M4-AC-U1 Valid label.** With `labels=["cheap","default"]`, a caller returning `"default"` (or `" default\n"`, exercising `strip()`) yields `ClassifierDecision(pool="default", reason="classifier:label=default")`.
- **M4-AC-U2 Timeout reason.** A caller that raises `asyncio.TimeoutError` yields `ClassifierDecision(pool=<fallback_pool>, reason="classifier:timeout-><fallback_pool>")`.
- **M4-AC-U3 Error reason.** A caller that raises any other exception (e.g. `RuntimeError`) yields `ClassifierDecision(pool=<fallback_pool>, reason="classifier:error-><fallback_pool>")`.
- **M4-AC-U4 Garbage reason.** A caller returning a string not in `labels` (e.g. `"banana"`, `""`, or a non-string) yields `ClassifierDecision(pool=<fallback_pool>, reason="classifier:garbage-><fallback_pool>")`.
- **M4-AC-U5 Hard timeout is wired and bounded.** A caller that awaits `asyncio.sleep(5)` with `timeout_s=0.05` returns a timeout decision in well under a second (proves `asyncio.wait_for` bounds a genuinely slow call without hanging).
- **M4-AC-U6 Truncated probe.** For features with a system prompt and last user message each longer than `max_probe_chars`, the probe handed to the caller (a) contains exactly one `system` and one `user` message, (b) truncates each source text to `max_probe_chars` characters, and (c) carries a constrained-output cap (`max_tokens == max_output_tokens`). The classifier makes routing decisions **only** from the system prompt and the last user message (earlier user/assistant turns are not included).

### Caller unit

- **M4-AC-P1 Extracts assistant text.** `ExecutorProbeCaller` given a fake executor whose outcome response body is `{"choices":[{"message":{"content":"cheap"}}]}` returns the string `"cheap"`, and it calls the executor with a `Resolution` whose `pool` equals the configured classifier pool.
- **M4-AC-P2 Raises on exhaustion.** When the fake executor's outcome has `response is None` (probe pool exhausted), the caller raises (so the classifier degrades to `fallback_pool`).
- **M4-AC-P3 Raises on malformed shape.** When the response body lacks `choices[0].message.content`, the caller raises (degrade to fallback), never returns a partial/`None` label.

### No regression

- **M4-AC-X1 One row, one log line per client request.** Any classifier-routed request (label or degrade) writes exactly one `requests` row and one JSON log line; the probe adds no extra row or request log line.
- **M4-AC-X2 M1/M2/M3 suites green.** All existing acceptance/endpoint suites pass unchanged; the only edits to prior test files are the mechanical `await` in `test_acceptance_m2::test_ac_r10` and the async conversion of `test_router.py`.

---

## Task 1: Add `ClassifierConfig` to config + example + validation

**Milestone:** M4.

**Files:**
- Modify: `app/core/config.py`, `config.example.yaml`
- Modify (test): `tests/test_config.py`

**Interfaces:**
- Consumes: existing `AppConfig`, `load_config`, `_validate`.
- Produces:
  - `app.core.config.ClassifierConfig(enabled: bool = False, pool: str = "", labels: list[str] = [], fallback_pool: str = "", timeout_s: float = 5.0, max_probe_chars: int = 2000, max_output_tokens: int = 8)`.
  - `AppConfig.classifier: ClassifierConfig` (default-constructed, i.e. disabled, when absent from YAML).
  - `_validate` raises `ValueError` for an **enabled** classifier referencing undefined pools or empty labels.

- [ ] **Step 1: Append failing tests to `tests/test_config.py`**

```python
def _classifier_yaml(block: str) -> str:
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


def test_classifier_defaults_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("K", "x")
    ok = tmp_path / "ok.yaml"
    ok.write_text(_classifier_yaml(""))
    cfg = load_config(ok)
    assert cfg.classifier.enabled is False


def test_example_config_ships_enabled_classifier(monkeypatch):
    _set_env(monkeypatch, EXAMPLE_ENV)
    cfg = load_config(EXAMPLE)
    assert cfg.classifier.enabled is True
    assert cfg.classifier.pool == "cheap"
    assert cfg.classifier.labels == ["cheap", "default"]
    assert cfg.classifier.fallback_pool == "default"


def test_enabled_classifier_rejects_unknown_pool(tmp_path, monkeypatch):
    monkeypatch.setenv("K", "x")
    bad = tmp_path / "bad.yaml"
    bad.write_text(_classifier_yaml(
        "classifier:\n  enabled: true\n  pool: nope\n"
        "  labels: [cheap, default]\n  fallback_pool: default\n"))
    with pytest.raises(ValueError):
        load_config(bad)


def test_enabled_classifier_rejects_unknown_fallback_pool(tmp_path, monkeypatch):
    monkeypatch.setenv("K", "x")
    bad = tmp_path / "bad.yaml"
    bad.write_text(_classifier_yaml(
        "classifier:\n  enabled: true\n  pool: cheap\n"
        "  labels: [cheap, default]\n  fallback_pool: nope\n"))
    with pytest.raises(ValueError):
        load_config(bad)


def test_enabled_classifier_rejects_unknown_label(tmp_path, monkeypatch):
    monkeypatch.setenv("K", "x")
    bad = tmp_path / "bad.yaml"
    bad.write_text(_classifier_yaml(
        "classifier:\n  enabled: true\n  pool: cheap\n"
        "  labels: [cheap, nope]\n  fallback_pool: default\n"))
    with pytest.raises(ValueError):
        load_config(bad)


def test_enabled_classifier_rejects_empty_labels(tmp_path, monkeypatch):
    monkeypatch.setenv("K", "x")
    bad = tmp_path / "bad.yaml"
    bad.write_text(_classifier_yaml(
        "classifier:\n  enabled: true\n  pool: cheap\n"
        "  labels: []\n  fallback_pool: default\n"))
    with pytest.raises(ValueError):
        load_config(bad)


def test_disabled_classifier_skips_validation(tmp_path, monkeypatch):
    monkeypatch.setenv("K", "x")
    ok = tmp_path / "ok.yaml"
    # references a bogus pool but disabled -> must NOT raise
    ok.write_text(_classifier_yaml(
        "classifier:\n  enabled: false\n  pool: nope\n"
        "  labels: [nope]\n  fallback_pool: nope\n"))
    cfg = load_config(ok)
    assert cfg.classifier.enabled is False
```

- [ ] **Step 2: Run to verify failure** — `pytest tests/test_config.py -k classifier -v` → FAIL (`AppConfig` has no `classifier`; example lacks the block).

- [ ] **Step 3: Edit `app/core/config.py`** — add the model (after `RuleConfig`):

```python
class ClassifierConfig(BaseModel):
    enabled: bool = False
    pool: str = ""              # cheap pool the classifier itself runs on
    labels: list[str] = Field(default_factory=list)  # candidate pool names
    fallback_pool: str = ""     # used on timeout / error / garbage
    timeout_s: float = 5.0      # hard timeout for the classifier call
    max_probe_chars: int = 2000  # per-slice truncation cap (system, last user)
    max_output_tokens: int = 8   # constrained classifier output
```

Add the field to `AppConfig` (after `rules`):

```python
    rules: list[RuleConfig] = Field(default_factory=list)
    classifier: ClassifierConfig = Field(default_factory=ClassifierConfig)
```

At the end of `_validate`, add:

```python
    c = cfg.classifier
    if c.enabled:
        if c.pool not in cfg.pools:
            raise ValueError(f"classifier.pool '{c.pool}' is not a defined pool")
        if c.fallback_pool not in cfg.pools:
            raise ValueError(
                f"classifier.fallback_pool '{c.fallback_pool}' is not a defined pool"
            )
        if not c.labels:
            raise ValueError("classifier.labels must be non-empty when enabled")
        for label in c.labels:
            if label not in cfg.pools:
                raise ValueError(
                    f"classifier label '{label}' is not a defined pool"
                )
```

- [ ] **Step 4: Edit `config.example.yaml`** — append the classifier block after the `rules:` section:

```yaml
# Stage-2 classifier: consulted ONLY when no stage-1 rule matches. It runs a cheap
# model that returns exactly one label (a pool name); on timeout/error/garbage the
# request degrades to fallback_pool. A classifier failure never fails the request.
classifier:
  enabled: true
  pool: cheap              # the classifier itself runs on the cheap pool
  labels: [cheap, default] # allowed outputs; each must be a defined pool
  fallback_pool: default   # used on timeout / error / unparseable output
  timeout_s: 5.0           # hard timeout for the classifier call
  max_probe_chars: 2000    # truncate system prompt + last user message to this many chars each
  max_output_tokens: 8     # constrained output for the label
```

- [ ] **Step 5: Run to verify pass** — `pytest tests/test_config.py -v` → PASS.

- [ ] **Step 6: Commit**

```bash
git add app/core/config.py config.example.yaml tests/test_config.py
git commit -m "add classifier config block with enabled-only validation"
```

---

## Task 2: Add `last_user_text` to request features

**Milestone:** M4.

**Files:**
- Modify: `app/routing/features.py`
- Modify (test): `tests/test_features.py`

**Interfaces:**
- Consumes: existing `extract_features`, `_content_text`.
- Produces: `RequestFeatures.last_user_text: str = ""` — the text content of the **last** `role == "user"` message (empty string when there is none). Additive and defaulted; existing direct constructions of `RequestFeatures` remain valid.

- [ ] **Step 1: Append failing tests to `tests/test_features.py`**

```python
from app.routing.features import extract_features


def test_last_user_text_is_final_user_message():
    body = {"messages": [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "reply"},
        {"role": "user", "content": "second and last"},
    ]}
    f = extract_features(body, {})
    assert f.last_user_text == "second and last"


def test_last_user_text_empty_when_no_user_message():
    f = extract_features({"messages": [{"role": "system", "content": "sys"}]}, {})
    assert f.last_user_text == ""


def test_last_user_text_defaults_on_direct_construction():
    from app.routing.features import RequestFeatures
    f = RequestFeatures(input_tokens=0, has_tools=False, system_prompt="", headers={})
    assert f.last_user_text == ""
```

- [ ] **Step 2: Run to verify failure** — `pytest tests/test_features.py -k last_user -v` → FAIL (`RequestFeatures` has no `last_user_text`).

- [ ] **Step 3: Edit `app/routing/features.py`** — add the defaulted field:

```python
@dataclass
class RequestFeatures:
    input_tokens: int
    has_tools: bool
    system_prompt: str
    headers: dict[str, str]
    last_user_text: str = ""
```

In `extract_features`, track the last user message inside the existing loop and pass it through:

```python
    messages = body.get("messages") or []
    all_text: list[str] = []
    system_text: list[str] = []
    last_user_text = ""
    for message in messages:
        if not isinstance(message, dict):
            continue
        text = _content_text(message.get("content"))
        all_text.append(text)
        role = message.get("role")
        if role == "system":
            system_text.append(text)
        elif role == "user":
            last_user_text = text

    has_tools = bool(body.get("tools")) or bool(body.get("functions"))
    return RequestFeatures(
        input_tokens=estimate_tokens("".join(all_text)),
        has_tools=has_tools,
        system_prompt="\n".join(system_text),
        headers={k.lower(): v for k, v in headers.items()},
        last_user_text=last_user_text,
    )
```

- [ ] **Step 4: Run to verify pass** — `pytest tests/test_features.py -v` → PASS.

- [ ] **Step 5: Commit**

```bash
git add app/routing/features.py tests/test_features.py
git commit -m "capture last user message text in request features"
```

---

## Task 3: `Classifier` — truncated probe, hard timeout, strict parse, degrade to fallback

**Milestone:** M4.

**Files:**
- Create: `app/routing/classifier.py`, `tests/test_classifier.py`

**Interfaces:**
- Consumes: `RequestFeatures` (Task 2); an injected `ClassifierCaller` (an `async` callable `dict -> str`); `asyncio`.
- Produces:
  - `app.routing.classifier.ClassifierDecision` dataclass: `pool: str`, `reason: str`.
  - `app.routing.classifier.ClassifierCaller = Callable[[dict], Awaitable[str]]` (type alias).
  - `app.routing.classifier.Classifier(caller: ClassifierCaller, labels: list[str], fallback_pool: str, timeout_s: float = 5.0, max_probe_chars: int = 2000, max_output_tokens: int = 8)`.
  - `async classify(features: RequestFeatures) -> ClassifierDecision` — builds a truncated probe, awaits `caller(probe)` under `asyncio.wait_for(timeout_s)`, strictly parses the result, and returns a decision. Never raises. Reasons: `classifier:label=<label>` / `classifier:timeout-><fallback_pool>` / `classifier:error-><fallback_pool>` / `classifier:garbage-><fallback_pool>`.

- [ ] **Step 1: Write the failing test** `tests/test_classifier.py`:

```python
import asyncio

from app.routing.classifier import Classifier, ClassifierDecision
from app.routing.features import RequestFeatures

LABELS = ["cheap", "default"]


def _features(system="", last_user=""):
    return RequestFeatures(input_tokens=0, has_tools=False, system_prompt=system,
                           headers={}, last_user_text=last_user)


def _classifier(caller, timeout_s=5.0, max_probe_chars=2000, max_output_tokens=8):
    return Classifier(caller=caller, labels=LABELS, fallback_pool="default",
                      timeout_s=timeout_s, max_probe_chars=max_probe_chars,
                      max_output_tokens=max_output_tokens)


async def test_valid_label_returns_that_pool():
    async def caller(probe):
        return "default"
    d = await _classifier(caller).classify(_features(last_user="hi"))
    assert d == ClassifierDecision(pool="default", reason="classifier:label=default")


async def test_label_is_stripped():
    async def caller(probe):
        return "  cheap\n"
    d = await _classifier(caller).classify(_features(last_user="hi"))
    assert d == ClassifierDecision(pool="cheap", reason="classifier:label=cheap")


async def test_timeout_degrades_to_fallback():
    async def caller(probe):
        raise asyncio.TimeoutError
    d = await _classifier(caller).classify(_features(last_user="hi"))
    assert d == ClassifierDecision(pool="default", reason="classifier:timeout->default")


async def test_error_degrades_to_fallback():
    async def caller(probe):
        raise RuntimeError("boom")
    d = await _classifier(caller).classify(_features(last_user="hi"))
    assert d == ClassifierDecision(pool="default", reason="classifier:error->default")


async def test_garbage_label_degrades_to_fallback():
    async def caller(probe):
        return "banana"
    d = await _classifier(caller).classify(_features(last_user="hi"))
    assert d == ClassifierDecision(pool="default", reason="classifier:garbage->default")


async def test_empty_output_degrades_to_fallback():
    async def caller(probe):
        return ""
    d = await _classifier(caller).classify(_features(last_user="hi"))
    assert d.pool == "default" and d.reason == "classifier:garbage->default"


async def test_hard_timeout_bounds_slow_caller():
    async def caller(probe):
        await asyncio.sleep(5)
        return "cheap"
    loop = asyncio.get_event_loop()
    start = loop.time()
    d = await _classifier(caller, timeout_s=0.05).classify(_features(last_user="hi"))
    elapsed = loop.time() - start
    assert d.reason == "classifier:timeout->default"
    assert elapsed < 1.0            # wait_for cancelled the slow call promptly


async def test_probe_is_truncated_and_constrained():
    captured = {}

    async def caller(probe):
        captured["probe"] = probe
        return "default"

    big_sys = "S" * 5000
    big_user = "U" * 5000
    await _classifier(caller, max_probe_chars=2000, max_output_tokens=8).classify(
        _features(system=big_sys, last_user=big_user))

    probe = captured["probe"]
    roles = [m["role"] for m in probe["messages"]]
    assert roles == ["system", "user"]                      # exactly one of each
    assert probe["max_tokens"] == 8                          # constrained output
    user_content = probe["messages"][1]["content"]
    assert user_content.count("S") == 2000                  # system slice truncated
    assert user_content.count("U") == 2000                  # user slice truncated


async def test_only_last_user_message_informs_decision():
    # earlier turns are excluded: only system + last user text reach the probe
    captured = {}

    async def caller(probe):
        captured["probe"] = probe
        return "cheap"

    await _classifier(caller).classify(_features(system="sys", last_user="the-last-ask"))
    content = captured["probe"]["messages"][1]["content"]
    assert "the-last-ask" in content and "sys" in content
```

- [ ] **Step 2: Run to verify failure** — `pytest tests/test_classifier.py -v` → FAIL (module missing).

- [ ] **Step 3: Write `app/routing/classifier.py`**

```python
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Awaitable, Callable

from app.routing.features import RequestFeatures

# A ClassifierCaller runs the probe request and returns the model's raw text
# output. It MUST raise on any failure so the Classifier can degrade cleanly.
ClassifierCaller = Callable[[dict], Awaitable[str]]


@dataclass
class ClassifierDecision:
    pool: str
    reason: str


class Classifier:
    """Stage-2 router: a cheap model returns a pool label for requests the
    stage-1 rules did not resolve. Consulted only on no-match. Never raises;
    any timeout/error/garbage degrades to `fallback_pool`."""

    def __init__(
        self,
        caller: ClassifierCaller,
        labels: list[str],
        fallback_pool: str,
        timeout_s: float = 5.0,
        max_probe_chars: int = 2000,
        max_output_tokens: int = 8,
    ) -> None:
        self._caller = caller
        self._labels = labels
        self._fallback_pool = fallback_pool
        self._timeout_s = timeout_s
        self._max_probe_chars = max_probe_chars
        self._max_output_tokens = max_output_tokens

    async def classify(self, features: RequestFeatures) -> ClassifierDecision:
        probe = self._build_probe(features)
        try:
            raw = await asyncio.wait_for(self._caller(probe), self._timeout_s)
        except asyncio.TimeoutError:
            return self._degrade("timeout")
        except Exception:
            return self._degrade("error")

        label = self._parse(raw)
        if label is None:
            return self._degrade("garbage")
        return ClassifierDecision(pool=label, reason=f"classifier:label={label}")

    def _degrade(self, kind: str) -> ClassifierDecision:
        return ClassifierDecision(
            pool=self._fallback_pool,
            reason=f"classifier:{kind}->{self._fallback_pool}",
        )

    def _parse(self, raw: object) -> str | None:
        if not isinstance(raw, str):
            return None
        candidate = raw.strip()
        return candidate if candidate in self._labels else None

    def _build_probe(self, features: RequestFeatures) -> dict:
        system_slice = features.system_prompt[: self._max_probe_chars]
        user_slice = features.last_user_text[: self._max_probe_chars]
        labels = ", ".join(self._labels)
        instruction = (
            "You are a routing classifier. Choose the single best label for the "
            "request below. Reply with EXACTLY one of these labels and nothing "
            f"else: {labels}."
        )
        view = ""
        if system_slice:
            view += f"System prompt:\n{system_slice}\n\n"
        view += f"User message:\n{user_slice}"
        return {
            "model": "__classifier__",   # rewritten per pool entry by the adapter
            "messages": [
                {"role": "system", "content": instruction},
                {"role": "user", "content": view},
            ],
            "max_tokens": self._max_output_tokens,
            "temperature": 0,
        }
```

- [ ] **Step 4: Run to verify pass** — `pytest tests/test_classifier.py -v` → PASS.

- [ ] **Step 5: Commit**

```bash
git add app/routing/classifier.py tests/test_classifier.py
git commit -m "add stage-2 Classifier with hard timeout and fallback degrade"
```

---

## Task 4: `ExecutorProbeCaller` — run the probe through the M3 executor

**Milestone:** M4.

**Files:**
- Create: `app/routing/classifier_caller.py`, `tests/test_classifier_caller.py`

**Interfaces:**
- Consumes: `FallbackExecutor` / `ExecutionOutcome` (M3), `PoolResolver` (M2), `Resolution` (existing `app/routing/router.py`), `ProviderResponse` (existing).
- Produces:
  - `app.routing.classifier_caller.ExecutorProbeCaller(executor: FallbackExecutor, pool_resolver: PoolResolver, pool: str)` — a `ClassifierCaller`.
  - `async __call__(probe: dict) -> str` — builds a `Resolution` for `pool` (first entry), runs `executor.execute(probe, resolution)`, and returns `outcome.response.body["choices"][0]["message"]["content"]`. Raises `RuntimeError` when the outcome has no response (probe pool exhausted); lets `KeyError`/`TypeError`/`IndexError` propagate on a malformed body. Never records a request row (delegates only to the executor, which does not persist).

Note on imports: this module imports `FallbackExecutor`/`Resolution`; `Router` imports `Classifier` (Task 3) which imports **only** `features`. So `Router → Classifier → features` and `classifier_caller → executor → router` form no cycle (nothing imports `classifier_caller` except `main.py`).

- [ ] **Step 1: Write the failing test** `tests/test_classifier_caller.py`:

```python
import pytest

from app.core.config import PoolEntry
from app.providers.base import ProviderError, ProviderResponse
from app.routing.classifier_caller import ExecutorProbeCaller
from app.routing.executor import ExecutionOutcome

POOLS_FIRST = {"cheap": [PoolEntry(provider="deepseek", model="deepseek-chat")]}


class FakePoolResolver:
    def first_entry(self, name):
        return POOLS_FIRST[name][0]


class FakeExecutor:
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


async def test_extracts_assistant_text_and_uses_configured_pool():
    resp = ProviderResponse(200, {"choices": [{"message": {"content": "cheap"}}]}, 1, 1)
    ex = FakeExecutor(_outcome(response=resp))
    caller = ExecutorProbeCaller(ex, FakePoolResolver(), "cheap")
    out = await caller({"model": "__classifier__", "messages": []})
    assert out == "cheap"
    assert ex.calls[0][1].pool == "cheap"          # Resolution targeted the probe pool


async def test_raises_when_pool_exhausted():
    err = ProviderError("down", 503, "deepseek", retryable=True)
    ex = FakeExecutor(_outcome(response=None, error=err))
    caller = ExecutorProbeCaller(ex, FakePoolResolver(), "cheap")
    with pytest.raises(RuntimeError):
        await caller({"messages": []})


async def test_raises_on_malformed_body():
    resp = ProviderResponse(200, {"choices": []}, 1, 1)   # no message
    ex = FakeExecutor(_outcome(response=resp))
    caller = ExecutorProbeCaller(ex, FakePoolResolver(), "cheap")
    with pytest.raises((KeyError, IndexError, TypeError)):
        await caller({"messages": []})
```

- [ ] **Step 2: Run to verify failure** — `pytest tests/test_classifier_caller.py -v` → FAIL (module missing).

- [ ] **Step 3: Write `app/routing/classifier_caller.py`**

```python
from __future__ import annotations

from app.routing.executor import FallbackExecutor
from app.routing.pools import PoolResolver
from app.routing.router import Resolution


class ExecutorProbeCaller:
    """Production ClassifierCaller: runs the classifier probe against its
    configured cheap pool via the shared M3 FallbackExecutor and returns the
    model's raw text. Raises on any failure so the Classifier degrades to its
    fallback_pool. Does not persist anything (the executor never writes rows)."""

    def __init__(
        self, executor: FallbackExecutor, pool_resolver: PoolResolver, pool: str
    ) -> None:
        self._executor = executor
        self._pools = pool_resolver
        self._pool = pool

    async def __call__(self, probe: dict) -> str:
        entry = self._pools.first_entry(self._pool)
        resolution = Resolution(
            pool=self._pool, provider=entry.provider, model=entry.model,
            route_stage="classifier", route_reason="classifier-probe",
        )
        outcome = await self._executor.execute(probe, resolution)
        if outcome.response is None:
            raise RuntimeError("classifier probe failed (pool exhausted)")
        return outcome.response.body["choices"][0]["message"]["content"]
```

- [ ] **Step 4: Run to verify pass** — `pytest tests/test_classifier_caller.py -v` → PASS.

- [ ] **Step 5: Commit**

```bash
git add app/routing/classifier_caller.py tests/test_classifier_caller.py
git commit -m "add ExecutorProbeCaller running the classifier probe via the fallback executor"
```

---

## Task 5: Make `Router.route` async and consult the classifier on no-match

**Milestone:** M4.

**Files:**
- Modify: `app/routing/router.py`, `app/routing/gateway.py`
- Modify (test): `tests/test_router.py`, `tests/test_acceptance_m2.py`

**Interfaces:**
- Consumes: `RuleEngine`, `PoolResolver`, `extract_features` (existing); `Classifier`/`ClassifierDecision` (Task 3).
- Produces:
  - `app.routing.router.Router(rule_engine: RuleEngine, pool_resolver: PoolResolver, classifier: Classifier | None = None)`.
  - `async route(body: dict, headers: dict[str, str]) -> Resolution` — stage-1 rules first (unchanged). On no-match: if `classifier is not None`, `await classifier.classify(features)` and build a `Resolution` with `route_stage="classifier"` and the decision's reason (guarding an unknown returned pool to `default_pool`); otherwise `default_pool` with `route_stage="default"`, `route_reason="no-rule-matched"` (unchanged).
  - `GatewayService.complete` now `await`s `self._router.route(...)`. `_record` is unchanged.

- [ ] **Step 1: Rewrite `tests/test_router.py`** — convert to async, add classifier-consultation tests. Full file:

```python
import pytest

from app.core.config import PoolEntry, RuleConfig, RuleWhen
from app.routing.classifier import ClassifierDecision
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


class FakeClassifier:
    """Records whether it was consulted and returns a fixed decision."""

    def __init__(self, decision):
        self.decision = decision
        self.calls = 0

    async def classify(self, features):
        self.calls += 1
        return self.decision


def _router(classifier=None):
    return Router(RuleEngine(RULES), PoolResolver(POOLS, "default"), classifier)


async def test_short_request_routes_to_cheap_via_rule():
    r = await _router().route({"messages": [{"role": "user", "content": "hi"}]}, {})
    assert r == Resolution(pool="cheap", provider="deepseek", model="deepseek-chat",
                           route_stage="rule", route_reason="rule:short-and-simple")


async def test_header_hint_wins():
    r = await _router().route({"messages": [{"role": "user", "content": "hi"}]},
                              {"x-pool": "cheap"})
    assert r.pool == "cheap" and r.route_stage == "rule"
    assert r.route_reason == "rule:explicit-hint"


async def test_unknown_hint_degrades_to_default():
    r = await _router().route({"messages": [{"role": "user", "content": "hi"}]},
                              {"x-pool": "bogus"})
    assert r.pool == "default" and r.provider == "anthropic"
    assert r.route_stage == "default"
    assert "unknown-pool" in r.route_reason and "bogus" in r.route_reason


async def test_no_match_routes_to_default_when_classifier_absent():
    long_text = "x" * 40000       # ~10000 tokens: above 500, below 20000 -> no rule
    r = await _router().route({"messages": [{"role": "user", "content": long_text}]}, {})
    assert r.pool == "default"
    assert r.route_stage == "default" and r.route_reason == "no-rule-matched"


async def test_empty_rules_all_default():
    r = await Router(RuleEngine([]), PoolResolver(POOLS, "default")).route(
        {"messages": []}, {})
    assert r.pool == "default" and r.route_reason == "no-rule-matched"


async def test_no_match_consults_classifier_when_present():
    clf = FakeClassifier(ClassifierDecision(pool="cheap", reason="classifier:label=cheap"))
    long_text = "x" * 40000       # no rule matches
    r = await _router(clf).route({"messages": [{"role": "user", "content": long_text}]}, {})
    assert clf.calls == 1
    assert r.pool == "cheap" and r.provider == "deepseek"
    assert r.route_stage == "classifier"
    assert r.route_reason == "classifier:label=cheap"


async def test_rule_match_never_consults_classifier():
    clf = FakeClassifier(ClassifierDecision(pool="cheap", reason="classifier:label=cheap"))
    # short, tool-less -> matches short-and-simple; classifier must NOT run
    r = await _router(clf).route({"messages": [{"role": "user", "content": "hi"}]}, {})
    assert clf.calls == 0
    assert r.route_stage == "rule" and r.route_reason == "rule:short-and-simple"


async def test_classifier_fallback_pool_decision_recorded():
    clf = FakeClassifier(ClassifierDecision(pool="default", reason="classifier:timeout->default"))
    long_text = "x" * 40000
    r = await _router(clf).route({"messages": [{"role": "user", "content": long_text}]}, {})
    assert r.pool == "default" and r.route_stage == "classifier"
    assert r.route_reason == "classifier:timeout->default"


async def test_classifier_unknown_pool_guards_to_default():
    clf = FakeClassifier(ClassifierDecision(pool="ghost", reason="classifier:label=ghost"))
    long_text = "x" * 40000
    r = await _router(clf).route({"messages": [{"role": "user", "content": long_text}]}, {})
    assert r.pool == "default" and r.route_stage == "default"
    assert "unknown-pool" in r.route_reason and "ghost" in r.route_reason
```

- [ ] **Step 2: Run to verify failure** — `pytest tests/test_router.py -v` → FAIL (`route` is sync / no classifier param / consultation absent).

- [ ] **Step 3: Rewrite `app/routing/router.py`**

```python
from __future__ import annotations

from dataclasses import dataclass

from app.routing.classifier import Classifier
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
    def __init__(
        self,
        rule_engine: RuleEngine,
        pool_resolver: PoolResolver,
        classifier: Classifier | None = None,
    ) -> None:
        self._rules = rule_engine
        self._pools = pool_resolver
        self._classifier = classifier

    async def route(self, body: dict, headers: dict[str, str]) -> Resolution:
        features = extract_features(body, headers)
        match = self._rules.match(features)

        if match is not None and self._pools.has_pool(match.pool):
            pool, stage, reason = match.pool, "rule", f"rule:{match.rule_name}"
        elif match is not None:
            pool, stage = self._pools.default_pool, "default"
            reason = f"rule:{match.rule_name}:unknown-pool:{match.pool}"
        elif self._classifier is not None:
            decision = await self._classifier.classify(features)
            if self._pools.has_pool(decision.pool):
                pool, stage, reason = decision.pool, "classifier", decision.reason
            else:
                pool, stage = self._pools.default_pool, "default"
                reason = f"{decision.reason}:unknown-pool:{decision.pool}"
        else:
            pool, stage, reason = self._pools.default_pool, "default", "no-rule-matched"

        entry = self._pools.first_entry(pool)
        return Resolution(
            pool=pool, provider=entry.provider, model=entry.model,
            route_stage=stage, route_reason=reason,
        )
```

- [ ] **Step 4: Edit `app/routing/gateway.py`** — in `complete`, await the router:

```python
        resolution = await self._router.route(body, headers)
```

(No other change; `_record` already composes fallback vs non-fallback reasons generically.)

- [ ] **Step 5: Edit `tests/test_acceptance_m2.py`** — in `test_ac_r10_routing_pure_and_never_raises`, change the loop call to await (the test is already `async def`):

```python
    for b in [{}, {"messages": None}, {"messages": [1, 2, 3]},
              {"tools": "notalist"}, {"messages": [{"role": "system"}]}]:
        res = await router.route(b, {"x-pool": ""})
        assert res.pool in pools
```

- [ ] **Step 6: Run to verify pass** — `pytest tests/test_router.py tests/test_acceptance_m2.py tests/test_gateway.py -v` → PASS.

- [ ] **Step 7: Commit**

```bash
git add app/routing/router.py app/routing/gateway.py tests/test_router.py tests/test_acceptance_m2.py
git commit -m "make Router.route async and consult classifier on stage-1 no-match"
```

---

## Task 6: Wire the classifier into the app + end-to-end endpoint tests

**Milestone:** M4.

**Files:**
- Modify: `app/main.py`
- Create (test): `tests/test_classifier_endpoint.py`

**Interfaces:**
- Consumes: `Classifier` (Task 3), `ExecutorProbeCaller` (Task 4), existing `FallbackExecutor`, `HealthTracker`, `PoolResolver`, `Router`, `RuleEngine`, `build_registry`.
- Produces: `create_app` builds the `FallbackExecutor` first, then — **only when `config.classifier.enabled`** — an `ExecutorProbeCaller` + `Classifier`, then the `Router` with that classifier (or `None`). No public signature change to `create_app`.

- [ ] **Step 1: Edit `app/main.py`** — add imports:

```python
from app.routing.classifier import Classifier
from app.routing.classifier_caller import ExecutorProbeCaller
```

Replace the routing/executor construction inside `lifespan` so the executor is built before the classifier and the router last:

```python
        registry = build_registry(config, http_client)
        rule_engine = RuleEngine(config.rules)
        pool_resolver = PoolResolver(config.pools, config.default_pool)
        health = HealthTracker(cooldown_seconds=config.cooldown_seconds)
        executor = FallbackExecutor(registry, pool_resolver, health)

        classifier = None
        if config.classifier.enabled:
            caller = ExecutorProbeCaller(executor, pool_resolver, config.classifier.pool)
            classifier = Classifier(
                caller=caller,
                labels=config.classifier.labels,
                fallback_pool=config.classifier.fallback_pool,
                timeout_s=config.classifier.timeout_s,
                max_probe_chars=config.classifier.max_probe_chars,
                max_output_tokens=config.classifier.max_output_tokens,
            )
        router = Router(rule_engine, pool_resolver, classifier)

        cost = CostTracker(config.prices)
        repo = RequestsRepository(db.connection)
        app.state.config = config
        app.state.db = db
        app.state.http_client = http_client
        app.state.gateway = GatewayService(router, executor, cost, repo)
```

- [ ] **Step 2: Write `tests/test_classifier_endpoint.py`** — end-to-end classifier routing via the chat endpoint. A mid-size, tool-less request (~1000 tokens) matches no rule and reaches stage 2.

```python
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
```

- [ ] **Step 3: Run to verify pass** — `pytest tests/test_classifier_endpoint.py -v` → PASS.

- [ ] **Step 4: Run the full suite** — `pytest -v` → all PASS. Confirm the ripple set (`test_chat_endpoint.py`, `test_app_integration.py`, `test_acceptance_m1.py`, `test_acceptance_m2.py`, `test_acceptance_m3.py`, `test_fallback_endpoint.py`) is green.

- [ ] **Step 5: Commit**

```bash
git add app/main.py tests/test_classifier_endpoint.py
git commit -m "wire classifier into the app and route stage-1 no-match through it"
```

---

## Task 7: Independent M4 acceptance tests

**Milestone:** M4.

**Files:**
- Create: `tests/test_acceptance_m4.py`

**Interfaces:**
- Consumes: the public HTTP surface + `Classifier`/`ExecutorProbeCaller`/config behavior. Derived from the Acceptance Criteria above, NOT from the implementation. Reuse the `make_app` factory pattern and helpers from Task 6 by copying them into this file so the acceptance suite is self-contained.

- [ ] **Step 1: Write `tests/test_acceptance_m4.py`** — one test per criterion:
  - **M4-AC-CFG1** — `load_config` on a classifier-less YAML → `cfg.classifier.enabled is False`; an app built from it routes a ~1000-token no-rule-match request to `("default", "default", "no-rule-matched")`.
  - **M4-AC-CFG2** — `load_config(config.example.yaml)` (set the three example env vars) → `enabled is True`, `pool == "cheap"`, `labels == ["cheap", "default"]`, `fallback_pool == "default"`.
  - **M4-AC-CFG3** — parametrize four bad enabled-classifier YAMLs (unknown `pool`, unknown `fallback_pool`, unknown label, empty labels); each raises `ValueError`. Plus one disabled config referencing a bogus pool that loads without error.
  - **M4-AC-R1** — probe returns `"default"`; row `("default", "anthropic", "claude-sonnet-5", "classifier", "classifier:label=default", 200)`.
  - **M4-AC-R2** — short request matches a rule; assert deepseek called exactly once (no probe) and row `("rule", "rule:short-and-simple")`.
  - **M4-AC-R3** — probe returns `"banana"`; served by anthropic (200); row `("default", "classifier", "classifier:garbage->default")`.
  - **M4-AC-R4** — probe upstream 500 (cheap pool exhausts); served by anthropic (200); row `("default", "classifier", "classifier:error->default")`.
  - **M4-AC-R5** — classifier unit assertion: caller raising `asyncio.TimeoutError` → `ClassifierDecision("default", "classifier:timeout->default")` (endpoint reproduction not required).
  - **M4-AC-R6** — disabled classifier: ~1000-token request → `("default", "default", "no-rule-matched")`, no probe (deepseek not called).
  - **M4-AC-R7** — two-entry `default` pool; probe → `"default"`; first entry 503, second 200; row `route_stage="fallback"`, `route_reason` startswith `"classifier:label=default"` and contains `"fallback:"`+`"hops=1"`, `fallback_from` = first entry id.
  - **M4-AC-R8** — parametrize the three degrade endpoint scenarios (garbage / probe-500 / — timeout covered by unit); each returns client `200` from the fallback pool.
  - **M4-AC-U1..U6** — direct `Classifier` unit assertions with fake callers (label, timeout, error, garbage, bounded `wait_for`, truncated/constrained probe with exactly one system + one user message).
  - **M4-AC-P1..P3** — direct `ExecutorProbeCaller` unit assertions with a fake executor (extract, raise-on-exhaustion, raise-on-malformed).
  - **M4-AC-X1** — a classifier-routed request writes exactly one row (`SELECT count(*)`), and — capturing the `"gateway"` logger as in `test_acceptance_m2::test_ac_r9` — exactly one `event == "request"` log line whose `route_stage == "classifier"`.
  - **M4-AC-X2** — assert by running the whole suite in Step 2 (documented, not a code assertion).

- [ ] **Step 2: Run** — `pytest tests/test_acceptance_m4.py -v` → all PASS, then `pytest -v` → entire suite PASS.

- [ ] **Step 3: Commit**

```bash
git add tests/test_acceptance_m4.py
git commit -m "add independent M4 classifier acceptance tests"
```

---

## Self-Review (completed by planner)

- **Spec coverage:** (1) Two-stage, cheapest-first; classifier only on no-match → Task 5 (`Router.route`), M4-AC-R1/R2/R6. (2) Cheap model, truncated view, labels are pools, strict parse → Task 3 (`Classifier._build_probe`/`_parse`), M4-AC-U1/U4/U6. (3) Hint never a hard dependency; hard timeout; timeout/error/garbage → `fallback_pool`; reuses M3 provider path but reduces to a label → Task 3 + Task 4 (`ExecutorProbeCaller`), M4-AC-U2/U3/U5, R3/R4/R8, P1/P2/P3. (4) Route capture `route_stage="classifier"` + reason; orthogonal to fallback hop → Task 5 + unchanged `GatewayService._record`, M4-AC-R1/R7. (5) Config block + example + `enabled:false` preserves M2/M3 → Task 1, M4-AC-CFG1/CFG2/CFG3/R6.
- **Reuses existing seams:** `FallbackExecutor` (M3) for the probe, `HealthTracker` shared so probe failures cool models for real traffic, `RequestFeatures` extended additively, `route_stage` free-text column (no migration), and the unchanged `_record` fallback-composition (classifier origin preserved in `route_reason`). No new dependency, no schema change.
- **Import-cycle check:** `Router → Classifier → features` (Classifier imports nothing from `executor`/`router`). `classifier_caller → executor → router` and `classifier_caller → router`; nothing imports `classifier_caller` except `main.py`. No cycle.
- **Type consistency:** `Classifier(caller, labels, fallback_pool, timeout_s, max_probe_chars, max_output_tokens)`, `ClassifierDecision(pool, reason)`, `classify(features) -> ClassifierDecision`, `ExecutorProbeCaller(executor, pool_resolver, pool)` with `__call__(probe) -> str`, `Router(rule_engine, pool_resolver, classifier=None)` with `async route(body, headers)`, and `ClassifierConfig(...)` fields are used identically across producing/consuming tasks and tests.
- **Invariant checks:** rules always first (classifier consulted only in the no-match branch); classifier never raises (all exceptions/timeout caught → degrade); exactly one row + one log line per client request (probe goes through the executor which does not persist; verified M4-AC-X1); OpenAI compatibility unchanged (probe body internal; client body still only rewrites `model`); constant-time auth unchanged; no caching added.
- **Ripple is explicit and minimal:** only `test_router.py` (async conversion) and one mechanical `await` in `test_acceptance_m2::test_ac_r10` change in prior test files; all other suites use fixtures with the classifier disabled and are verified green in Tasks 6–7. `test_features.py` unaffected (new field defaulted).
- **Placeholder scan:** every code step contains concrete code; Task 7 enumerates each acceptance test against a named criterion. No TBD/TODO.

---

## Open Questions

- **Non-blocking (decision #1).** *Truncation strategy.* Defaulted to a character-truncated slice of the **system prompt + last user message**, `max_probe_chars` (2000) each, config-tunable. If real traffic shows the classifier misreading long multi-turn requests, widen the cap or include more turns — a localized change in `Classifier._build_probe`. Flagged for awareness; not blocking.
- **Non-blocking (decision #6).** *The classifier probe's token cost is not recorded.* V1 records one row per client-facing request; the probe is an internal meta-call, so its (small, cheap-model) cost is currently unaccounted in the ledger. If the owner wants probe spend visible, M5 (dashboard) is the natural place to add a separate probe-cost column or a second row type. Called out so the dashboard's spend total is understood to exclude probe cost in V1.
- **Owner-facing but safely defaulted (decision #4).** *Whether a fallback hop should overwrite `route_stage="classifier"` with `"fallback"`.* This plan keeps the M3 rule (yes, with the classifier origin preserved inside `route_reason`), because the domain invariant and schema enumerate `fallback` as a `route_stage` value. If the owner later prefers `route_stage` to always denote the *selection* stage (so a classifier-selected pool that hops still reads `"classifier"`), switch to relying on the `fallback_from` column — a single localized change in `GatewayService._record`. Not blocking.
