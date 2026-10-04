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

| 6 | claude-a idle while T-002 (infra, Ready) waited behind codex-b's single busy slot; `rebalance` required equal-or-greater strength | `swarm status` 23:12 UTC | rebalance steals when the donor is saturated and the idle agent scores ≥ 3 (commit 273b3a6) |
| 7 | Workers had no channel to report harness/prompt/skill problems; orchestrator-only ownership of the harness was implicit | user request | `harness_feedback` in the report → `[harness]` fyi question + decision log; rule 10 in worker rules, AGENTS.md, ORCHESTRATOR.md, skill (dc85895) |
| 8 | No automatic record of which models each laptop can run | user request | runner publishes CLI version + tier map on first heartbeat; `swarm doctor --models` probes ids (ea4ee99) |
| 9 | `swarm plan` without a TTY aborts at the confirm prompt after spending the planner call; proposals are kept in `.swarm/tasks.proposed.json` | plan-M0.log | use `apply-proposals` (worked); idea: auto-detect non-TTY and skip the prompt with a notice |

| 10 | Both first reviews (T-001, T-002) failed `verify_full.sh` with "No module named ruff/numpy": the main checkout's `.venv` had become a symlink to itself (`.venv -> .git/../.venv`, 19:15 EDT), so the worktree links resolved to nothing and the scripts fell back to the swarm-control venv's python on PATH. Cause of the self-link not pinned down (template `setup_worktree.sh` links `$MAIN/.venv` when `.venv` is missing; something removed the venv first) | task feedback, `ls -la .venv` | venv recreated; `setup_worktree.sh` exits in the main checkout and never self-links; `scripts/_py.sh` resolves the interpreter (project venv → main venv → python3) and prints it; both workers told via `swarm tell` (c40702f). Harness idea: `doctor` should check that `.venv/bin/python` resolves and is not a symlink loop; the reviewer should log which interpreter verify used |
| 11 | `swarm tell` / worker `messages` only reach a task's **next** attempt (feedback is read when a run starts) | design | acceptable; the skill says so. Idea: for Running tasks, also append the message to the task's Notion page so a human sees it immediately |
| 12 | codex-b heartbeat gap (3 min) 20 min into its first task: laptop sleep or runner stop on laptop-b | `swarm status` 23:52 UTC | reminded the user; the reaper requeues after `heartbeat_stale_minutes` (10) |

| 13 | Root cause of #10 found by the T-002 worker via `harness_feedback`: `.gitignore` had `.venv/` only, which does not match a symlink; the harness's `git add -A` committed a worktree's `.venv` link in T-003's PR, and the next `git pull` in the main checkout replaced the real venv with it | Q-010 | project + template gitignore gain bare `.venv`; worktrees get `.venv`/`node_modules` in `info/exclude`; `run_script` strips swarm's venv from PATH; `setup_worktree` failures fail the attempt (runner) / escalate (reviewer); rebase feedback names the main commits that conflicted; Codex gets `--add-dir <main checkout>` so pip can write the shared venv (Q-003); `context7` dropped for workers (Q-001); verify_full tolerates pytest exit 5 (Q-002); "Needs you" hides harness/relay notes |
| 14 | The user merged PRs #1, #3, #4 by hand while the swarm was looping; T-001 stayed Running on a silent codex-b | GitHub timestamps 23:27-23:29Z | marked Done by hand; rule added to the skill |

## Ideas for the harness (not done)
- serve: when a task's PR is MERGED on GitHub but the task is not Done, mark it Done (handles human merges).
- `swarm doctor`: fail when `.venv/bin/python` (or the configured interpreter) does not resolve; warn when `.venv` is a symlink in the main checkout.
- reviewer: include the interpreter path and `pip freeze | head` in the verify_full failure finding so a bad environment is obvious at a glance.
- `swarm plan`: when stdin is not a TTY, print the table and exit 0 with "run `swarm apply-proposals`" instead of "Aborted".
- Worker → worker messages (`messages` in the report, `swarm tell`), plus an "Agent messages" view on the Questions database.
- `swarm init` idempotency (still deferred from the ledger).
- A `swarm plan --dry-run` that only prints the planner prompt size, for budgeting.

## Setup summary (Oct 4, 22:50 UTC)
- Harness: 1 fix shipped (family keys in `*_by_type`), skill `skills/swarm-control/SKILL.md` created and symlinked into `~/.claude/skills`, 9 field rules recorded.
- Project: repo + board + JOIN.md + plans + hearing-stack skill live; `swarm doctor` all yes on laptop-a; `swarm status` works (both agents awaiting heartbeat).
- Planner set to Fable on the user's request; reviewer claude-a/sonnet to protect the teammate's Codex credits.
- Not run yet: `swarm plan PLAN.md --milestone M0` (the user starts the run). Context resolution checked with `select_docs` for ml_audio/backend/eval/frontend/infra.

## Run summary
(filled in at the end of the run)
