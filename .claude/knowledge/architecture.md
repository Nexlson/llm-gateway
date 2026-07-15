# LLM Cost-Aware Routing Gateway

An OpenAI-compatible proxy that routes requests to the cheapest model that can actually handle them, and tracks what every request costs.

## Problem

Every LLM call in a codebase tends to hardcode one model. That model is usually chosen for the hardest case in the app, so the easy 80% of traffic — classification, extraction, short summaries — pays frontier prices. Meanwhile spend is invisible until the invoice arrives, and there's no per-feature or per-day breakdown to act on.

## Solution

A single proxy sitting between the app and the providers. Point `base_url` at the gateway and nothing else changes. The gateway decides which model to use, falls back when a provider misbehaves, and writes cost per request to a local database with a dashboard on top.

## Non-Goals (V1)

Not a load balancer, not an observability platform, not a prompt framework. It routes, falls back, and accounts for spend. That's it.

---

## Architecture

```
Client (OpenAI SDK)
  │  base_url = https://gateway.example.dev/v1
  ▼
┌─────────────────────────────────────────────┐
│ FastAPI gateway                             │
│                                             │
│  auth ──▶ router ──▶ pool resolver ──▶ call │
│            │             │                  │
│         YAML rules    fallback chain        │
│         classifier    (60s cooldown)        │
│                                             │
│  ──▶ cost tracker ──▶ SQLite ──▶ /dashboard │
└─────────────────────────────────────────────┘
  ▼
Providers (Anthropic / OpenAI / DeepSeek / …)
```

### Stack

| Layer | Choice | Why |
|---|---|---|
| API | FastAPI + Uvicorn | Async, matches the OpenAI schema cleanly |
| Routing | YAML rules + cheap classifier | Deterministic where possible, model only for the gray area |
| Storage | SQLite (WAL) | Single node, no ops burden, plenty for request-level rows |
| Dashboard | Server-rendered HTML | No build step, no frontend to maintain |
| Deploy | Docker Compose on VPS | Fits existing infra |

---

## V1 Scope

### 1. OpenAI-compatible proxy

- `POST /v1/chat/completions` — the surface that matters
- `GET /v1/models` — returns the configured pools as if they were models
- Request/response bodies pass through unchanged except for the model field
- Unknown fields forwarded rather than rejected, so SDK upgrades don't break the gateway

Swap `base_url`, keep the SDK. That's the whole integration story.

### 2. Hybrid routing

Two stages, cheapest first:

**Stage 1 — YAML rules.** Deterministic matches on things that are obvious: token count thresholds, presence of tools, explicit pool hints, request metadata (e.g. `x-pool: cheap`), regex on the system prompt. Most traffic should never reach stage 2.

```yaml
rules:
  - name: explicit-hint
    when: { header: "x-pool" }
    pool: "{{ header.x-pool }}"

  - name: short-and-simple
    when: { max_input_tokens: 500, has_tools: false }
    pool: cheap

  - name: long-context
    when: { min_input_tokens: 20000 }
    pool: large-context

pools:
  cheap:
    - provider: deepseek
      model: deepseek-chat
    - provider: anthropic
      model: claude-haiku-4-5
  default:
    - provider: anthropic
      model: claude-sonnet-5
  large-context:
    - provider: anthropic
      model: claude-sonnet-5

classifier:
  enabled: true
  pool: cheap          # the classifier itself runs on a cheap model
  labels: [cheap, default]
  fallback_pool: default
```

**Stage 2 — classifier.** For anything the rules don't catch, a small model gets a truncated view of the request and returns a pool label. Constrained output, hard timeout, and on timeout or garbage output the request goes to `fallback_pool`. The classifier is a routing hint, never a hard dependency.

Every routed request records which stage decided and why, so the rules can be tuned from real traffic instead of guesses.

### 3. Fallback chain

Each pool is an ordered list. On a retryable failure (5xx, timeout, rate limit), the gateway moves to the next entry. A model that fails is marked unhealthy and skipped for **60 seconds**, then gets a free attempt again. No half-open state machine, no rolling error windows — a cooldown is enough at this scale.

If every entry in the chain is exhausted, the error is returned to the client in OpenAI's error shape. Non-retryable errors (400, auth) return immediately without burning the chain.

### 4. Cost dashboard

`GET /dashboard` — a single page, server-rendered:

- Spend today / this week / this month, against an optional budget line
- Breakdown by pool, by model, by provider
- Routing mix — what share went cheap vs default, and how much that saved versus a single-model baseline
- Recent requests table with model, tokens, cost, latency, routing reason
- Fallback events and cooldown trips

Cost is computed at write time from a static price table in config, so historical rows stay accurate when prices change.

**Schema sketch**

```sql
CREATE TABLE requests (
  id            TEXT PRIMARY KEY,
  ts            INTEGER NOT NULL,
  pool          TEXT NOT NULL,
  provider      TEXT NOT NULL,
  model         TEXT NOT NULL,
  route_stage   TEXT NOT NULL,   -- 'rule' | 'classifier' | 'fallback'
  route_reason  TEXT,
  input_tokens  INTEGER NOT NULL,
  output_tokens INTEGER NOT NULL,
  cost_usd      REAL NOT NULL,
  latency_ms    INTEGER NOT NULL,
  status        INTEGER NOT NULL,
  fallback_from TEXT
);
CREATE INDEX idx_requests_ts ON requests(ts);
```

### 5. Auth and logging

- Single static API key via `Authorization: Bearer …`, compared with a constant-time check
- The dashboard sits behind the same key (or a reverse-proxy access layer)
- Structured JSON logs to stdout — one line per request, carrying request id, pool, model, route stage, tokens, cost, latency, and any fallback hops
- No prompt or completion bodies in logs by default

### 6. Deployment

Docker Compose on the VPS, behind the existing reverse proxy with TLS. SQLite file on a mounted volume, WAL mode on. Config is a mounted YAML file; changing it means a container restart.

---

## Deferred

| Item | Why it waits |
|---|---|
| Multi-key auth + per-key budgets | One consumer at V1; multi-tenancy changes the schema and the dashboard |
| Prometheus / OpenTelemetry | stdout logs answer the questions V1 has |
| Latency-aware routing | Needs a latency history worth trusting; cost is the point right now |
| Streaming (SSE) | Token accounting on streamed responses is its own problem |
| Live config reload | Restart is a second on a single node |
| Semantic caching | Real savings, but orthogonal to routing — a later layer |

---

## Success Criteria

1. An existing app switches to the gateway by changing `base_url` only, with no behavior regression.
2. The dashboard shows a real spend number that reconciles with provider billing to within a few percent.
3. Measurable cost reduction versus the single-model baseline, with the routing mix visible enough to explain where the savings came from.
4. A provider outage degrades to fallback instead of taking the app down.
5. Routing overhead stays negligible relative to the upstream call — rule path in single-digit milliseconds.

## Open Questions

- Does the classifier see the full prompt or a truncated slice? Truncation is cheaper, but risks misreading long requests.
- Should classifier decisions be cached by prompt shape or hash to avoid paying twice for repeated traffic?
- Where do embeddings and other non-chat endpoints fit — same pool model, or out of scope entirely?
- What's the behavior when the classifier's chosen pool is entirely in cooldown — reroute or fail?

## Roadmap

| Milestone | Deliverable |
|---|---|
| M1 | Passthrough proxy + static key + SQLite request logging |
| M2 | YAML rule engine + pools + cost table |
| M3 | Fallback chain + 60s cooldown |
| M4 | Classifier stage + routing reason capture |
| M5 | Dashboard |
| M6 | VPS deploy, first real workload cut over |