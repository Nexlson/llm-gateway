# Engineering conventions

How to write the code. Domain behavior lives in
[domain-invariants.md](domain-invariants.md); this file is about style and structure.

## Source of truth: the skills

Do not restate these here — invoke them via the Skill tool and apply them:

- **`fastapi-template`** — project structure, dependency injection via `Depends`, async
  route/DB/middleware patterns, and the pytest + `httpx.AsyncClient` test setup.
- **`python-design-patterns`** — KISS, single responsibility, composition over inheritance,
  rule of three, constructor injection, layering (API → service → repository, dependency
  arrow always points down).

## Python

- **Type hints on all public functions**, plus Pydantic models for request/response
  schemas.
- **Async all the way down** for anything touching I/O (provider HTTP calls, DB writes).
  Never block the event loop with sync I/O in a handler.
- **Constructor-inject dependencies** (config, DB session, provider clients) so units test
  in isolation. Avoid module-level singletons for anything stateful.
- **Keep functions small and single-purpose** (~20–50 lines). A growing file is a signal to
  split, not to add another responsibility.

## Layering (this project)

```
api/ (handlers)  →  routing/ + cost/ (services)  →  repositories/ (data access)
                          ↑
                   schemas/  (shared Pydantic models both sides import)
```

Handlers must not contain routing or persistence logic; services must not import from
`api/`. Provider adapters live behind a common interface in `providers/` so the fallback
chain treats them uniformly.

## Configuration

- All tunables (rules, pools, classifier, prices, key) come from the mounted `config.yaml`,
  loaded once at boot in `core/config.py`. Changing config means a container restart in V1 —
  no live reload.
- Never hardcode model names, prices, or thresholds in code; they belong in config.

## Testing

- Follow the `conftest.py` fixture pattern from the FastAPI skill (in-memory SQLite,
  `dependency_overrides`, `AsyncClient`).
- Every routing decision and fallback path needs a test — these are the invariants most
  likely to silently regress.
- Mock provider calls; never hit real provider APIs in tests.

## Git

- Small, focused commits scoped to one milestone slice.
- Present-tense, imperative subject lines (e.g. `add YAML rule engine`).
- Do not commit secrets or a populated SQLite file; keep real keys out of `config.yaml`
  committed to the repo (use an example config).
