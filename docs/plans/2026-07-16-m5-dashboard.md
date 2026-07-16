# M5 — Cost Dashboard Implementation Plan

**Run-id:** `20260716-m5-dashboard`

**Goal:** Replace the authenticated dashboard stub with one server-rendered HTML page showing UTC spend for today, this week, and this month; optional monthly budget progress; monthly spend breakdowns by pool, model, and provider; monthly routing mix and a configurable single-model savings estimate; recent requests; and fallback events. All operational values come from the existing `requests` table. There is no frontend build step and `/dashboard` keeps the existing bearer-key dependency.

## Design decisions

- Actual spend always sums the `cost_usd` stored at request-write time. Historical actual cost is never recomputed from today's prices.
- Calendar periods are UTC: day starts at 00:00, week starts Monday, and month starts on day 1. The page labels this explicitly.
- `dashboard.budget_usd` is an optional positive monthly budget. When absent, the page says it is not configured; no per-key or multi-tenant budget is added.
- `dashboard.baseline_model` is optional. When configured, its static input/output rates estimate what this month's recorded tokens would cost on that one model. Savings are `estimated baseline - stored actual spend` and are clearly labeled as an estimate. The config validator requires the model to exist in `prices`.
- Monthly data drives breakdowns, routing mix, baseline, and budget progress. Recent requests and fallback events are bounded independently by config defaults (50 and 25).
- Fallback events are rows where `fallback_from IS NOT NULL`. Cooldown skips cannot be reconstructed from the schema, so the page states that cooldown trips are not separately recorded instead of fabricating them.
- Database-originated strings are HTML-escaped. The page is a Python-rendered template with embedded CSS and no JavaScript, CDN, template engine, or new dependency.

## File structure

- Modify `app/core/config.py`: add `DashboardConfig` and validate the optional baseline.
- Modify `config.example.yaml`: document optional budget/baseline and bounded table limits.
- Modify `app/repositories/requests_repo.py`: add read-only aggregate/recent/fallback query methods and dashboard row dataclasses.
- Create `app/dashboard.py`: define dashboard view models and `DashboardService`, including UTC boundaries and baseline math.
- Modify `app/api/dependencies.py`: expose the constructed dashboard service.
- Replace `app/api/dashboard.py`: authenticated handler plus escaped server-side renderer.
- Modify `app/main.py`: construct `DashboardService` with the existing repository/config and store it in app state.
- Modify `tests/test_config.py`: defaults, example, and invalid-baseline coverage.
- Modify `tests/test_dashboard.py`: auth, empty state, populated page, escaping, period/budget/baseline/fallback coverage.
- Create `tests/test_dashboard_service.py`: deterministic boundary and counterfactual-cost unit tests.
- Create `tests/test_acceptance_m5.py`: independent end-to-end acceptance checks against seeded SQLite rows.
- Modify `.claude/CLAUDE.md`: mark M5 done only after test and review gates approve.

## Acceptance criteria

1. Missing or wrong bearer auth still returns 401 in the existing OpenAI error shape; correct auth returns 200 `text/html`.
2. An empty database renders zero spend for all three windows and useful empty states for every table/section.
3. Today/week/month use UTC calendar boundaries (Monday week start), include rows at the lower bound, and exclude older rows.
4. Actual totals and all pool/model/provider breakdown totals use stored `cost_usd`; pool/model/provider groupings cover the current month and are ordered by spend descending.
5. Routing mix shows current-month request count/share by pool and route stage, including configured and unexpected stored names without hardcoded pool/model identifiers.
6. With no budget, the page says `Not configured`. With a monthly budget, it shows the amount, month spend, percentage used, and a capped visual progress bar while preserving the uncapped percentage text.
7. With no baseline, savings says `Not configured`. With a baseline model, estimated baseline cost applies that configured model's current input/output rates to current-month recorded token totals, while actual remains stored cost; the displayed delta may be positive or negative.
8. Recent requests show UTC timestamp, pool, provider/model, total tokens, stored cost, latency, status, route stage, and route reason, newest first, bounded by the configured limit.
9. Fallback events show timestamp, selected pool, `fallback_from`, final provider/model, status, and routing reason, newest first, bounded by the configured limit. The page discloses that cooldown trips are unavailable because they are not separately recorded.
10. All database strings are escaped as text; inserted HTML/script-like values do not become markup.
11. No new runtime dependency, build step, schema migration, provider call, push, deploy, or real-key use is introduced. All pre-M5 tests remain green.

## Implementation sequence

1. Add failing config and service tests for optional dashboard settings, UTC windows, stored-spend totals, routing shares, and baseline math.
2. Add `DashboardConfig`, repository read models/queries, and `DashboardService`; run focused unit tests.
3. Replace the endpoint stub and add the no-build HTML renderer, DI, and lifespan wiring; add endpoint tests for populated/empty/escaped states.
4. Add independent M5 acceptance tests, then run the complete suite.
5. Review the full milestone diff for correctness, security, layering, query bounds, accessibility/readability, and scope regressions. Fix only blocking findings and re-run the complete suite after changes.
6. On `APPROVE` plus a green full suite, commit the plan/code/tests, merge the milestone branch into `main` with `--no-ff`, mark M5 done, and stop before M6. Do not push.

