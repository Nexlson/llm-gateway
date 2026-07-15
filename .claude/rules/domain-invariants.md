# Domain invariants

Behaviors the gateway must preserve. These come from the architecture spec; breaking one
is a regression even if tests pass. See [../knowledge/architecture.md](../knowledge/architecture.md)
for the reasoning behind each.

## OpenAI compatibility

- The only integration step for a caller is swapping `base_url`. Do not require any other
  client change.
- Request and response bodies pass through **unchanged except for the `model` field**.
- **Forward unknown fields** rather than rejecting them, so SDK upgrades don't break the
  gateway.
- Errors are returned in **OpenAI's error shape**, including when the whole fallback chain
  is exhausted.

## Routing

- **Rules before classifier, always.** Stage 1 is deterministic YAML matching (token
  thresholds, tool presence, pool hints, header/metadata, system-prompt regex). Most
  traffic must never reach stage 2.
- **The classifier is a hint, never a hard dependency.** It runs on a cheap model with
  constrained output and a hard timeout. On timeout or garbage output, route to
  `fallback_pool`. A classifier failure must never fail the request.
- **Record how every request was routed:** `route_stage` (`rule` | `classifier` |
  `fallback`) and a `route_reason`. This is what makes the rules tunable from real traffic.

## Fallback & health

- Each pool is an **ordered list**. On a *retryable* failure (5xx, timeout, rate limit),
  advance to the next entry.
- A failing model is marked unhealthy and **skipped for 60 seconds**, then gets one free
  attempt again. No half-open state machine, no rolling error windows — a plain cooldown.
- **Non-retryable errors (400, auth) return immediately** without burning the chain.

## Cost & storage

- **Cost is computed at write time** from the static price table in config, then stored on
  the row. Historical rows stay accurate when prices later change — never recompute old
  rows against new prices.
- SQLite runs in **WAL mode**. Single node; the `requests` table is the source of truth for
  the dashboard.
- Keep the `requests` schema close to the spec's sketch; index on `ts`.

## Auth & logging

- Single **static API key** via `Authorization: Bearer …`, compared with a
  **constant-time** check. The dashboard sits behind the same key.
- Logs are **structured JSON to stdout**, one line per request: request id, pool, model,
  route stage, tokens, cost, latency, fallback hops.
- **No prompt or completion bodies in logs** by default.

## Scope guard

Everything in the architecture doc's **Deferred** table (multi-key auth, Prometheus/OTel,
latency-aware routing, streaming/SSE, live config reload, semantic caching) is **out of
V1 scope**. Do not build it without an explicit decision to move it in.
