# `.claude/` — agent guidance for this repo

Everything a Claude Code session needs to work on the LLM gateway. Start at
[`CLAUDE.md`](CLAUDE.md); it's auto-loaded (the root `CLAUDE.md` just imports this one).

## Layout

| Path | What it holds |
|---|---|
| `CLAUDE.md` | Entry point: what the project is, stack, repo layout, build order, commands. |
| `knowledge/` | Durable reference docs. `architecture.md` is the full V1 design spec. |
| `rules/` | Constraints to follow while coding. `domain-invariants.md` (must-not-regress behaviors) and `engineering.md` (style/structure). |
| `skills/` | Claude Code skills, invoked via the Skill tool. `fastapi-template` (project structure) and `python-design-patterns` (design principles). |
| `agents/` | The four isolated workflow subagents: `workflow-planner`, `workflow-coder`, `workflow-tester`, `workflow-reviewer`. |
| `commands/` | Slash commands. `/workflow <task>` orchestrates the plan → code → test → review loop. |
| `workflow/runs/` | Transient per-run reports (gitignored). Created at runtime. |

## Conventions

- **Knowledge vs. rules:** `knowledge/` explains *what and why* (design); `rules/`
  states *what you must/mustn't do* while implementing. Keep them separate.
- **Don't duplicate skills.** If a convention is captured in `skills/`, reference it from
  `rules/` rather than copying the prose — one source of truth.
- **Keep `CLAUDE.md` short.** It orients; depth lives in `knowledge/` and `rules/`.
- **Skills are real Claude Code skills.** Each lives in `skills/<name>/SKILL.md` with YAML
  frontmatter (`name`, `description`) and is invoked via the Skill tool — not linked as a
  plain doc.
- **Development runs through `/workflow`.** An isolated plan → code → test → review loop;
  the four stages are separate subagents so each is unbiased. See
  [`knowledge/workflow.md`](knowledge/workflow.md).
