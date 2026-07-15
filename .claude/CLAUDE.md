# LLM Cost-Aware Routing Gateway

An OpenAI-compatible proxy that routes each request to the cheapest model that can
handle it, falls back when a provider misbehaves, and records what every request costs.

Full design lives in [knowledge/architecture.md](knowledge/architecture.md) — read it
before making non-trivial routing, storage, or dashboard decisions.

## Status

**Design stage — no application code exists yet.** The repo currently holds this
`.claude/` guidance and the architecture spec. Build order is the roadmap below.

## Tech stack

| Layer | Choice |
|---|---|
| API | FastAPI + Uvicorn (async) |
| Routing | YAML rules (stage 1) + cheap-model classifier (stage 2) |
| Storage | SQLite in WAL mode |
| Dashboard | Server-rendered HTML (no build step) |
| Config | Mounted `config.yaml` (rules, pools, classifier, price table) |
| Deploy | Docker Compose on a VPS behind an existing reverse proxy |

## Intended repo layout

Follow the FastAPI structure skill (see below), adapted for this gateway:

```
app/
├── api/v1/
│   ├── endpoints/
│   │   ├── chat.py          # POST /v1/chat/completions
│   │   └── models.py        # GET  /v1/models  (pools presented as models)
│   ├── dashboard.py         # GET  /dashboard  (server-rendered HTML)
│   └── router.py
├── core/
│   ├── config.py            # loads config.yaml + static price table
│   ├── security.py          # static bearer key, constant-time compare
│   └── database.py          # SQLite (WAL) connection
├── routing/
│   ├── rules.py             # stage 1: deterministic YAML rule engine
│   ├── classifier.py        # stage 2: cheap-model pool classifier
│   └── pools.py             # pool resolver + fallback chain + 60s cooldown
├── providers/               # per-provider adapters (anthropic, openai, deepseek, …)
├── cost/tracker.py          # cost-at-write-time; builds the requests row
├── schemas/                 # Pydantic OpenAI-compatible request/response models
├── repositories/            # data access (e.g. requests_repo.py)
└── main.py                  # app entry
config.yaml                  # rules + pools + classifier + prices (mounted, read at boot)
```

## Working in this repo

- **Read the rules before writing code:** [rules/domain-invariants.md](rules/domain-invariants.md)
  (behaviors that must not regress) and [rules/engineering.md](rules/engineering.md)
  (how to write the code).
- **Conventions come from skills**, not copied prose. Invoke them via the Skill tool:
  - `fastapi-template` — project structure, async, DI, testing setup.
  - `python-design-patterns` — KISS, SRP, composition, rule of three, layering.
- **Keep V1 scope tight.** Anything in the "Deferred" table of the architecture doc is
  out of scope — don't build it without a decision to move it in.
- **Build features through the workflow.** Run `/workflow <task>` to drive the isolated
  plan → code → test → review loop instead of coding ad hoc. See
  [knowledge/workflow.md](knowledge/workflow.md).

## Commands

> Intended once code lands — no build system exists yet. Update this section when it does.

```bash
uvicorn app.main:app --reload      # run the gateway locally
pytest                             # run the test suite
docker compose up                  # run as deployed (SQLite volume + reverse proxy)
```

## Build order (roadmap)

| Milestone | Deliverable |
|---|---|
| M1 | Passthrough proxy + static key + SQLite request logging |
| M2 | YAML rule engine + pools + cost table |
| M3 | Fallback chain + 60s cooldown |
| M4 | Classifier stage + routing-reason capture |
| M5 | Dashboard |
| M6 | VPS deploy, first real workload cut over |
