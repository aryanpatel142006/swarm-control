# Field notes: selective-hearing setup (Oct 4, 2026)

Orchestrator session on laptop-a (Opus, then Fable). Project repo: github.com/aryanpatel142006/selective-hearing (private). Board: "Swarm · selective-hearing" under the user's Swarm Test page.

## Findings
| # | What happened | Evidence | Fix |
|---|---|---|---|
| 1 | `skills_by_type` / `mcp_by_type` / `docs_by_type` in the template used the key `ml`, but task types are `ml_audio` / `ml_vision` / `ml_fusion`; ML workers got no HF skills and no ARCHITECTURE#ml context in every run so far | template config vs `swarm/config.py` `skills_for` | `by_type()` family lookup in config.py + prompt.py, test added, commit 52afef2 |
| 2 | `docs_by_type` section refs are matched against the heading text, so `PLAN.md#ground-rules` silently returns nothing | `swarm/prompt.py::_section` | config uses `PLAN.md#Ground rules`; `swarm doctor` now has `docs refs` and `type keys` checks (`doctor.context_checks`) |
| 3 | Nothing loads `~/.swarm/env`; every new shell needs `source ~/.swarm/env` | `swarm doctor` NOTION_TOKEN not set | `swarm/env.py` loads it at CLI start when keys are missing (shell values win) |
| 4 | `gh repo create --source . --push` failed to push (exit 1) right after creating the repo; a plain `git push -u origin main` a second later succeeded | repo creation step | transient; note only |
| 5 | Codex reviewer would spend the teammate's limited credits on reviews | config design | reviewer = claude-a (sonnet); codex-b parallel 1, mid model for normal work |

## Ideas for the harness (not done)
- `swarm init` idempotency (still deferred from the ledger).
- A `swarm plan --dry-run` that only prints the planner prompt size, for budgeting.

## Setup summary (Oct 4, 22:50 UTC)
- Harness: 1 fix shipped (family keys in `*_by_type`), skill `skills/swarm-control/SKILL.md` created and symlinked into `~/.claude/skills`, 9 field rules recorded.
- Project: repo + board + JOIN.md + plans + hearing-stack skill live; `swarm doctor` all yes on laptop-a; `swarm status` works (both agents awaiting heartbeat).
- Planner set to Fable on the user's request; reviewer claude-a/sonnet to protect the teammate's Codex credits.
- Not run yet: `swarm plan PLAN.md --milestone M0` (the user starts the run). Context resolution checked with `select_docs` for ml_audio/backend/eval/frontend/infra.

## Run summary
(filled in at the end of the run)

## Laptop-b setup follow-up
- A fresh `swarm doctor --smoke codex-b` needed manual Git initialization, and Codex rejected the unnormalized report schema. Normalize schemas in the adapter (closed objects, all keys required, null for optional values) and initialize the disposable smoke checkout before making the model call.
- Retro discovered Claude MCP servers only; permanent Codex registration was invisible to tool promotion. Discover Codex registrations as well, including disabled servers that the runner enables per task.
- Keep the end-to-end retro test's project config explicit about its usable Playwright server; verify Codex-only discovery through a local CLI fixture. Plugin-install tests declare their CLI availability instead of requiring Claude to be installed on a Codex laptop.
- Smoke checks now discard stale fallback reports and inherit worker extra arguments. No live runner or global MCP configuration is changed by this PR.
- Validation: full suite passed (254 passed, 1 skipped); permanently registered Context7 and Playwright names were discovered on laptop-b.
