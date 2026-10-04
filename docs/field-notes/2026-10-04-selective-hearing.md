# Field notes: selective-hearing setup (Oct 4, 2026)

Orchestrator session on laptop-a (Opus, then Fable). Project repo: github.com/aryanpatel142006/selective-hearing (private). Board: "Swarm · selective-hearing" under the user's Swarm Test page.

## Findings
| # | What happened | Evidence | Fix |
|---|---|---|---|
| 1 | `skills_by_type` / `mcp_by_type` / `docs_by_type` in the template used the key `ml`, but task types are `ml_audio` / `ml_vision` / `ml_fusion`; ML workers got no HF skills and no ARCHITECTURE#ml context in every run so far | template config vs `swarm/config.py` `skills_for` | `by_type()` family lookup in config.py + prompt.py, test added, commit 52afef2 |
| 2 | `docs_by_type` section refs are matched against the heading text, so `PLAN.md#ground-rules` silently returns nothing | `swarm/prompt.py::_section` | config uses `PLAN.md#Ground rules`; rule added to the skill. Idea: warn in `swarm doctor` when a configured ref resolves to nothing |
| 3 | Nothing loads `~/.swarm/env`; every new shell needs `source ~/.swarm/env` | `swarm doctor` NOTION_TOKEN not set | documented in the skill; idea: `swarm` could read `~/.swarm/env` itself when NOTION_TOKEN is unset |
| 4 | `gh repo create --source . --push` failed to push (exit 1) right after creating the repo; a plain `git push -u origin main` a second later succeeded | repo creation step | transient; note only |
| 5 | Codex reviewer would spend the teammate's limited credits on reviews | config design | reviewer = claude-a (sonnet); codex-b parallel 1, mid model for normal work |

## Ideas for the harness (not done)
- `swarm doctor`: validate every `docs_by_type` ref resolves to a non-empty section; validate `*_by_type` keys are task types or families.
- `swarm` auto-sources `~/.swarm/env` when `NOTION_TOKEN` is missing.
- `swarm init` idempotency (still deferred from the ledger).
- A `swarm plan --dry-run` that only prints the planner prompt size, for budgeting.

## Setup summary (Oct 4, 22:50 UTC)
- Harness: 1 fix shipped (family keys in `*_by_type`), skill `skills/swarm-control/SKILL.md` created and symlinked into `~/.claude/skills`, 9 field rules recorded.
- Project: repo + board + JOIN.md + plans + hearing-stack skill live; `swarm doctor` all yes on laptop-a; `swarm status` works (both agents awaiting heartbeat).
- Planner set to Fable on the user's request; reviewer claude-a/sonnet to protect the teammate's Codex credits.
- Not run yet: `swarm plan PLAN.md --milestone M0` (the user starts the run). Context resolution checked with `select_docs` for ml_audio/backend/eval/frontend/infra.

## Run summary
(filled in at the end of the run)
