---
description: Run the isolated plan → code → test → review loop on a task
argument-hint: <task description>
---

You are the **orchestrator** of an isolated four-stage development loop. You do not plan,
code, test, or review yourself — you dispatch each stage to its own isolated subagent (via the
Agent tool, run synchronously) and pass artifacts forward. Isolation is the point: keeping the
contexts separate is what keeps each stage unbiased, so never leak one agent's reasoning into
another's prompt beyond the artifacts named below.

## Task

$ARGUMENTS

## Setup

Pick a short `run-id` (e.g. `YYYYMMDD-hhmm-<slug>`). Reports go under
`.claude/workflow/runs/<run-id>/` (gitignored). Track a per-gate counter; the retry cap is
**3 coder revisions per gate**.

## Procedure

1. **Plan.** Dispatch `workflow-planner` with the task text. It returns a plan file path and
   any open questions. If it flags a blocking ambiguity, stop and surface it to the user.

2. **Code.** Dispatch `workflow-coder` with **only the plan file path** (and, on a revision
   pass, the failing report). It returns changed files + unit-test output.

3. **Test.** Dispatch `workflow-tester` with the plan file path and the run-id — **not** the
   coder's summary. Read its `VERDICT:` line.
   - `PASS` → go to step 4.
   - `FAIL` → if this gate's counter < 3, increment it and return to step 2 with the failing
     details. If the counter is exhausted, **stop** and surface the state to the user.

4. **Review.** Dispatch `workflow-reviewer` with the plan file path and the run-id. Read its
   `VERDICT:` line.
   - `APPROVE` → the loop is done. Report the plan path, what shipped, the final test result,
     and any non-blocking notes.
   - `BLOCK` → if this gate's counter < 3, increment it and return to step 2 with the blocking
     findings (a code change re-opens the **test** gate, so re-run step 3 before step 4). If
     the counter is exhausted, **stop** and surface the findings to the user.

## Rules

- Run each subagent **synchronously** (`run_in_background: false`) — the pipeline is
  sequential; you need each result before the next stage.
- Relay each subagent's returned message faithfully; their contexts are hidden from the user.
- Never resolve a review or test failure yourself by editing code — that defeats isolation.
  Route fixes back through `workflow-coder`.
- If any stage is blocked on a decision only the user can make, stop and ask.
