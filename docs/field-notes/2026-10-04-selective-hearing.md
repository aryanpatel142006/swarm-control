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

## Provider readiness follow-up
- Provider adapters can emit flags that an installed CLI no longer supports. Derive the preflight flags from the actual adapter command and compare with the local CLI's help; use `codex exec --help` for Codex. Run this preflight before live smoke checks and before the worker accesses the board.
- Doctor previously made its smoke call even after failed setup checks or with `--offline`. It now prints a skipped-smoke failure and leaves the model untouched.
- Successful CLI exit or arbitrary fallback-file existence is not proof of the requested response. Smoke now validates the exact current `done` / `smoke ok` report and removes stale fallback files.
- Custom CLI templates split after path substitution, breaking prompt and cwd arguments containing spaces. Split the template first, then substitute within arguments.
- Coverage includes all built-in providers' incompatible help, unknown/missing/custom CLIs, failed preflight, stale/malformed reports, offline operation, and paths with spaces. Validation: 271 passed, 1 skipped; live Codex flag preflight passed; combined setup/readiness checks passed and both patches combine cleanly. No live worker or global provider settings changed.

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
