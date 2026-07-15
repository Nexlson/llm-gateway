# M1 — Passthrough Proxy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build an OpenAI-compatible passthrough proxy that forwards `POST /v1/chat/completions` to a provider unchanged (except the `model` field), exposes configured pools as models at `GET /v1/models`, guards everything with a static bearer key, and logs one SQLite row plus one JSON stdout line per request.

**Architecture:** FastAPI app assembled by a `create_app()` factory with an async lifespan that loads `config.yaml` once, opens a single WAL-mode SQLite connection, builds provider adapters, and wires a `GatewayService`. Layering follows `api/` handlers → `routing/` + `cost/` services → `repositories/` data access, with shared Pydantic/dataclass types in `schemas/`. Every collaborator is constructor-injected so units test in isolation. M1 has no router/classifier/fallback yet: a minimal `PoolResolver` picks the first entry of the requested pool (or the configured `default_pool`), which is the exact seam M2/M3/M4 extend without rework.

**Tech Stack:** Python 3.11+, FastAPI, Uvicorn, httpx (async), aiosqlite, PyYAML, Pydantic v2. Tests: pytest, pytest-asyncio, respx (httpx mocking), httpx.AsyncClient.

## Global Constraints

- **OpenAI compatibility:** Request and response bodies pass through **unchanged except the `model` field**. **Unknown fields are forwarded, never rejected.** Never coerce the body through a strict schema that drops fields — forward the raw parsed JSON.
- **Error shape:** All errors (auth, bad request, upstream failure) return OpenAI's error envelope: `{"error": {"message": <str>, "type": <str>, "param": <str|null>, "code": <str|null>}}`.
- **Auth:** Single static API key via `Authorization: Bearer <key>`, compared with `secrets.compare_digest` (constant-time). Same key guards `/v1/*` and `/dashboard`.
- **Storage:** SQLite in **WAL mode**. `requests` table matches the architecture schema sketch exactly; index on `ts`. **Cost is computed at write time** from the config price table and stored on the row (never recomputed later).
- **Logging:** Structured **JSON to stdout**, one line per request, carrying request id, pool, model, route_stage, input/output tokens, cost, latency_ms, status, fallback_hops. **No prompt or completion bodies in logs.**
- **Config:** All tunables come from a mounted `config.yaml`, loaded **once at boot** in `core/config.py`. No hardcoded model names, prices, keys, or thresholds. Never commit real keys — commit `config.example.yaml` with placeholders; real `config.yaml` is gitignored.
- **Async all the way down:** No blocking I/O in handlers. Constructor-inject dependencies; no module-level stateful singletons.
- **Scope guard (do NOT build in M1):** YAML rule engine (M2), fallback chain + cooldown (M3), classifier (M4), dashboard UI (M5), Docker/VPS deploy (M6), and everything in the architecture "Deferred" table (multi-key auth, Prometheus/OTel, latency routing, streaming/SSE, live reload, semantic caching).
- **Tests never hit real provider APIs.** Mock all outbound HTTP with respx. Tests use a temp-file SQLite DB (not `:memory:`) so WAL applies.

---

## M1 Design Decisions (documented assumptions)

These resolve ambiguities the task flagged. They are chosen for minimal, forward-compatible M1 behavior. **All open questions have been confirmed by the human** — see decisions #8 (secrets from env vars) and #9 (Anthropic via OpenAI-compat); `route_stage='passthrough'` (#2) and the unchanged response body echoing the upstream model name (#5) are accepted as written.

1. **Provider selection (no router yet).** The incoming `model` field is treated as a **pool name**. `PoolResolver.resolve(model)` returns the **first entry** (`provider`, `model`) of that pool. If the requested name is not a known pool, it resolves to the configured `default_pool` (still first entry). This is exactly the ordered-list seam M3's fallback chain will iterate, so no rework.
2. **`route_stage` value for M1.** The schema comment lists `rule | classifier | fallback`, but M1 does no routing. We write the placeholder **`route_stage='passthrough'`** with `route_reason` like `m1-passthrough:pool=<name>` (or `m1-passthrough:default:model-not-a-pool` when falling back to `default_pool`). The column is `TEXT` (unconstrained), so this is schema-compatible; M2 replaces it with real values. (Confirmed by human.)
3. **Cost in M1.** `CostTracker` computes cost at write time from the config `prices` table (per-1M-token rates). The committed `config.example.yaml` ships an **empty `prices` map**, so every M1 row stores `cost_usd = 0.0`. When M2 populates prices, cost lands automatically with no code change. This honors the cost-at-write-time invariant early.
4. **Provider protocol.** M1 assumes every configured provider exposes an **OpenAI-compatible `POST {base_url}/chat/completions`** (true for OpenAI, DeepSeek, and Anthropic's OpenAI-compat endpoint). One `OpenAICompatibleAdapter` covers all providers. Native Anthropic Messages translation is out of M1 scope. (Confirmed by human.)
5. **Response body is returned unchanged**, so it carries the upstream real model name (e.g. `deepseek-chat`), not the pool name. This satisfies "response passes unchanged"; the pool→model mapping is recorded in the DB row and log line, not injected into the response.
6. **`/dashboard` in M1** is an auth-guarded HTML stub (`"Dashboard coming in M5"`). Full UI is M5.
7. **`GET /healthz`** (unauthenticated liveness, `{"status":"ok"}`) is added for local/CI smoke tests and the M6 reverse-proxy health check. Small, standard, not one of the 7 scope items but justified.
8. **Secrets from environment variables (human decision — confirmed).** No secret is stored in `config.yaml`. Every secret is referenced by **env-var name**: the gateway's own static bearer key via top-level `api_key_env`, and each provider's key via `providers.<name>.api_key_env`. `load_config` resolves each name from `os.environ` at boot and **fails fast (raises `ValueError`) if any referenced variable is unset or empty** — a misconfigured deploy is caught at boot, not on first request. The resolved secret is populated onto `AppConfig.api_key` / `ProviderConfig.api_key` so all downstream code (security dependency, provider adapter) reads `.api_key` unchanged. `config.example.yaml` ships only `api_key_env` placeholders, never a raw key.
9. **Anthropic via OpenAI-compat endpoint (human decision — confirmed).** Confirms decision #4: V1 reaches Anthropic through its OpenAI-compatible endpoint; no native Messages adapter in scope.

---

## File Structure

**Create (application):**
- `pyproject.toml` — deps, pytest config
- `config.example.yaml` — committed placeholder config
- `app/__init__.py`, `app/api/__init__.py`, `app/api/v1/__init__.py`, `app/api/v1/endpoints/__init__.py`, `app/core/__init__.py`, `app/routing/__init__.py`, `app/providers/__init__.py`, `app/cost/__init__.py`, `app/schemas/__init__.py`, `app/repositories/__init__.py` — package markers
- `app/core/config.py` — Pydantic config models + `load_config(path)`
- `app/core/logging.py` — JSON stdout formatter + `configure_logging()` + `log_request()`
- `app/core/database.py` — `Database` (WAL, schema creation, single async connection)
- `app/core/security.py` — constant-time bearer key dependency
- `app/core/errors.py` — `GatewayError` hierarchy + OpenAI error-shape helpers + exception handlers
- `app/repositories/requests_repo.py` — `RequestRecord` dataclass + `RequestsRepository.insert`
- `app/providers/base.py` — `ProviderAdapter` protocol + `ProviderResponse` dataclass
- `app/providers/openai_compatible.py` — `OpenAICompatibleAdapter`
- `app/providers/registry.py` — `build_registry(config, http_client)`
- `app/routing/pools.py` — `Resolution` dataclass + `PoolResolver`
- `app/cost/tracker.py` — `CostTracker`
- `app/routing/gateway.py` — `GatewayService` (orchestrates resolve → call → cost → persist → log)
- `app/schemas/openai.py` — `models_list_response()` helper + error envelope model
- `app/api/dependencies.py` — DI providers reading `app.state`
- `app/api/v1/endpoints/chat.py` — `POST /v1/chat/completions`
- `app/api/v1/endpoints/models.py` — `GET /v1/models`
- `app/api/v1/router.py` — aggregates v1 endpoint routers
- `app/api/dashboard.py` — `GET /dashboard` stub
- `app/main.py` — `create_app()` factory + lifespan + `app` instance

**Create (tests):**
- `tests/__init__.py`
- `tests/conftest.py` — fixtures: `test_config`, `app`, `client`
- `tests/test_config.py`, `tests/test_database.py`, `tests/test_logging.py`, `tests/test_security.py`, `tests/test_errors.py`, `tests/test_providers.py`, `tests/test_pools.py`, `tests/test_cost.py`, `tests/test_models_endpoint.py`, `tests/test_chat_endpoint.py`, `tests/test_dashboard.py`, `tests/test_app_integration.py`

**Modify:**
- `.gitignore` — add `config.yaml`, `*.db`, `*.db-wal`, `*.db-shm`, `__pycache__/`, `.pytest_cache/`, `*.egg-info/`

---

## Acceptance Criteria (for the tester — derivable without reading implementation)

**A1 — Auth (constant-time bearer).**
- `POST /v1/chat/completions`, `GET /v1/models`, and `GET /dashboard` with **no** `Authorization` header return **401** in OpenAI error shape (`body["error"]["type"] == "invalid_request_error"` or `"authentication_error"`, `message` non-empty).
- Wrong bearer key → **401**, OpenAI error shape.
- Correct bearer key → request proceeds (not 401).
- `GET /healthz` requires **no** auth and returns 200 `{"status":"ok"}`.

**A2 — `GET /v1/models` shape.**
- With valid auth, returns 200 with `{"object":"list","data":[...]}`.
- `data` contains exactly one entry per **pool name** in config (e.g. `cheap`, `default`, `large-context`). Each entry has `id` == pool name, `object` == `"model"`, and an integer `created`.

**A3 — Chat passthrough (happy path).**
- Given a mocked provider returning a valid OpenAI chat completion, `POST /v1/chat/completions` with body `{"model":"cheap","messages":[...],"temperature":0.2,"some_future_field":true}` returns **200** with the provider's response body **byte-for-content unchanged**.
- The **request forwarded upstream** has its `model` field rewritten to the resolved upstream model (first entry of the `cheap` pool, e.g. `deepseek-chat`), and **all other fields preserved**, including `temperature` and the unknown `some_future_field`.
- The upstream request goes to the resolved provider's `base_url` + `/chat/completions` with header `Authorization: Bearer <that provider's key>`.

**A4 — Unknown-field forwarding.**
- A request body containing a field not in the OpenAI schema is forwarded to the provider unchanged (never stripped, never 422).

**A5 — Default-pool fallback for unknown model.**
- A request with `model` that is **not** a configured pool name resolves to the configured `default_pool`; the upstream call uses that pool's first-entry provider/model. Response still 200.

**A6 — Request logging (SQLite).**
- After any completed request (success or upstream error), exactly **one row** is inserted into `requests`.
- Row columns match the schema sketch: `id` (non-empty text), `ts` (int, ~now), `pool`, `provider`, `model` (the resolved upstream model), `route_stage` == `"passthrough"`, `route_reason` (non-null), `input_tokens`/`output_tokens` (ints from provider `usage`, or 0 when absent), `cost_usd` (float, `0.0` under the example config's empty price table), `latency_ms` (int ≥ 0), `status` (HTTP status of the upstream result), `fallback_from` (NULL in M1).
- The DB opens in **WAL mode** (`PRAGMA journal_mode` returns `wal`).
- An index named `idx_requests_ts` exists on `requests(ts)`.

**A7 — Cost at write time.**
- With a non-empty price table entry for the resolved model, `cost_usd = input_tokens/1e6*input_rate + output_tokens/1e6*output_rate` (rates per 1M tokens). Unknown model → `0.0`. (Under example config, all rows are `0.0`.)

**A8 — Structured JSON log line.**
- Each request emits **exactly one** JSON line to stdout parseable as an object containing keys: `request_id`, `pool`, `model`, `route_stage`, `input_tokens`, `output_tokens`, `cost_usd`, `latency_ms`, `status`, `fallback_hops` (== 0 in M1).
- The log line contains **no** `messages`, prompt text, or completion text.

**A9 — Upstream error mapping.**
- When the mocked provider returns a 500 (retryable) or 400 (non-retryable), the client receives the error in **OpenAI error shape** with the upstream status code (M1 does not retry/fallback — that's M3).
- A row is still logged with `status` == the upstream status code.

**A10 — Missing `model` field.**
- `POST /v1/chat/completions` with a body lacking `model` returns **400** in OpenAI error shape (`type` == `"invalid_request_error"`, `param` == `"model"`). No upstream call is made.

**A11 — Config loading.**
- `load_config(path)` reads `config.example.yaml` into a typed object exposing `api_key`, `default_pool`, `providers`, `pools`, `prices`, `db_path`. Boot fails fast (raises) if `default_pool` is not a key in `pools`, or a pool entry references an unknown provider.

**A12 — Dashboard stub.**
- `GET /dashboard` with valid auth → 200, `text/html`, body mentions M5. Without auth → 401.

**A13 — Secrets from environment.**
- No secret appears in `config.yaml`/`config.example.yaml`; keys are referenced by env-var name (`api_key_env`, `providers.<name>.api_key_env`).
- With all referenced env vars set, `load_config` populates `cfg.api_key` and each `providers[...].api_key` from `os.environ`.
- If any referenced env var is unset or empty, `load_config` raises (boot fails fast) — no request is served with a missing secret.

---

## Task 1: Project scaffolding + config loader + example config

**Files:**
- Create: `pyproject.toml`, `config.example.yaml`, `app/__init__.py`, `app/core/__init__.py`, `app/core/config.py`, `tests/__init__.py`, `tests/test_config.py`
- Modify: `.gitignore`

**Interfaces:**
- Produces:
  - `app.core.config.ProviderConfig(base_url: str, api_key_env: str, api_key: str = "", timeout_s: float = 30.0)` — `api_key` is resolved from `os.environ[api_key_env]` at load time; never set in YAML.
  - `app.core.config.PoolEntry(provider: str, model: str)`
  - `app.core.config.PriceEntry(input_per_1m: float = 0.0, output_per_1m: float = 0.0)`
  - `app.core.config.AppConfig(api_key_env: str, api_key: str = "", db_path: str, default_pool: str, providers: dict[str, ProviderConfig], pools: dict[str, list[PoolEntry]], prices: dict[str, PriceEntry])` — `api_key` is resolved from `os.environ[api_key_env]` at load time.
  - `app.core.config.load_config(path: str | os.PathLike) -> AppConfig` — resolves all `*_env` secrets from `os.environ` and raises `ValueError` on invalid references **or any unset/empty referenced env var**.

- [ ] **Step 1: Write `pyproject.toml`**

```toml
[project]
name = "llm-gateway"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
    "fastapi>=0.110",
    "uvicorn[standard]>=0.29",
    "httpx>=0.27",
    "aiosqlite>=0.20",
    "pyyaml>=6.0",
    "pydantic>=2.6",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.0",
    "pytest-asyncio>=0.23",
    "respx>=0.21",
]

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]

[tool.setuptools.packages.find]
include = ["app*"]
```

- [ ] **Step 2: Update `.gitignore`** — append:

```
config.yaml
*.db
*.db-wal
*.db-shm
__pycache__/
.pytest_cache/
*.egg-info/
.venv/
```

- [ ] **Step 3: Write the failing test** `tests/test_config.py`:

```python
from pathlib import Path

import pytest

from app.core.config import load_config, AppConfig

EXAMPLE = Path(__file__).resolve().parents[1] / "config.example.yaml"

# Env vars the committed example references. Set them for every load test.
EXAMPLE_ENV = {
    "GATEWAY_API_KEY": "test-gateway-key",
    "DEEPSEEK_API_KEY": "test-deepseek-key",
    "ANTHROPIC_API_KEY": "test-anthropic-key",
}


def _set_env(monkeypatch, env: dict[str, str]) -> None:
    for k, v in env.items():
        monkeypatch.setenv(k, v)


def test_loads_example_config(monkeypatch):
    _set_env(monkeypatch, EXAMPLE_ENV)
    cfg = load_config(EXAMPLE)
    assert isinstance(cfg, AppConfig)
    # api_key is resolved from os.environ[api_key_env], never stored in YAML.
    assert cfg.api_key == "test-gateway-key"
    assert cfg.providers["deepseek"].api_key == "test-deepseek-key"
    assert cfg.default_pool in cfg.pools
    assert cfg.pools["default"][0].provider in cfg.providers
    assert cfg.prices == {}  # M1 example ships empty price table


def test_missing_referenced_env_var_fails_fast(monkeypatch):
    # Gateway key present, but a provider's referenced env var is unset -> boot fails.
    monkeypatch.setenv("GATEWAY_API_KEY", "test-gateway-key")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-deepseek-key")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(ValueError):
        load_config(EXAMPLE)


def test_rejects_default_pool_not_in_pools(tmp_path, monkeypatch):
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


def test_rejects_pool_entry_unknown_provider(tmp_path, monkeypatch):
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
```

- [ ] **Step 4: Run test to verify it fails** — `pytest tests/test_config.py -v` → FAIL (`ModuleNotFoundError: app.core.config`).

- [ ] **Step 5: Write `config.example.yaml`**

```yaml
# Committed example. Copy to config.yaml (gitignored) and set the referenced env vars.
# Secrets are NEVER stored here — each *_env value names an environment variable that
# load_config resolves from os.environ at boot (unset/empty -> boot fails fast).
api_key_env: "GATEWAY_API_KEY"   # gateway's own static bearer key
db_path: "gateway.db"
default_pool: "default"

providers:
  deepseek:
    base_url: "https://api.deepseek.com/v1"
    api_key_env: "DEEPSEEK_API_KEY"
    timeout_s: 30.0
  anthropic:
    base_url: "https://api.anthropic.com/v1"
    api_key_env: "ANTHROPIC_API_KEY"
    timeout_s: 30.0

pools:
  cheap:
    - { provider: deepseek, model: deepseek-chat }
    - { provider: anthropic, model: claude-haiku-4-5 }
  default:
    - { provider: anthropic, model: claude-sonnet-5 }
  large-context:
    - { provider: anthropic, model: claude-sonnet-5 }

# M1 ships an empty price table -> cost_usd stored as 0.0. M2 populates this.
prices: {}
```

- [ ] **Step 6: Write `app/core/config.py`**

```python
from __future__ import annotations

import os

import yaml
from pydantic import BaseModel, Field


class ProviderConfig(BaseModel):
    base_url: str
    api_key_env: str          # name of the env var holding this provider's key
    api_key: str = ""         # resolved from os.environ[api_key_env] at load time
    timeout_s: float = 30.0


class PoolEntry(BaseModel):
    provider: str
    model: str


class PriceEntry(BaseModel):
    input_per_1m: float = 0.0
    output_per_1m: float = 0.0


class AppConfig(BaseModel):
    api_key_env: str          # name of the env var holding the gateway's static key
    api_key: str = ""         # resolved from os.environ[api_key_env] at load time
    db_path: str = "gateway.db"
    default_pool: str
    providers: dict[str, ProviderConfig]
    pools: dict[str, list[PoolEntry]]
    prices: dict[str, PriceEntry] = Field(default_factory=dict)


def load_config(path: str | os.PathLike) -> AppConfig:
    with open(path, "r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    cfg = AppConfig(**raw)
    _resolve_secrets(cfg)
    _validate(cfg)
    return cfg


def _require_env(var: str) -> str:
    """Resolve a required secret from the environment; fail fast if unset/empty."""
    value = os.environ.get(var)
    if not value:
        raise ValueError(f"required environment variable '{var}' is unset or empty")
    return value


def _resolve_secrets(cfg: AppConfig) -> None:
    # No secret is stored in config.yaml — resolve each *_env name from os.environ.
    cfg.api_key = _require_env(cfg.api_key_env)
    for provider in cfg.providers.values():
        provider.api_key = _require_env(provider.api_key_env)


def _validate(cfg: AppConfig) -> None:
    if cfg.default_pool not in cfg.pools:
        raise ValueError(f"default_pool '{cfg.default_pool}' is not a defined pool")
    for pool_name, entries in cfg.pools.items():
        if not entries:
            raise ValueError(f"pool '{pool_name}' has no entries")
        for entry in entries:
            if entry.provider not in cfg.providers:
                raise ValueError(
                    f"pool '{pool_name}' references unknown provider '{entry.provider}'"
                )
```

- [ ] **Step 7: Create empty package markers** — `app/__init__.py`, `tests/__init__.py` (empty files).

- [ ] **Step 8: Run tests to verify they pass** — `pytest tests/test_config.py -v` → PASS.

- [ ] **Step 9: Commit**

```bash
git add pyproject.toml config.example.yaml .gitignore app/__init__.py app/core/__init__.py app/core/config.py tests/__init__.py tests/test_config.py
git commit -m "add config loader and example config"
```

---

## Task 2: SQLite database (WAL) + requests repository

**Milestone:** M1.

**Files:**
- Create: `app/core/database.py`, `app/repositories/__init__.py`, `app/repositories/requests_repo.py`, `tests/test_database.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `app.core.database.Database(path: str)`; async `connect() -> None` (opens aiosqlite conn, sets `PRAGMA journal_mode=WAL`, creates schema+index if absent); async `disconnect() -> None`; property `connection -> aiosqlite.Connection`.
  - `app.repositories.requests_repo.RequestRecord` dataclass with fields: `id: str, ts: int, pool: str, provider: str, model: str, route_stage: str, route_reason: str | None, input_tokens: int, output_tokens: int, cost_usd: float, latency_ms: int, status: int, fallback_from: str | None`.
  - `app.repositories.requests_repo.RequestsRepository(connection)`; async `insert(record: RequestRecord) -> None`.

- [ ] **Step 1: Write the failing test** `tests/test_database.py`:

```python
import time

from app.core.database import Database
from app.repositories.requests_repo import RequestRecord, RequestsRepository


async def test_wal_mode_and_index(tmp_path):
    db = Database(str(tmp_path / "g.db"))
    await db.connect()
    try:
        cur = await db.connection.execute("PRAGMA journal_mode")
        assert (await cur.fetchone())[0].lower() == "wal"
        cur = await db.connection.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_requests_ts'"
        )
        assert await cur.fetchone() is not None
    finally:
        await db.disconnect()


async def test_insert_row(tmp_path):
    db = Database(str(tmp_path / "g.db"))
    await db.connect()
    try:
        repo = RequestsRepository(db.connection)
        rec = RequestRecord(
            id="req-1", ts=int(time.time()), pool="cheap", provider="deepseek",
            model="deepseek-chat", route_stage="passthrough",
            route_reason="m1-passthrough:pool=cheap", input_tokens=10,
            output_tokens=5, cost_usd=0.0, latency_ms=42, status=200,
            fallback_from=None,
        )
        await repo.insert(rec)
        cur = await db.connection.execute(
            "SELECT pool, model, route_stage, status FROM requests WHERE id='req-1'"
        )
        row = await cur.fetchone()
        assert row == ("cheap", "deepseek-chat", "passthrough", 200)
    finally:
        await db.disconnect()
```

- [ ] **Step 2: Run test to verify it fails** — `pytest tests/test_database.py -v` → FAIL (module missing).

- [ ] **Step 3: Write `app/core/database.py`**

```python
from __future__ import annotations

import aiosqlite

_SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
  id            TEXT PRIMARY KEY,
  ts            INTEGER NOT NULL,
  pool          TEXT NOT NULL,
  provider      TEXT NOT NULL,
  model         TEXT NOT NULL,
  route_stage   TEXT NOT NULL,
  route_reason  TEXT,
  input_tokens  INTEGER NOT NULL,
  output_tokens INTEGER NOT NULL,
  cost_usd      REAL NOT NULL,
  latency_ms    INTEGER NOT NULL,
  status        INTEGER NOT NULL,
  fallback_from TEXT
);
CREATE INDEX IF NOT EXISTS idx_requests_ts ON requests(ts);
"""


class Database:
    def __init__(self, path: str) -> None:
        self._path = path
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self._conn = await aiosqlite.connect(self._path)
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.executescript(_SCHEMA)
        await self._conn.commit()

    async def disconnect(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def connection(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database not connected")
        return self._conn
```

- [ ] **Step 4: Write `app/repositories/requests_repo.py`**

```python
from __future__ import annotations

from dataclasses import dataclass

import aiosqlite


@dataclass
class RequestRecord:
    id: str
    ts: int
    pool: str
    provider: str
    model: str
    route_stage: str
    route_reason: str | None
    input_tokens: int
    output_tokens: int
    cost_usd: float
    latency_ms: int
    status: int
    fallback_from: str | None


class RequestsRepository:
    def __init__(self, connection: aiosqlite.Connection) -> None:
        self._conn = connection

    async def insert(self, record: RequestRecord) -> None:
        await self._conn.execute(
            """
            INSERT INTO requests (
              id, ts, pool, provider, model, route_stage, route_reason,
              input_tokens, output_tokens, cost_usd, latency_ms, status, fallback_from
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                record.id, record.ts, record.pool, record.provider, record.model,
                record.route_stage, record.route_reason, record.input_tokens,
                record.output_tokens, record.cost_usd, record.latency_ms,
                record.status, record.fallback_from,
            ),
        )
        await self._conn.commit()
```

- [ ] **Step 5: Run tests to verify they pass** — `pytest tests/test_database.py -v` → PASS.

- [ ] **Step 6: Commit**

```bash
git add app/core/database.py app/repositories/ tests/test_database.py
git commit -m "add WAL SQLite database and requests repository"
```

---

## Task 3: OpenAI error envelope + exception handlers

**Milestone:** M1.

**Files:**
- Create: `app/core/errors.py`, `app/schemas/__init__.py`, `app/schemas/openai.py`, `tests/test_errors.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `app.core.errors.GatewayError(message: str, status_code: int, error_type: str, param: str | None = None, code: str | None = None)` — base app exception.
  - `app.core.errors.AuthError(message="Missing or invalid API key")` — subclass, `status_code=401`, `error_type="authentication_error"`.
  - `app.core.errors.BadRequestError(message, param=None)` — subclass, `status_code=400`, `error_type="invalid_request_error"`.
  - `app.core.errors.error_envelope(message, error_type, param=None, code=None) -> dict` — returns `{"error": {...}}`.
  - `app.core.errors.register_exception_handlers(app)` — installs handlers so `GatewayError` and any provider `ProviderError` (Task 5) and unhandled `Exception` render the OpenAI envelope with the right status.

- [ ] **Step 1: Write the failing test** `tests/test_errors.py`:

```python
from app.core.errors import error_envelope, GatewayError, AuthError, BadRequestError


def test_envelope_shape():
    env = error_envelope("boom", "invalid_request_error", param="model", code="x")
    assert env == {
        "error": {"message": "boom", "type": "invalid_request_error",
                  "param": "model", "code": "x"}
    }


def test_auth_error_defaults():
    e = AuthError()
    assert e.status_code == 401 and e.error_type == "authentication_error"


def test_bad_request_carries_param():
    e = BadRequestError("no model", param="model")
    assert e.status_code == 400 and e.param == "model"
    assert isinstance(e, GatewayError)
```

- [ ] **Step 2: Run test to verify it fails** — `pytest tests/test_errors.py -v` → FAIL (module missing).

- [ ] **Step 3: Write `app/core/errors.py`**

```python
from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

logger = logging.getLogger("gateway")


def error_envelope(
    message: str, error_type: str, param: str | None = None, code: str | None = None
) -> dict:
    return {"error": {"message": message, "type": error_type, "param": param, "code": code}}


class GatewayError(Exception):
    def __init__(
        self, message: str, status_code: int, error_type: str,
        param: str | None = None, code: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.error_type = error_type
        self.param = param
        self.code = code


class AuthError(GatewayError):
    def __init__(self, message: str = "Missing or invalid API key") -> None:
        super().__init__(message, 401, "authentication_error")


class BadRequestError(GatewayError):
    def __init__(self, message: str, param: str | None = None) -> None:
        super().__init__(message, 400, "invalid_request_error", param=param)


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(GatewayError)
    async def _gateway(_: Request, exc: GatewayError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=error_envelope(exc.message, exc.error_type, exc.param, exc.code),
        )

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled error")
        return JSONResponse(
            status_code=500,
            content=error_envelope("Internal server error", "api_error"),
        )
```

Note: the `ProviderError` handler is added in Task 5 (it imports `error_envelope` from here).

- [ ] **Step 4: Write `app/schemas/openai.py`**

```python
from __future__ import annotations

import time
from typing import Iterable


def models_list_response(pool_names: Iterable[str], owned_by: str = "llm-gateway") -> dict:
    created = int(time.time())
    return {
        "object": "list",
        "data": [
            {"id": name, "object": "model", "created": created, "owned_by": owned_by}
            for name in pool_names
        ],
    }
```

- [ ] **Step 5: Create `app/schemas/__init__.py`** (empty).

- [ ] **Step 6: Run tests to verify they pass** — `pytest tests/test_errors.py -v` → PASS.

- [ ] **Step 7: Commit**

```bash
git add app/core/errors.py app/schemas/ tests/test_errors.py
git commit -m "add OpenAI error envelope, exception handlers, models-list helper"
```

---

## Task 4: Structured JSON logging

**Milestone:** M1.

**Files:**
- Create: `app/core/logging.py`, `tests/test_logging.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `app.core.logging.configure_logging() -> None` — installs a single stdout handler on the `"gateway"` logger with the JSON formatter, level INFO, `propagate=False`.
  - `app.core.logging.log_request(fields: dict) -> None` — emits one JSON line at INFO with `event="request"` merged into `fields`. Caller passes only non-sensitive fields.

- [ ] **Step 1: Write the failing test** `tests/test_logging.py`:

```python
import json
import logging

from app.core.logging import configure_logging, log_request


def test_log_request_emits_single_json_line(capsys):
    configure_logging()
    log_request({
        "request_id": "r1", "pool": "cheap", "model": "deepseek-chat",
        "route_stage": "passthrough", "input_tokens": 10, "output_tokens": 5,
        "cost_usd": 0.0, "latency_ms": 42, "status": 200, "fallback_hops": 0,
    })
    out = capsys.readouterr().out.strip().splitlines()
    assert len(out) == 1
    payload = json.loads(out[0])
    assert payload["request_id"] == "r1"
    assert payload["event"] == "request"
    assert payload["status"] == 200
    # No bodies leaked
    assert "messages" not in payload


def test_formatter_serializes_standard_log(capsys):
    configure_logging()
    logging.getLogger("gateway").info("hello")
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["message"] == "hello"
```

- [ ] **Step 2: Run test to verify it fails** — `pytest tests/test_logging.py -v` → FAIL (module missing).

- [ ] **Step 3: Write `app/core/logging.py`**

```python
from __future__ import annotations

import json
import logging
import sys

_RESERVED = {
    "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
    "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
    "created", "msecs", "relativeCreated", "thread", "threadName",
    "processName", "process", "taskName",
}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict = {"level": record.levelname, "message": record.getMessage()}
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value
        return json.dumps(payload, default=str)


def configure_logging() -> None:
    logger = logging.getLogger("gateway")
    logger.handlers.clear()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False


def log_request(fields: dict) -> None:
    logging.getLogger("gateway").info("request", extra={"event": "request", **fields})
```

- [ ] **Step 4: Run tests to verify they pass** — `pytest tests/test_logging.py -v` → PASS.

- [ ] **Step 5: Commit**

```bash
git add app/core/logging.py tests/test_logging.py
git commit -m "add structured JSON stdout logging"
```

---

## Task 5: Provider adapter interface + OpenAI-compatible adapter + registry

**Milestone:** M1.

**Files:**
- Create: `app/providers/__init__.py`, `app/providers/base.py`, `app/providers/openai_compatible.py`, `app/providers/registry.py`, `tests/test_providers.py`
- Modify: `app/core/errors.py` (register `ProviderError` handler)

**Interfaces:**
- Consumes: `ProviderConfig` (Task 1), `error_envelope` (Task 3).
- Produces:
  - `app.providers.base.ProviderResponse` dataclass: `status_code: int, body: dict, input_tokens: int, output_tokens: int`.
  - `app.providers.base.ProviderAdapter` — Protocol with attribute `name: str` and async method `chat_completion(payload: dict, model: str) -> ProviderResponse`.
  - `app.providers.base.ProviderError(GatewayError)` — `__init__(message, status_code, provider, retryable, error_type="api_error", code=None)`; stores `provider`, `retryable`. (Retryable classification is unused in M1 but seeds M3.)
  - `app.providers.openai_compatible.OpenAICompatibleAdapter(name, config: ProviderConfig, http_client: httpx.AsyncClient)`.
  - `app.providers.registry.build_registry(config: AppConfig, http_client) -> dict[str, ProviderAdapter]`.

- [ ] **Step 1: Write the failing test** `tests/test_providers.py`:

```python
import httpx
import pytest
import respx

from app.core.config import ProviderConfig
from app.providers.base import ProviderError
from app.providers.openai_compatible import OpenAICompatibleAdapter


def _adapter(client):
    cfg = ProviderConfig(base_url="https://api.deepseek.com/v1", api_key="sk-test")
    return OpenAICompatibleAdapter("deepseek", cfg, client)


@respx.mock
async def test_forwards_and_rewrites_model():
    route = respx.post("https://api.deepseek.com/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={"id": "x", "usage": {"prompt_tokens": 7, "completion_tokens": 3}},
        )
    )
    async with httpx.AsyncClient() as client:
        adapter = _adapter(client)
        resp = await adapter.chat_completion(
            {"model": "cheap", "messages": [{"role": "user", "content": "hi"}],
             "future_field": True},
            model="deepseek-chat",
        )
    sent = route.calls.last.request
    body = __import__("json").loads(sent.content)
    assert body["model"] == "deepseek-chat"          # rewritten
    assert body["future_field"] is True               # unknown field forwarded
    assert sent.headers["authorization"] == "Bearer sk-test"
    assert resp.status_code == 200
    assert (resp.input_tokens, resp.output_tokens) == (7, 3)


@respx.mock
async def test_5xx_raises_retryable_provider_error():
    respx.post("https://api.deepseek.com/v1/chat/completions").mock(
        return_value=httpx.Response(503, json={"error": {"message": "down"}})
    )
    async with httpx.AsyncClient() as client:
        with pytest.raises(ProviderError) as ei:
            await _adapter(client).chat_completion({"model": "x"}, model="deepseek-chat")
    assert ei.value.status_code == 503 and ei.value.retryable is True


@respx.mock
async def test_400_raises_nonretryable_provider_error():
    respx.post("https://api.deepseek.com/v1/chat/completions").mock(
        return_value=httpx.Response(400, json={"error": {"message": "bad"}})
    )
    async with httpx.AsyncClient() as client:
        with pytest.raises(ProviderError) as ei:
            await _adapter(client).chat_completion({"model": "x"}, model="deepseek-chat")
    assert ei.value.status_code == 400 and ei.value.retryable is False
```

- [ ] **Step 2: Run test to verify it fails** — `pytest tests/test_providers.py -v` → FAIL (module missing).

- [ ] **Step 3: Write `app/providers/base.py`**

```python
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from app.core.errors import GatewayError


@dataclass
class ProviderResponse:
    status_code: int
    body: dict
    input_tokens: int
    output_tokens: int


class ProviderError(GatewayError):
    def __init__(
        self, message: str, status_code: int, provider: str, retryable: bool,
        error_type: str = "api_error", code: str | None = None,
    ) -> None:
        super().__init__(message, status_code, error_type, code=code)
        self.provider = provider
        self.retryable = retryable


@runtime_checkable
class ProviderAdapter(Protocol):
    name: str

    async def chat_completion(self, payload: dict, model: str) -> ProviderResponse:
        ...


RETRYABLE_STATUS = {408, 409, 429}


def is_retryable(status_code: int) -> bool:
    return status_code in RETRYABLE_STATUS or status_code >= 500
```

- [ ] **Step 4: Write `app/providers/openai_compatible.py`**

```python
from __future__ import annotations

import httpx

from app.core.config import ProviderConfig
from app.providers.base import ProviderError, ProviderResponse, is_retryable


class OpenAICompatibleAdapter:
    def __init__(self, name: str, config: ProviderConfig, http_client: httpx.AsyncClient) -> None:
        self.name = name
        self._config = config
        self._client = http_client

    async def chat_completion(self, payload: dict, model: str) -> ProviderResponse:
        upstream = {**payload, "model": model}
        url = f"{self._config.base_url.rstrip('/')}/chat/completions"
        headers = {"Authorization": f"Bearer {self._config.api_key}"}
        try:
            resp = await self._client.post(
                url, json=upstream, headers=headers, timeout=self._config.timeout_s
            )
        except httpx.TimeoutException as exc:
            raise ProviderError(
                f"provider '{self.name}' timed out", 504, self.name, retryable=True
            ) from exc
        except httpx.HTTPError as exc:
            raise ProviderError(
                f"provider '{self.name}' request failed: {exc}", 502, self.name,
                retryable=True,
            ) from exc

        if resp.status_code >= 400:
            message = _extract_message(resp) or f"provider '{self.name}' error"
            raise ProviderError(
                message, resp.status_code, self.name,
                retryable=is_retryable(resp.status_code),
            )

        body = resp.json()
        usage = body.get("usage") or {}
        return ProviderResponse(
            status_code=resp.status_code,
            body=body,
            input_tokens=int(usage.get("prompt_tokens", 0)),
            output_tokens=int(usage.get("completion_tokens", 0)),
        )


def _extract_message(resp: httpx.Response) -> str | None:
    try:
        data = resp.json()
    except Exception:
        return None
    err = data.get("error")
    if isinstance(err, dict):
        return err.get("message")
    return None
```

- [ ] **Step 5: Write `app/providers/registry.py`**

```python
from __future__ import annotations

import httpx

from app.core.config import AppConfig
from app.providers.base import ProviderAdapter
from app.providers.openai_compatible import OpenAICompatibleAdapter


def build_registry(config: AppConfig, http_client: httpx.AsyncClient) -> dict[str, ProviderAdapter]:
    return {
        name: OpenAICompatibleAdapter(name, provider_cfg, http_client)
        for name, provider_cfg in config.providers.items()
    }
```

- [ ] **Step 6: Register the `ProviderError` handler in `app/core/errors.py`** — inside `register_exception_handlers`, add (import locally to avoid a cycle):

```python
    from app.providers.base import ProviderError

    @app.exception_handler(ProviderError)
    async def _provider(_: Request, exc: ProviderError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=error_envelope(exc.message, exc.error_type, exc.param, exc.code),
        )
```

Note: `ProviderError` subclasses `GatewayError`, but registering it explicitly keeps intent clear and lets M3 diverge its handling later. Because it is a subclass, the explicit handler takes precedence in FastAPI's handler lookup.

- [ ] **Step 7: Create `app/providers/__init__.py`** (empty).

- [ ] **Step 8: Run tests to verify they pass** — `pytest tests/test_providers.py tests/test_errors.py -v` → PASS.

- [ ] **Step 9: Commit**

```bash
git add app/providers/ app/core/errors.py tests/test_providers.py
git commit -m "add provider adapter interface, OpenAI-compatible adapter, registry"
```

---

## Task 6: Minimal pool resolver

**Milestone:** M1 (seed for M2/M3).

**Files:**
- Create: `app/routing/__init__.py`, `app/routing/pools.py`, `tests/test_pools.py`

**Interfaces:**
- Consumes: `PoolEntry` (Task 1).
- Produces:
  - `app.routing.pools.Resolution` dataclass: `pool: str, provider: str, model: str, route_stage: str, route_reason: str`.
  - `app.routing.pools.PoolResolver(pools: dict[str, list[PoolEntry]], default_pool: str)`; method `resolve(requested_model: str) -> Resolution`.
  - Behavior: if `requested_model` is a known pool → first entry, `pool=requested_model`, `route_stage="passthrough"`, `route_reason=f"m1-passthrough:pool={requested_model}"`. Else → `default_pool` first entry, `route_reason=f"m1-passthrough:default:model-not-a-pool:{requested_model}"`.

- [ ] **Step 1: Write the failing test** `tests/test_pools.py`:

```python
from app.core.config import PoolEntry
from app.routing.pools import PoolResolver

POOLS = {
    "cheap": [PoolEntry(provider="deepseek", model="deepseek-chat"),
              PoolEntry(provider="anthropic", model="claude-haiku-4-5")],
    "default": [PoolEntry(provider="anthropic", model="claude-sonnet-5")],
}


def test_resolves_known_pool_to_first_entry():
    r = PoolResolver(POOLS, "default").resolve("cheap")
    assert (r.pool, r.provider, r.model) == ("cheap", "deepseek", "deepseek-chat")
    assert r.route_stage == "passthrough"
    assert r.route_reason == "m1-passthrough:pool=cheap"


def test_unknown_model_falls_to_default_pool():
    r = PoolResolver(POOLS, "default").resolve("gpt-4o")
    assert (r.pool, r.provider, r.model) == ("default", "anthropic", "claude-sonnet-5")
    assert "model-not-a-pool" in r.route_reason
```

- [ ] **Step 2: Run test to verify it fails** — `pytest tests/test_pools.py -v` → FAIL (module missing).

- [ ] **Step 3: Write `app/routing/pools.py`**

```python
from __future__ import annotations

from dataclasses import dataclass

from app.core.config import PoolEntry


@dataclass
class Resolution:
    pool: str
    provider: str
    model: str
    route_stage: str
    route_reason: str


class PoolResolver:
    def __init__(self, pools: dict[str, list[PoolEntry]], default_pool: str) -> None:
        self._pools = pools
        self._default_pool = default_pool

    def resolve(self, requested_model: str) -> Resolution:
        if requested_model in self._pools:
            entry = self._pools[requested_model][0]
            return Resolution(
                pool=requested_model, provider=entry.provider, model=entry.model,
                route_stage="passthrough",
                route_reason=f"m1-passthrough:pool={requested_model}",
            )
        entry = self._pools[self._default_pool][0]
        return Resolution(
            pool=self._default_pool, provider=entry.provider, model=entry.model,
            route_stage="passthrough",
            route_reason=f"m1-passthrough:default:model-not-a-pool:{requested_model}",
        )
```

- [ ] **Step 4: Create `app/routing/__init__.py`** (empty).

- [ ] **Step 5: Run tests to verify they pass** — `pytest tests/test_pools.py -v` → PASS.

- [ ] **Step 6: Commit**

```bash
git add app/routing/__init__.py app/routing/pools.py tests/test_pools.py
git commit -m "add minimal pool resolver"
```

---

## Task 7: Cost tracker

**Milestone:** M1.

**Files:**
- Create: `app/cost/__init__.py`, `app/cost/tracker.py`, `tests/test_cost.py`

**Interfaces:**
- Consumes: `PriceEntry` (Task 1).
- Produces:
  - `app.cost.tracker.CostTracker(prices: dict[str, PriceEntry])`; method `compute(model: str, input_tokens: int, output_tokens: int) -> float`.
  - Formula: `input_tokens/1_000_000*input_per_1m + output_tokens/1_000_000*output_per_1m`; unknown model → `0.0`.

- [ ] **Step 1: Write the failing test** `tests/test_cost.py`:

```python
from app.core.config import PriceEntry
from app.cost.tracker import CostTracker


def test_unknown_model_is_zero():
    assert CostTracker({}).compute("x", 1000, 1000) == 0.0


def test_known_model_cost():
    prices = {"deepseek-chat": PriceEntry(input_per_1m=0.27, output_per_1m=1.10)}
    cost = CostTracker(prices).compute("deepseek-chat", 1_000_000, 1_000_000)
    assert round(cost, 6) == round(0.27 + 1.10, 6)
```

- [ ] **Step 2: Run test to verify it fails** — `pytest tests/test_cost.py -v` → FAIL (module missing).

- [ ] **Step 3: Write `app/cost/tracker.py`**

```python
from __future__ import annotations

from app.core.config import PriceEntry


class CostTracker:
    def __init__(self, prices: dict[str, PriceEntry]) -> None:
        self._prices = prices

    def compute(self, model: str, input_tokens: int, output_tokens: int) -> float:
        price = self._prices.get(model)
        if price is None:
            return 0.0
        return (
            input_tokens / 1_000_000 * price.input_per_1m
            + output_tokens / 1_000_000 * price.output_per_1m
        )
```

- [ ] **Step 4: Create `app/cost/__init__.py`** (empty).

- [ ] **Step 5: Run tests to verify they pass** — `pytest tests/test_cost.py -v` → PASS.

- [ ] **Step 6: Commit**

```bash
git add app/cost/ tests/test_cost.py
git commit -m "add cost tracker computing cost at write time"
```

---

## Task 8: Gateway service (orchestration: resolve → call → cost → persist → log)

**Milestone:** M1 (this is the seam M2/M3/M4 extend).

**Files:**
- Create: `app/routing/gateway.py`, `tests/test_gateway.py`

**Interfaces:**
- Consumes: `PoolResolver`/`Resolution` (Task 6), `ProviderAdapter`/`ProviderResponse`/`ProviderError` (Task 5), `CostTracker` (Task 7), `RequestsRepository`/`RequestRecord` (Task 2), `BadRequestError` (Task 3), `log_request` (Task 4).
- Produces:
  - `app.routing.gateway.GatewayService(resolver, registry: dict[str, ProviderAdapter], cost: CostTracker, repo: RequestsRepository)`.
  - async `complete(request_id: str, body: dict) -> dict` — returns the upstream response body unchanged (dict).
  - Behavior: raise `BadRequestError("...", param="model")` if `body` has no `model`. Resolve; call the resolved provider's adapter; on success build+insert a `RequestRecord` and emit one `log_request` line, then return the body. On `ProviderError`, still insert a row (status=error status, tokens=0, cost=0) and emit the log line, then **re-raise** so the handler renders the OpenAI error. Latency measured with `time.monotonic()`.

- [ ] **Step 1: Write the failing test** `tests/test_gateway.py`:

```python
import pytest

from app.core.config import PoolEntry, PriceEntry
from app.core.errors import BadRequestError
from app.cost.tracker import CostTracker
from app.providers.base import ProviderError, ProviderResponse
from app.repositories.requests_repo import RequestsRepository
from app.routing.gateway import GatewayService
from app.routing.pools import PoolResolver
from app.core.database import Database

POOLS = {"cheap": [PoolEntry(provider="deepseek", model="deepseek-chat")],
         "default": [PoolEntry(provider="anthropic", model="claude-sonnet-5")]}


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
    svc = GatewayService(
        PoolResolver(POOLS, "default"), adapters, CostTracker({}), repo
    )
    return db, repo, svc


async def test_missing_model_raises_bad_request(tmp_path):
    db, _, svc = await _service(tmp_path, {})
    try:
        with pytest.raises(BadRequestError) as ei:
            await svc.complete("r1", {"messages": []})
        assert ei.value.param == "model"
    finally:
        await db.disconnect()


async def test_happy_path_rewrites_model_and_logs_row(tmp_path):
    resp = ProviderResponse(200, {"id": "x", "choices": []}, 7, 3)
    adapter = FakeAdapter("deepseek", response=resp)
    db, repo, svc = await _service(tmp_path, {"deepseek": adapter})
    try:
        out = await svc.complete("r1", {"model": "cheap", "messages": [], "extra": 1})
        assert out == {"id": "x", "choices": []}          # unchanged body
        assert adapter.last_model == "deepseek-chat"       # resolved upstream model
        assert adapter.last_payload["extra"] == 1          # unknown field preserved
        cur = await db.connection.execute(
            "SELECT pool, provider, model, route_stage, input_tokens, output_tokens, "
            "cost_usd, status FROM requests WHERE id='r1'")
        row = await cur.fetchone()
        assert row == ("cheap", "deepseek", "deepseek-chat", "passthrough", 7, 3, 0.0, 200)
    finally:
        await db.disconnect()


async def test_provider_error_logs_row_and_reraises(tmp_path):
    err = ProviderError("boom", 503, "deepseek", retryable=True)
    adapter = FakeAdapter("deepseek", error=err)
    db, repo, svc = await _service(tmp_path, {"deepseek": adapter})
    try:
        with pytest.raises(ProviderError):
            await svc.complete("r2", {"model": "cheap", "messages": []})
        cur = await db.connection.execute(
            "SELECT status, input_tokens, cost_usd FROM requests WHERE id='r2'")
        assert await cur.fetchone() == (503, 0, 0.0)
    finally:
        await db.disconnect()
```

- [ ] **Step 2: Run test to verify it fails** — `pytest tests/test_gateway.py -v` → FAIL (module missing).

- [ ] **Step 3: Write `app/routing/gateway.py`**

```python
from __future__ import annotations

import time

from app.core.errors import BadRequestError
from app.core.logging import log_request
from app.cost.tracker import CostTracker
from app.providers.base import ProviderAdapter, ProviderError
from app.repositories.requests_repo import RequestRecord, RequestsRepository
from app.routing.pools import PoolResolver, Resolution


class GatewayService:
    def __init__(
        self, resolver: PoolResolver, registry: dict[str, ProviderAdapter],
        cost: CostTracker, repo: RequestsRepository,
    ) -> None:
        self._resolver = resolver
        self._registry = registry
        self._cost = cost
        self._repo = repo

    async def complete(self, request_id: str, body: dict) -> dict:
        requested = body.get("model")
        if not requested:
            raise BadRequestError("Missing required field 'model'", param="model")

        resolution = self._resolver.resolve(requested)
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

- [ ] **Step 4: Run tests to verify they pass** — `pytest tests/test_gateway.py -v` → PASS.

- [ ] **Step 5: Commit**

```bash
git add app/routing/gateway.py tests/test_gateway.py
git commit -m "add gateway service orchestrating passthrough, persistence, logging"
```

---

## Task 9: App factory, lifespan wiring, DI, security dependency, healthz

**Milestone:** M1.

**Files:**
- Create: `app/core/security.py`, `app/api/__init__.py`, `app/api/dependencies.py`, `app/main.py`, `tests/conftest.py`, `tests/test_security.py`
- (No endpoint routers yet — added in Tasks 10–12. `create_app` mounts them once they exist; for this task it mounts only `/healthz`.)

**Interfaces:**
- Consumes: everything above.
- Produces:
  - `app.core.security.require_api_key` — FastAPI dependency: reads `Authorization`, expects `Bearer <key>`, `secrets.compare_digest` against `request.app.state.config.api_key`; raises `AuthError` on mismatch/missing. Returns `None` on success.
  - `app.api.dependencies.get_config(request) -> AppConfig`, `get_gateway(request) -> GatewayService` (both read `request.app.state`).
  - `app.main.create_app(config: AppConfig | None = None) -> FastAPI` — if `config is None`, load from env var `GATEWAY_CONFIG` (default `config.yaml`). Lifespan: `configure_logging()`, connect `Database`, open shared `httpx.AsyncClient`, build registry/resolver/cost/repo/gateway, store on `app.state`. Registers exception handlers. Mounts `/healthz` now; v1 router + dashboard mounted in later tasks.
  - `app.main.app` — module-level instance via `create_app()` (used by uvicorn).

- [ ] **Step 1: Write the failing test** `tests/test_security.py`:

```python
async def test_healthz_no_auth(client):
    resp = await client.get("/healthz")
    assert resp.status_code == 200 and resp.json() == {"status": "ok"}


async def test_protected_route_requires_bearer(client):
    # /v1/models is added in Task 10; use it once available. For Task 9,
    # assert healthz works and a missing route returns 404 (auth wired via
    # dependency is exercised fully in Tasks 10-12 tests).
    resp = await client.get("/healthz")
    assert resp.status_code == 200
```

Note: the full auth 401 assertions live in Tasks 10/12 where protected routes exist. This task proves the app boots with DI and lifespan.

- [ ] **Step 2: Write `tests/conftest.py`**

```python
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.core.config import AppConfig, PoolEntry, PriceEntry, ProviderConfig
from app.main import create_app

VALID_KEY = "test-key-123"


@pytest.fixture
def test_config(tmp_path) -> AppConfig:
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
        prices={},
    )


@pytest_asyncio.fixture
async def app(test_config):
    application = create_app(test_config)
    async with application.router.lifespan_context(application):
        yield application


@pytest_asyncio.fixture
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
def auth_headers():
    return {"Authorization": f"Bearer {VALID_KEY}"}
```

- [ ] **Step 3: Run test to verify it fails** — `pytest tests/test_security.py -v` → FAIL (`app.main` missing).

- [ ] **Step 4: Write `app/core/security.py`**

```python
from __future__ import annotations

import secrets

from fastapi import Request

from app.core.errors import AuthError


async def require_api_key(request: Request) -> None:
    header = request.headers.get("Authorization", "")
    prefix = "Bearer "
    if not header.startswith(prefix):
        raise AuthError()
    provided = header[len(prefix):]
    expected = request.app.state.config.api_key
    if not secrets.compare_digest(provided, expected):
        raise AuthError()
```

- [ ] **Step 5: Write `app/api/dependencies.py`**

```python
from __future__ import annotations

from fastapi import Request

from app.core.config import AppConfig
from app.routing.gateway import GatewayService


def get_config(request: Request) -> AppConfig:
    return request.app.state.config


def get_gateway(request: Request) -> GatewayService:
    return request.app.state.gateway
```

- [ ] **Step 6: Write `app/main.py`**

```python
from __future__ import annotations

import os
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI

from app.core.config import AppConfig, load_config
from app.core.database import Database
from app.core.errors import register_exception_handlers
from app.core.logging import configure_logging
from app.cost.tracker import CostTracker
from app.providers.registry import build_registry
from app.repositories.requests_repo import RequestsRepository
from app.routing.gateway import GatewayService
from app.routing.pools import PoolResolver


def create_app(config: AppConfig | None = None) -> FastAPI:
    if config is None:
        config = load_config(os.environ.get("GATEWAY_CONFIG", "config.yaml"))

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        configure_logging()
        db = Database(config.db_path)
        await db.connect()
        http_client = httpx.AsyncClient()
        registry = build_registry(config, http_client)
        resolver = PoolResolver(config.pools, config.default_pool)
        cost = CostTracker(config.prices)
        repo = RequestsRepository(db.connection)
        app.state.config = config
        app.state.db = db
        app.state.http_client = http_client
        app.state.gateway = GatewayService(resolver, registry, cost, repo)
        try:
            yield
        finally:
            await http_client.aclose()
            await db.disconnect()

    app = FastAPI(title="LLM Cost-Aware Routing Gateway", lifespan=lifespan)
    register_exception_handlers(app)

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"status": "ok"}

    # v1 router and dashboard are mounted in Tasks 10-12.
    from app.api.v1.router import router as v1_router  # noqa: E402
    from app.api.dashboard import router as dashboard_router  # noqa: E402
    app.include_router(v1_router, prefix="/v1")
    app.include_router(dashboard_router)
    return app


app = create_app.__wrapped__ if hasattr(create_app, "__wrapped__") else None
```

Note: the two `include_router` imports reference modules created in Tasks 10–12. **Implement Task 9 up to and including the `/healthz` route and lifespan, but add the two `include_router` lines only after Task 10/12 modules exist** — OR create minimal empty routers now. To keep Task 9 runnable in isolation, create the stubs in Step 7 below.

- [ ] **Step 7: Create minimal router stubs so the app imports cleanly**

Create `app/api/v1/__init__.py` (empty), `app/api/v1/endpoints/__init__.py` (empty), and:

`app/api/v1/router.py`:
```python
from fastapi import APIRouter

router = APIRouter()
```

`app/api/dashboard.py`:
```python
from fastapi import APIRouter

router = APIRouter()
```

Fix the bottom of `app/main.py` — replace the confusing last line with a clean module-level app:
```python
app = create_app()
```
(Remove the `__wrapped__` line entirely; it was a placeholder. `create_app()` at import time will call `load_config` from `GATEWAY_CONFIG`/`config.yaml`. For local dev this needs a real `config.yaml`; tests always call `create_app(test_config)` directly and never import `app`.)

- [ ] **Step 8: Run tests to verify they pass** — `pytest tests/test_security.py -v` → PASS.

- [ ] **Step 9: Commit**

```bash
git add app/core/security.py app/api/__init__.py app/api/v1/__init__.py app/api/v1/endpoints/__init__.py app/api/v1/router.py app/api/dashboard.py app/api/dependencies.py app/main.py tests/conftest.py tests/test_security.py
git commit -m "add app factory, lifespan wiring, DI, bearer auth, healthz"
```

---

## Task 10: `GET /v1/models` endpoint

**Milestone:** M1.

**Files:**
- Create: `app/api/v1/endpoints/models.py`, `tests/test_models_endpoint.py`
- Modify: `app/api/v1/router.py`

**Interfaces:**
- Consumes: `models_list_response` (Task 3), `require_api_key` + `get_config` (Task 9).
- Produces: route `GET /v1/models` → `models_list_response(config.pools.keys())`, guarded by `require_api_key`.

- [ ] **Step 1: Write the failing test** `tests/test_models_endpoint.py`:

```python
async def test_requires_auth(client):
    resp = await client.get("/v1/models")
    assert resp.status_code == 401
    assert resp.json()["error"]["type"] == "authentication_error"


async def test_lists_pools_as_models(client, auth_headers):
    resp = await client.get("/v1/models", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["object"] == "list"
    ids = {m["id"] for m in data["data"]}
    assert ids == {"cheap", "default", "large-context"}
    assert all(m["object"] == "model" for m in data["data"])
```

- [ ] **Step 2: Run test to verify it fails** — `pytest tests/test_models_endpoint.py -v` → FAIL (401 route not mounted / 404).

- [ ] **Step 3: Write `app/api/v1/endpoints/models.py`**

```python
from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.dependencies import get_config
from app.core.config import AppConfig
from app.core.security import require_api_key
from app.schemas.openai import models_list_response

router = APIRouter()


@router.get("/models", dependencies=[Depends(require_api_key)])
async def list_models(config: AppConfig = Depends(get_config)) -> dict:
    return models_list_response(config.pools.keys())
```

- [ ] **Step 4: Wire into `app/api/v1/router.py`**

```python
from fastapi import APIRouter

from app.api.v1.endpoints import models

router = APIRouter()
router.include_router(models.router)
```

- [ ] **Step 5: Run tests to verify they pass** — `pytest tests/test_models_endpoint.py -v` → PASS.

- [ ] **Step 6: Commit**

```bash
git add app/api/v1/endpoints/models.py app/api/v1/router.py tests/test_models_endpoint.py
git commit -m "add GET /v1/models listing pools as models"
```

---

## Task 11: `POST /v1/chat/completions` endpoint

**Milestone:** M1.

**Files:**
- Create: `app/api/v1/endpoints/chat.py`, `tests/test_chat_endpoint.py`
- Modify: `app/api/v1/router.py`

**Interfaces:**
- Consumes: `get_gateway` (Task 9), `require_api_key` (Task 9), `GatewayService.complete` (Task 8).
- Produces: route `POST /v1/chat/completions` guarded by `require_api_key`. Reads raw JSON body (`await request.json()`), generates `request_id = uuid4().hex`, calls `gateway.complete(request_id, body)`, returns `JSONResponse(content=body_out)` (status 200). `GatewayError`/`ProviderError`/`BadRequestError` propagate to the registered handlers. Malformed JSON → `BadRequestError`.

- [ ] **Step 1: Write the failing test** `tests/test_chat_endpoint.py`:

```python
import json

import httpx
import respx


async def test_requires_auth(client):
    resp = await client.post("/v1/chat/completions", json={"model": "cheap"})
    assert resp.status_code == 401


async def test_missing_model_is_400(client, auth_headers):
    resp = await client.post("/v1/chat/completions", json={"messages": []},
                             headers=auth_headers)
    assert resp.status_code == 400
    body = resp.json()
    assert body["error"]["type"] == "invalid_request_error"
    assert body["error"]["param"] == "model"


@respx.mock
async def test_passthrough_rewrites_model_and_forwards_unknown_fields(client, auth_headers):
    route = respx.post("https://api.deepseek.com/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={"id": "resp-1", "choices": [{"message": {"content": "hi"}}],
                  "usage": {"prompt_tokens": 7, "completion_tokens": 3}},
        )
    )
    resp = await client.post(
        "/v1/chat/completions",
        headers=auth_headers,
        json={"model": "cheap", "messages": [{"role": "user", "content": "hi"}],
              "temperature": 0.2, "future_field": True},
    )
    assert resp.status_code == 200
    assert resp.json()["id"] == "resp-1"                 # body unchanged
    sent = json.loads(route.calls.last.request.content)
    assert sent["model"] == "deepseek-chat"              # rewritten
    assert sent["temperature"] == 0.2                    # preserved
    assert sent["future_field"] is True                  # unknown forwarded


@respx.mock
async def test_upstream_500_returns_openai_error(client, auth_headers):
    respx.post("https://api.deepseek.com/v1/chat/completions").mock(
        return_value=httpx.Response(500, json={"error": {"message": "down"}})
    )
    resp = await client.post(
        "/v1/chat/completions", headers=auth_headers,
        json={"model": "cheap", "messages": []},
    )
    assert resp.status_code == 500
    assert "error" in resp.json()


@respx.mock
async def test_writes_one_request_row(client, auth_headers, app):
    respx.post("https://api.deepseek.com/v1/chat/completions").mock(
        return_value=httpx.Response(
            200, json={"id": "r", "usage": {"prompt_tokens": 4, "completion_tokens": 2}})
    )
    await client.post("/v1/chat/completions", headers=auth_headers,
                      json={"model": "cheap", "messages": []})
    conn = app.state.db.connection
    cur = await conn.execute(
        "SELECT pool, provider, model, route_stage, input_tokens, output_tokens, "
        "cost_usd, status, fallback_from FROM requests")
    rows = await cur.fetchall()
    assert len(rows) == 1
    assert rows[0] == ("cheap", "deepseek", "deepseek-chat", "passthrough", 4, 2, 0.0, 200, None)
```

- [ ] **Step 2: Run test to verify it fails** — `pytest tests/test_chat_endpoint.py -v` → FAIL (route not mounted).

- [ ] **Step 3: Write `app/api/v1/endpoints/chat.py`**

```python
from __future__ import annotations

from uuid import uuid4

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from app.api.dependencies import get_gateway
from app.core.errors import BadRequestError
from app.core.security import require_api_key
from app.routing.gateway import GatewayService

router = APIRouter()


@router.post("/chat/completions", dependencies=[Depends(require_api_key)])
async def chat_completions(
    request: Request, gateway: GatewayService = Depends(get_gateway)
) -> JSONResponse:
    try:
        body = await request.json()
    except Exception as exc:
        raise BadRequestError("Request body must be valid JSON") from exc
    if not isinstance(body, dict):
        raise BadRequestError("Request body must be a JSON object")

    request_id = uuid4().hex
    result = await gateway.complete(request_id, body)
    return JSONResponse(content=result, status_code=200)
```

- [ ] **Step 4: Wire into `app/api/v1/router.py`**

```python
from fastapi import APIRouter

from app.api.v1.endpoints import chat, models

router = APIRouter()
router.include_router(models.router)
router.include_router(chat.router)
```

- [ ] **Step 5: Run tests to verify they pass** — `pytest tests/test_chat_endpoint.py -v` → PASS.

- [ ] **Step 6: Commit**

```bash
git add app/api/v1/endpoints/chat.py app/api/v1/router.py tests/test_chat_endpoint.py
git commit -m "add POST /v1/chat/completions passthrough endpoint"
```

---

## Task 12: `GET /dashboard` auth-guarded stub

**Milestone:** M1 (full UI is M5).

**Files:**
- Create: `tests/test_dashboard.py`
- Modify: `app/api/dashboard.py`

**Interfaces:**
- Consumes: `require_api_key` (Task 9).
- Produces: route `GET /dashboard` guarded by `require_api_key`, returns `HTMLResponse` placeholder.

- [ ] **Step 1: Write the failing test** `tests/test_dashboard.py`:

```python
async def test_dashboard_requires_auth(client):
    resp = await client.get("/dashboard")
    assert resp.status_code == 401


async def test_dashboard_stub_behind_auth(client, auth_headers):
    resp = await client.get("/dashboard", headers=auth_headers)
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "M5" in resp.text
```

- [ ] **Step 2: Run test to verify it fails** — `pytest tests/test_dashboard.py -v` → FAIL (route returns 404).

- [ ] **Step 3: Replace `app/api/dashboard.py`**

```python
from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse

from app.core.security import require_api_key

router = APIRouter()

_STUB = "<html><body><h1>Gateway dashboard</h1><p>Coming in M5.</p></body></html>"


@router.get("/dashboard", dependencies=[Depends(require_api_key)])
async def dashboard() -> HTMLResponse:
    return HTMLResponse(content=_STUB)
```

- [ ] **Step 4: Run tests to verify they pass** — `pytest tests/test_dashboard.py -v` → PASS.

- [ ] **Step 5: Commit**

```bash
git add app/api/dashboard.py tests/test_dashboard.py
git commit -m "add auth-guarded dashboard stub"
```

---

## Task 13: Full-suite integration pass + run docs

**Milestone:** M1.

**Files:**
- Create: `tests/test_app_integration.py`
- Modify: `.claude/CLAUDE.md` "Commands" section is already accurate; add a short `README`-style run note only if none exists (optional).

**Interfaces:**
- Consumes: the assembled app via `client` fixture.
- Produces: an end-to-end assertion that auth + models + chat + logging row all cohere in one boot.

- [ ] **Step 1: Write the integration test** `tests/test_app_integration.py`:

```python
import httpx
import respx


async def test_end_to_end_flow(client, auth_headers, app):
    # models
    models = await client.get("/v1/models", headers=auth_headers)
    assert {m["id"] for m in models.json()["data"]} == {"cheap", "default", "large-context"}

    # chat via default pool (unknown model name -> default_pool)
    with respx.mock:
        respx.post("https://api.anthropic.com/v1/chat/completions").mock(
            return_value=httpx.Response(
                200, json={"id": "z", "usage": {"prompt_tokens": 1, "completion_tokens": 1}})
        )
        resp = await client.post("/v1/chat/completions", headers=auth_headers,
                                 json={"model": "gpt-4o", "messages": []})
    assert resp.status_code == 200

    cur = await app.state.db.connection.execute(
        "SELECT pool, provider, route_reason FROM requests")
    row = await cur.fetchone()
    assert row[0] == "default" and row[1] == "anthropic"
    assert "model-not-a-pool" in row[2]
```

- [ ] **Step 2: Run the full suite** — `pytest -v` → all PASS.

- [ ] **Step 3: Manual smoke doc (no code).** Confirm the documented local run works conceptually: `cp config.example.yaml config.yaml` (fill placeholders) then `uvicorn app.main:app --reload`. Do not commit `config.yaml`.

- [ ] **Step 4: Commit**

```bash
git add tests/test_app_integration.py
git commit -m "add end-to-end integration test for M1 passthrough"
```

---

## Self-Review (completed by planner)

- **Spec coverage:** (1) chat passthrough → Tasks 8, 11; (2) `/v1/models` → Task 10; (3) constant-time bearer on API + dashboard → Tasks 9, 10, 11, 12; (4) SQLite WAL request logging → Tasks 2, 8; (5) OpenAI error shape incl. upstream failures → Tasks 3, 5, 11; (6) JSON stdout logs, no bodies → Tasks 4, 8; (7) config from `config.yaml` + price-table stub + committed example → Tasks 1, 7. All 7 items mapped.
- **Type consistency:** `RequestRecord`, `Resolution`, `ProviderResponse`, `GatewayService.complete`, `PoolResolver.resolve`, and `require_api_key` signatures are used identically across producing and consuming tasks.
- **Placeholder scan:** every code step contains concrete code; no TBD/TODO left.
- **Forward-compat check:** ordered pool lists (Task 6), `route_stage`/`route_reason`/`fallback_from` columns (Task 2), `ProviderError.retryable` + `is_retryable` (Task 5), and `CostTracker` reading a price table (Task 7) all exist now so M2/M3/M4 extend without schema or interface churn.
