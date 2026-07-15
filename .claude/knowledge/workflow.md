# Development workflow: plan → code → test → review

An iterative loop where each stage runs as an **isolated Claude Code subagent**. Kick it off
with `/workflow <task>`.

## The loop

```
/workflow "<task>"
      │
      ▼
  workflow-planner ──▶ docs/plans/<date>-<slug>.md
      │
      ▼
  workflow-coder ──▶ code + unit tests ◀──────────┐
      │                                            │ revise
      ▼                                            │ (≤3 per gate)
  workflow-tester ──▶ VERDICT: PASS | FAIL ────────┤
      │ PASS                                       │
      ▼                                            │
  workflow-reviewer ──▶ VERDICT: APPROVE | BLOCK ──┘
      │ APPROVE
      ▼
    DONE  (or STOP → surfaced to you if a gate exhausts its 3 retries)
```

The `/workflow` command (`.claude/commands/workflow.md`) is the orchestrator; the four agents
live in `.claude/agents/`.

## Why isolation removes bias

Each stage is a fresh subagent with a **clean context**. That separation is deliberate:

- The **tester never sees the coder's reasoning**, so it can't write tests that happen to
  match a bug. It derives expected behavior from the plan + `rules/domain-invariants.md`, then
  runs those expectations against the code. This is what catches a misread requirement.
- The **reviewer reviews the diff cold** — no exposure to the coder's rationalizations, so
  "it's fine because…" arguments never reach it.
- The **coder sees only the plan**, not the planner's deliberation, so it implements the spec
  as written rather than reconstructing intent.

Because contexts are isolated, agents can't share memory. State is handed off through
**artifacts only**:

| From → To | Artifact |
|---|---|
| planner → coder | the plan file in `docs/plans/` |
| coder → tester/reviewer | the working-tree diff (git) |
| tester → coder | `VERDICT:` + failing details (returned message; logged to run dir) |
| reviewer → coder | `VERDICT:` + findings (returned message; logged to run dir) |

## Loop safety

Each gate (test, review) allows at most **3 coder revisions**. On exhaustion the orchestrator
stops and hands you the current state rather than looping forever. Any stage may also stop
early to ask you a question it can't decide.

## Built on Superpowers

Each agent leans on the plugin rather than restating it: planner → `writing-plans`; coder →
`test-driven-development` (+ project skills `fastapi-template`, `python-design-patterns`);
tester → `test-driven-development` + `verification-before-completion`; reviewer →
`requesting-code-review`.

## Run artifacts

Transient reports are written under `.claude/workflow/runs/<run-id>/` (gitignored). The plan
in `docs/plans/` is durable and meant to be committed.
