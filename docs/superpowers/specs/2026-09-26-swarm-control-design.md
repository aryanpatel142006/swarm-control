# swarm-control — design spec

**Date:** 2026-09-26
**Purpose:** a small, reliable harness that lets two teammates run several AI coding agents (Claude Code, Codex, Antigravity/Gemini, Grok, or any CLI) in parallel across two laptops during HackRU Fall 2026 (Oct 10–11, 24 hours) to build the selective-hearing project, with Notion as the shared human-facing board and GitHub as the source of truth for code.

**One-line principle:** agents execute autonomously in fresh, bounded contexts; humans own intent through Notion; merged, verified code is the only proof of progress.

---

## 1. Goals and non-goals

### Goals

1. **Parallel progress without context blow-up.** Every task runs in a fresh CLI session capped by turns, wall time, and budget. No session ever grows into a multi-hour, million-token context.
2. **Best model for each task.** Tasks are typed (frontend, backend, ML audio, …) and rated by importance. A deterministic router picks the strongest available agent and the right model tier, so Fable and GPT-6 Astra are spent only where they matter.
3. **Two laptops, one board.** Both teammates' agents read and write the same Notion databases. GitHub branches and PRs carry the code.
4. **Humans steer, never babysit.** Agents proceed on reversible decisions and log them. High-impact choices and genuine blockers become Questions in Notion with a "proceeding with X meanwhile" field. Humans answer in Notion or in the orchestrator session.
5. **Independent review before merge.** A different model family reviews each significant PR against its acceptance criteria. Trivial, passing work merges without paying the review tax.
6. **Subscription-limit aware.** Per-provider concurrency caps, per-run usage logging, rate-limit detection with cooldown, and automatic rerouting of non-critical work.
7. **Rehearsed before the event.** Unit tests, an end-to-end fake-agent run, and a two-laptop rehearsal with a kill-and-recover drill.

### Non-goals

- Not a general multi-agent framework. No leases service, capacity controller, steering compiler, chronicler agent, or event-sourcing bus. Notion pages are the state; git is the code.
- Not an autonomous re-planner. Re-planning is a human decision made in the orchestrator session, informed by the status page.
- Not the hackathon project. It lives in its own public repo, dated before Oct 10, and is disclosed on the Devpost submission.

---

## 2. Architecture

```
  Laptop A (orchestrator host)                 Laptop B
  ┌───────────────────────────────┐            ┌──────────────────────────┐
  │ orchestrator session (human + │            │                          │
  │   Claude Opus 5.5, swarm CLI) │            │                          │
  │ swarm serve  (review, merge,  │            │                          │
  │   reaper, relay, status)      │            │                          │
  │ swarm run  (hosts claude-a,   │            │ swarm run (hosts codex-b,│
  │   codex-a as threads)         │            │   agy-b as threads)      │
  └──────────┬───────────┬────────┘            └──────────┬───────────────┘
             │           │                                │
             │     ┌─────▼────────────────────────────────▼─────┐
             │     │              NOTION (shared board)          │
             │     │  Tasks DB · Questions DB · Agents DB        │
             │     │  Boards: by Agent, by Status · Status page  │
             │     └─────────────────────────────────────────────┘
             │
       ┌─────▼──────────────────────────────────────────────────┐
       │                  GITHUB (project repo)                  │
       │  main · task/T-012 branches · PRs · docs/ · scripts/    │
       └────────────────────────────────────────────────────────┘
```

Three processes exist:

| Process | Where | What it does |
|---|---|---|
| `swarm run` | one per laptop; runs every agent configured for `SWARM_HOST`, each as a thread with its own worktree | worker loop: claim → worktree → prompt → run CLI → verify → push/PR → report |
| `swarm serve` | exactly one, on the orchestrator laptop | control loop: reaper, question relay, dependency promotion, review, merge, reroute, status page |
| orchestrator session | interactive Claude Code session on laptop A | humans + Opus 5.5 use `swarm plan/add/assign/cut/answer/status` to shape the task graph |

Notion is polled, not webhooked (no public endpoints needed). One `swarm run` process polls once per cycle for all agents on its host (15 s) and heartbeats once a minute; `serve` runs about five queries per 30 s cycle. Two laptops with six agents total stay under roughly 50 requests per minute against a 180 per minute limit, and idle hosts back off to 30 s.

---

## 3. Notion data model

Created by `swarm init --parent-page <id>` using Notion API version `2026-03-11` (`POST /v1/databases` with `initial_data_source`, then `POST /v1/views` for boards). Property `select` is used instead of `status` because it groups identically on boards and is simpler to write.

### 3.1 Tasks

| Property | Type | Notes |
|---|---|---|
| Name | title | `T-012 · Implement degraded tracking state` |
| ID | rich_text | `T-012`, generated by the tool, unique per project |
| Description | rich_text | what and why; the full body lives in the page |
| Acceptance | rich_text | observable criteria; copied into the prompt and the reviewer rubric |
| Type | select | `frontend, backend, realtime, ml_audio, ml_vision, ml_fusion, eval, tests, docs, research, bugfix, integration, infra` |
| Importance | select | `critical, high, normal, low` |
| Size | select | `S, M, L` |
| Milestone | select | `M0 skeleton, M1 vertical slice, M2 core, M3 polish, M4 demo` |
| Priority | number | lower runs first within an agent's column |
| Agent | select | options = agent names from config; `swarm agents sync` refreshes |
| Model | rich_text | resolved model id, e.g. `opus`, `gpt-6-sol` |
| Effort | rich_text | `low/medium/high/xhigh` |
| Status | select | `Backlog, Ready, Running, Review, Changes Requested, Merge Ready, Blocked, Failed, Done, Cut` |
| Depends On | rich_text | comma-separated task IDs |
| Scope | rich_text | glob list the worker may edit, e.g. `frontend/src/**` |
| Feedback | rich_text | what the next attempt must know: reviewer findings summary, verify failure tail, or the human's answer (≤ 2,000 chars; full text lives in the page body) |
| Branch | rich_text | `task/T-012` |
| PR | url | |
| Attempts | number | |
| Claim Nonce | rich_text | claim verification |
| Started / Updated | date | |
| Last Error | rich_text | tail of the last failure |

Page body: the task description, then one `## Report — attempt N` markdown section per attempt (written with `PATCH /v1/pages/{id}/markdown`), then `## Review — round N` sections.

Board views: **By Agent** (group by Agent) and **By Status** (group by Status). A third table view **Needs Human** filters `Status ∈ {Blocked, Failed}`.

### 3.2 Questions

| Property | Type | Notes |
|---|---|---|
| Question | title | |
| Kind | select | `blocking` (task waits) or `fyi` (task proceeds) |
| Context | rich_text | 2–5 lines from the agent |
| Options | rich_text | numbered options if any |
| Proceeding With | rich_text | the provisional decision (fyi only) |
| Impact | select | `high, medium, low` |
| Task | relation → Tasks | |
| Asked By | rich_text | agent name |
| Status | select | `Open, Applied` — set by the tool, never by humans |
| Answer | rich_text | humans type here; a non-empty Answer is what counts as answered |
| Needs Follow-up | checkbox | for `fyi` questions: tick it when your answer means the agent's provisional choice must be changed; the relay then creates a follow-up task |

Board view grouped by Status; the `Open` column is the "needs the developers" queue. Humans only ever touch `Answer` and `Needs Follow-up`.

### 3.3 Agents

| Property | Type |
|---|---|
| Name | title |
| Provider | select (`claude, codex, antigravity, gemini, grok, generic`) |
| Host | rich_text |
| Status | select (`idle, running, cooldown, offline`) |
| Current Task | rich_text |
| Last Heartbeat | date |
| Cooldown Until | date |
| Runs / Tokens In / Tokens Out / Cost USD | number |
| Note | rich_text |

### 3.4 Status page

One plain Notion page under the parent, rewritten every fifteen minutes by `swarm serve`: hours left, per-milestone done/remaining, blocked and failed lists, per-agent health and spend, risk lines.

### 3.5 Consistency rules

- Notion has no compare-and-set. Contention is avoided by design: each worker only claims tasks in its own Agent column. As a guard, a claim writes `Status=Running` plus a random `Claim Nonce`, sleeps 1.5 s, re-reads, and abandons the task if the nonce changed.
- All writes retry on `429`/`529` honoring `Retry-After`, with jitter, up to five times. A Notion outage stalls claiming and reporting but never corrupts git state; a worker mid-task finishes locally and retries publishing.

---

## 4. Configuration

`.swarm/config.yaml` lives in the **project repo** so both laptops share it. Secrets (`NOTION_TOKEN`, provider keys) live in each laptop's environment. `SWARM_HOST` names the laptop.

```yaml
project: selective-hearing
repo: aryan/selective-hearing          # GitHub owner/name
main_branch: main
worktree_root: ../selective-hearing-wt  # sibling dir, outside the repo

notion:
  parent_page_id: "..."
  tasks_db: "..."        # filled in by `swarm init`
  tasks_ds: "..."        # data source id
  questions_db: "..."
  agents_db: "..."
  status_page: "..."

event:
  start: "2026-10-10T12:00:00-04:00"
  end:   "2026-10-11T12:00:00-04:00"

poll_seconds: 15                  # per host, all agents in one query; 30 when idle
heartbeat_seconds: 60
serve_seconds: 30
review_policy: high_and_above     # all | high_and_above | critical_only | none
max_review_rounds: 2
max_attempts: 3
heartbeat_stale_minutes: 10
max_queue_depth: 4                # beyond this, non-critical tasks spill to next-best agent

verify:
  setup_worktree: scripts/setup_worktree.sh   # optional: link node_modules, venv
  fast: scripts/verify_fast.sh                # ≤ 60 s; run after every task
  full: scripts/verify_full.sh                # run by reviewer and before merge

task_limits:
  S: { turns: 30,  minutes: 20, budget_usd: 3 }
  M: { turns: 60,  minutes: 40, budget_usd: 8 }
  L: { turns: 120, minutes: 75, budget_usd: 20 }

hosts:
  aryan-mac:     { max_parallel: { claude: 3, codex: 3, antigravity: 2, grok: 1, generic: 2 } }
  friend-laptop: { max_parallel: { claude: 2, codex: 3, antigravity: 2 } }

routing:
  importance_to_tier: { critical: best, high: high, normal: mid, low: low }
  best_tier_only_when: { importance: critical }         # 'best' never used below critical
  type_model_overrides:                                  # evidence-based exceptions
    claude: { frontend: { best: opus } }                 # Opus 5.5 leads WebDev Arena; Fable is 3rd

docs_by_type:                     # what gets inlined into the prompt (paths, optional #section)
  _all:        [PLAN.md#summary, AGENTS.md]
  frontend:    [docs/DESIGN.md, docs/CONTRACTS.md]
  backend:     [docs/CONTRACTS.md, docs/ARCHITECTURE.md]
  realtime:    [docs/CONTRACTS.md, docs/ARCHITECTURE.md]
  ml_audio:    [docs/ARCHITECTURE.md#ml, docs/CONTRACTS.md#events]
  ml_vision:   [docs/ARCHITECTURE.md#ml, docs/CONTRACTS.md#events]
  ml_fusion:   [docs/ARCHITECTURE.md#ml, docs/CONTRACTS.md#events]
  integration: [docs/ARCHITECTURE.md, docs/CONTRACTS.md]
  eval:        [docs/ARCHITECTURE.md#ml]
  docs:        [docs/ARCHITECTURE.md, docs/DESIGN.md]

agents:
  claude-aryan:
    provider: claude
    host: aryan-mac
    parallel: 2                     # concurrent tasks for this agent, bounded by the host cap
    models: { best: fable, high: opus, mid: sonnet, low: haiku }
    effort: { best: high, high: high, mid: medium, low: low }
    strengths: { frontend: 5, backend: 4, realtime: 4, ml_audio: 4, ml_vision: 4, ml_fusion: 4,
                 eval: 4, tests: 4, docs: 5, research: 5, bugfix: 5, integration: 5, infra: 4 }
    soft_cap_5h_usd: 40            # router avoids this agent for non-critical work above 80 %
  codex-aryan:
    provider: codex
    host: aryan-mac
    models: { best: gpt-6-astra, high: gpt-6-astra, mid: gpt-6-sol, low: gpt-6-luna }
    effort: { best: xhigh, high: high, mid: medium, low: low }
    strengths: { frontend: 3, backend: 5, realtime: 5, ml_audio: 3, ml_vision: 3, ml_fusion: 3,
                 eval: 4, tests: 5, docs: 3, research: 3, bugfix: 4, integration: 4, infra: 5 }
    sandbox: workspace-write        # or danger-full-access if tests need network
  agy-friend:
    provider: antigravity
    host: friend-laptop
    models: { best: gemini-3.1-pro, high: gemini-3.1-pro, mid: gemini-3.8-flash, low: gemini-3.8-flash }
    strengths: { frontend: 3, backend: 3, realtime: 2, ml_audio: 3, ml_vision: 4, ml_fusion: 3,
                 eval: 4, tests: 3, docs: 4, research: 4, bugfix: 3, integration: 2, infra: 3 }
    experimental: true              # headless flags verified only at rehearsal

reviewer:
  agent: codex-aryan               # a different family than most authors
  model: gpt-6-sol
  effort: medium
planner:
  agent: claude-aryan
  model: opus
  effort: high
```

Default strength scores are shipped with a comment citing the evidence (WebDev Arena Sep 25 2026, Terminal-Bench 2.1, MLE-bench, practitioner reviews). They are opinions to be edited, not facts.

---

## 5. Routing

Input: a task with `type`, `importance`, `size`, optional `scope`. Output: `agent`, `model`, `effort`.

1. **Candidates** = agents whose provider is not in cooldown (ignored for `critical`), whose host has capacity, and whose 5-hour spend is below 80 % of `soft_cap_5h_usd` (ignored for `critical` and `high`).
2. **Score** = `strengths[type]`. Take the maximum. Ties break by shortest queue (Ready + Running count), then by cheaper provider.
3. **Spill**: if the winner's queue depth exceeds `max_queue_depth` and importance ≤ `high`, pick the best agent with score ≥ winner − 1.
4. **Scope affinity**: if another Ready/Running task has an overlapping `scope`, prefer its agent (fewer merge conflicts).
5. **Tier** = `importance_to_tier[importance]`, then apply `best_tier_only_when` and `type_model_overrides`. `model = agent.models[tier]`, `effort = agent.effort[tier]`.

Rerouting: `swarm reroute` (and every serve cycle) re-runs steps 1–5 for Ready tasks whose agent is offline (stale heartbeat), in cooldown, or over its soft cap. Manual override: `swarm assign T-012 --agent codex-aryan --model gpt-6-astra`, or drag the card in Notion; the runner respects whatever the card says.

---

## 6. Worker loop (`swarm run`)

One process per laptop. It reads `SWARM_HOST`, starts one worker thread per `parallel` slot of every agent on that host, and shares one poller. `--agent NAME` restricts to one agent; `--once` runs a single task; `--dry-run` compiles the prompt and stops.

```
poller (every poll_seconds):
  heartbeat() once per heartbeat_seconds        # Agents DB: status, current tasks
  tasks = query(Agent ∈ my agents, Status ∈ {Ready, Changes Requested}, sort Priority)
  hand each task to a free slot of its agent, respecting host max_parallel per provider

worker slot:
  if not claim(task): return                     # Running + nonce; sleep 1.5 s; re-read
  wt = workspace.provision(task)                 # git fetch; worktree add
                                                 #   Changes Requested / recovered: reuse origin/task/T-012
                                                 #   fresh or after Failed: -B task/T-012 origin/main
  run setup_worktree.sh if present
  prompt = compile_prompt(task, wt)              # §7
  result = adapter.run(prompt, model, effort, limits, cwd=wt)   # §8
  report = parse_report(result, wt)              # structured output → .swarm/report.json → synthesized
  scope_check(wt, task.scope)                    # out-of-scope files → flagged; forces review
  verify = run(verify.fast, cwd=wt)              # always, even if the model claims it ran tests
  git add -A && commit "T-012: <summary>" (if dirty); git push -u origin task/T-012
  pr = gh pr create/edit (title, body = report summary + verify tail)
  publish(task, report, verify, pr)              # §6.2
  workspace.dispose(wt)                          # branch stays
```

### 6.1 Prompt limits by size

`task_limits[size]` sets `--max-turns`, a wall-clock timeout (SIGTERM then SIGKILL), and a budget where the CLI supports it. Timeout or CLI error → `Failed`, `Attempts += 1`.

### 6.2 Publish outcomes

| Report status | Verify | Result |
|---|---|---|
| `done` | pass | `Status=Review` (or `Merge Ready` if review policy skips it) |
| `done` | fail | `Status=Changes Requested`, feedback = verify tail; same worker retries (counts as a review round) |
| `blocked` | any | create Question (`blocking`), `Status=Blocked` |
| `failed` / no report / timeout | any | `Status=Failed`, `Last Error` set |
| any, with `question` of kind `fyi` | any | create Question (`fyi`) and continue with the row above |

Retry ladder for `Failed`: attempt 1 same agent and model; attempt 2 escalates one tier (and a different agent if one scores within 1); attempt 3 files a `blocking` Question and stops. Agents with `experimental: true` count timeouts as attempt 3 immediately, to avoid burning the clock on a broken adapter.

### 6.3 Heartbeat and recovery

Every cycle the worker writes `Last Heartbeat` and `Current Task`. `swarm serve` returns any `Running` task whose agent's heartbeat is older than `heartbeat_stale_minutes` to `Ready` and increments `Attempts`; the branch, if pushed, is reused by the next attempt. Killing a runner mid-task therefore loses at most the uncommitted work of one task.

---

## 7. Prompt compiler

The prompt is assembled per task, in this order, and kept small (target 6–12k tokens):

1. **Role and rules** (from `AGENTS.md`, ~600 tokens): you are one worker among several; work only inside `Scope`; commit as you go; run `scripts/verify_fast.sh` before finishing; never leave a mock, hardcode, or skipped test unlisted; if a decision changes the product or a contract, ask (see below); finish by producing the report.
2. **Task card**: ID, title, description, acceptance criteria, scope, size, milestone, dependencies with their one-line summaries.
3. **Feedback**: reviewer's change list, or the human's answer to the linked question, or the verify failure tail (on retry).
4. **Docs**: files from `docs_by_type[_all] + docs_by_type[type]`, optionally a single `#section`. Everything else is referenced by path for the agent to read on demand.
5. **Report contract**: the JSON schema below. Claude receives it via `--json-schema`; Codex via `--output-schema`; other adapters are told to write `.swarm/report.json`. The runner accepts either.

```json
{
  "status": "done | blocked | failed",
  "summary": "2–4 sentences, what changed and why",
  "files_changed": ["frontend/src/..."],
  "tests": { "command": "...", "passed": true, "output_tail": "..." },
  "debts": [ { "kind": "mock|hardcode|todo|skipped_test|assumption|fallback",
               "location": "path:line", "reason": "...", "fix": "..." } ],
  "decisions": [ { "decision": "...", "why": "...", "impact": "high|medium|low" } ],
  "question": { "kind": "blocking|fyi", "text": "...", "options": ["..."], "proceeding_with": "..." },
  "notes_for_reviewer": "..."
}
```

A missing or malformed report is synthesized from `git diff --stat` and the verify output and flagged `report: missing` so the reviewer looks harder.

---

## 8. Provider adapters

One class per provider with a single interface:

```python
class Adapter(Protocol):
    def run(self, prompt_file: Path, *, model: str, effort: str | None,
            max_turns: int, budget_usd: float | None, timeout_s: int,
            cwd: Path, schema: dict | None) -> RunResult
    # RunResult: ok, exit_code, stdout, stderr, structured_output | None,
    #            usage {input_tokens, output_tokens, cost_usd | None},
    #            session_id | None, rate_limited: bool, reset_at | None
```

Exact invocations (verified against official docs on 2026-09-26 unless marked):

| Provider | Command |
|---|---|
| claude | `claude -p --output-format json --permission-mode bypassPermissions --model {model} --effort {effort} --max-turns {n} --max-budget-usd {b} --append-system-prompt-file rules.md --json-schema '{schema}' < prompt.md` in `cwd`. Parse `result`, `subtype`, `structured_output`, `total_cost_usd`, `usage`, `session_id`, `is_error`. |
| codex | `codex exec --json -C {cwd} -m {model} -c model_reasoning_effort="{effort}" --sandbox {workspace-write} -c approval_policy=never --output-schema schema.json -o last.txt - < prompt.md`. Parse JSONL: `thread.started.thread_id`, `turn.completed.usage`, `item.completed(agent_message)`, `turn.failed`/`error`. Cost is tokens only. |
| antigravity (**experimental**) | `agy -p "$(cat prompt.md)" --model={model} --approval-mode yolo --output-format json` in `cwd`. Flags are third-party sourced; `swarm doctor --agent` must pass at rehearsal or the agent is disabled. Report via file. |
| gemini (API key, legacy) | `gemini -p --output-format json --approval-mode yolo -m {model}` with prompt on stdin. Report via file. |
| grok | `grok -p --prompt-file prompt.md --output-format json --always-approve --max-turns {n} --cwd {cwd} --model {model}`. Parse `text`, `usage`, `total_cost_usd`. Report via file. |
| generic | `command_template` from config with `{prompt_file} {model} {cwd}` placeholders. Report via file only. Covers Copilot CLI, Cursor CLI, Muse, or anything else. |

Rate-limit detection: exit code, `is_error`, error events, or a regex over stderr/stdout (`rate.?limit|429|usage limit|hit your .* limit`). On detection the run is marked `rate_limited`, the task returns to `Ready` without consuming an attempt, and the provider enters cooldown (`reset_at` if parseable, else 15 minutes) recorded in the Agents DB so both laptops see it.

Network: workers never need network inside the CLI. The **runner** pushes and opens PRs, so Codex can stay in `workspace-write` sandbox and Claude needs no extra permissions beyond the worktree.

---

## 9. Control loop (`swarm serve`)

Every `serve_seconds`, in this order:

1. **Reaper**: stale `Running` → `Ready` (§6.3). Agents with stale heartbeat → `offline`.
2. **Question relay**: Questions with a non-empty `Answer` and `Status=Open`: if `blocking`, write the answer into the task's `Feedback` and set it `Ready`; if `fyi` and `Needs Follow-up` is ticked, create a follow-up task (`type=bugfix`, importance from the question's impact, `Feedback` = the answer) and route it. Mark the question `Applied`.
3. **Dependency promotion**: `Backlog` tasks whose `Depends On` are all `Done` → `Ready`, routed.
4. **Review** (§10) for tasks in `Review`, bounded by reviewer concurrency of 1.
5. **Merge** (§11) for tasks in `Merge Ready`, priority order, one at a time.
6. **Reroute** Ready tasks whose agent is offline, in cooldown, or over its soft cap.
7. **Status page** (§3.4) every fifteen minutes, plus a one-line terminal summary every cycle.

Only one `serve` may run. It takes a lock row in the Agents DB (`Name=serve`, heartbeat); a second instance refuses to start unless the heartbeat is stale.

---

## 10. Reviewer

Policy gate first: `review_policy` decides whether a task is reviewed at all. Defaults: `high_and_above` reviews `critical` and `high`; everything else that passes `verify.fast` goes straight to `Merge Ready`. A task whose diff touches `docs/CONTRACTS.md` or `docs/DESIGN.md` is always reviewed.

Review run:

1. Worktree of the branch, `verify.full` (deterministic checks first; a failure is a `Changes Requested` without spending model tokens).
2. Reviewer adapter in **read-only** mode (Claude `--permission-mode plan` with `--allowedTools Read,Grep,Glob,Bash(git diff*)`; Codex `--sandbox read-only`) receives: acceptance criteria, the diff, the worker's report, verify output. Output schema: `{ "verdict": "approve|request_changes|escalate", "findings": [ {"severity", "file", "line", "issue", "fix"} ], "summary" }`.
3. `approve` → `Merge Ready`. `request_changes` → `Changes Requested` with findings written as `## Review — round N` on the card; the same worker picks it up. After `max_review_rounds` → `escalate`. `escalate` → `blocking` Question, `Status=Blocked`.

The reviewer is by default a different model family than the author (config `reviewer.agent`); if only one family is available, the reviewer still runs with a different model.

---

## 11. Merge

For each `Merge Ready` task in priority order: fresh worktree of the branch, `git rebase origin/main`; on conflict → `Changes Requested` with the conflict list in `Feedback` (the worker resolves in its next attempt). Then `verify.fast` on the rebased tree, `git push --force-with-lease`, then `gh pr merge --squash --delete-branch`, `Status=Done`. Dependency promotion runs immediately after a merge. The task's `decisions[]` and `debts[]` are written by the worker step as `docs/decisions/<task id>.md` and `docs/debt/<task id>.md` on the task branch, so they merge with the PR and never conflict across parallel tasks.

`swarm merge --auto` is the default inside `serve`; `swarm merge --manual` lists Merge Ready PRs and merges only those the human picks, for the last hours when a broken main is unacceptable.

---

## 12. Orchestrator commands

| Command | What it does |
|---|---|
| `swarm doctor` | checks `NOTION_TOKEN` (`GET /v1/users/me`), DB ids, `gh auth status`, each configured provider CLI on this host (`--version` and a 1-turn smoke prompt), git remote, verify scripts executable. Prints a pass/fail table. |
| `swarm init --parent-page ID` | creates the three databases, board views, status page; writes ids into `config.yaml`. Idempotent. |
| `swarm agents sync` | refreshes the Agent select options and Agents DB rows from config. |
| `swarm plan PLAN.md [--milestone M1]` | runs the planner adapter (read-only tools) with a decomposition prompt and a tasks JSON schema; writes `tasks.proposed.json`; prints a table; `--apply` (or interactive `y`) creates the tasks in Notion, routed. Plans the named milestone in detail and later ones coarsely. Existing tasks are passed in to avoid duplicates. A conflict lint warns when two proposed tasks share `Scope` without a dependency between them. |
| `swarm add "title" --type backend --importance high --size M [--depends T-003] [--scope "backend/**"]` | one task, routed |
| `swarm assign T-012 --agent X [--model M --effort E]` | override routing |
| `swarm cut T-012` / `swarm split T-012` | cut a task, or ask the planner to split it into smaller ones |
| `swarm answer Q-004 "use tap"` | same as typing in Notion |
| `swarm status [--watch]` | the status page content in the terminal |
| `swarm reroute` | re-run routing for Ready tasks |
| `swarm run [--agent NAME] [--once] [--dry-run]` | worker loop for every agent on this host |
| `swarm serve [--no-merge] [--no-review]` | control loop |
| `swarm logs T-012 [--attempt N]` | prompt, stdout, stderr, report for a run |

The orchestrator session is a normal Claude Code session opened in the project repo. `swarm-control/ORCHESTRATOR.md` (imported by the project's `CLAUDE.md`) tells it how to plan a milestone, read the status page, decide re-plans, and answer questions. It never reads code.

---

## 13. Project template (`hackathon-base`)

A GitHub template repo the team instantiates at kickoff (all project code is written during the event; only structure and empty docs pre-exist):

```
PLAN.md                       # product, demo story, milestones, success criteria (orchestrator owns)
AGENTS.md                     # worker rules (Codex, Antigravity, Grok read natively)
CLAUDE.md                     # "@AGENTS.md" import + ORCHESTRATOR.md import
GEMINI.md                     # "@AGENTS.md"
docs/ARCHITECTURE.md          # components, data flow, how to run, ML section
docs/DESIGN.md                # UX principles, tokens, UI state machine, component list, screenshots
docs/CONTRACTS.md             # API endpoints, WebSocket events, JSON schemas
docs/DECISIONS.md             # append-only, written by merge
docs/DEBT.md                  # append-only, written by merge
scripts/setup_worktree.sh     # link deps into a worktree (pnpm store, shared venv)
scripts/verify_fast.sh        # lint + unit + smoke, ≤ 60 s
scripts/verify_full.sh        # + integration + playwright, minutes
.swarm/config.yaml
.github/workflows/verify.yml  # runs verify_fast on PRs (belt and braces; the runner runs it too)
frontend/ backend/ ml/ eval/ artifacts/   # empty, with READMEs
```

Ownership by task type, enforced by `Scope` and the reviewer: frontend tasks may edit `docs/DESIGN.md`; backend tasks may edit `docs/CONTRACTS.md` and must list affected consumers in the report; only the orchestrator edits `PLAN.md`.

---

## 14. Budget policy

The reason the system exists.

- **Fresh context per task**, capped by size. No task may exceed `task_limits`.
- **Tier by importance.** `best` (Fable, GPT-6 Astra) is used only for `critical`. `high` uses Opus 5.5 / Astra. `normal` uses Sonnet 5 / GPT-6 Sol / Gemini 3.8 Flash. `low` uses Haiku / Luna / Flash.
- **Evidence-based exceptions.** Opus 5.5 outranks Fable on frontend, so critical frontend runs Opus. Fable is reserved for critical, large, agentic tasks (long multi-file ML or integration work) where it leads.
- **Concurrency caps** per provider per host. Claude subscriptions have been observed to start returning 429 around six concurrent sessions; default cap is three per laptop.
- **Usage ledger.** Every run appends `{agent, model, task, tokens_in, tokens_out, cost_usd, duration}` to `~/.swarm/<project>/usage.jsonl` and to the Agents DB. `swarm status` shows per-agent 5-hour spend.
- **Soft caps.** Above 80 % of `soft_cap_5h_usd`, an agent stops receiving `normal`/`low` work.
- **Cooldown and reroute.** A rate-limit error puts the provider in cooldown and reroutes non-critical Ready work automatically. Critical work waits for the best agent unless another scores within one point.
- **Cheap orchestration.** The orchestrator session uses Opus 5.5 and never reads code. The planner is one structured call per milestone. The reviewer runs GPT-6 Sol and only on `high`+ tasks by default.
- **Time-aware defaults** the humans switch on the day: at T-4h set `review_policy: all` and `merge --manual`; at T-2h stop `swarm plan` and only `swarm add` bugfixes.

---

## 15. Failure handling summary

| Failure | Detection | Response |
|---|---|---|
| Worker process dies | stale heartbeat | task → Ready, attempts +1, branch reused |
| CLI errors / timeout | exit code / timer | Failed → retry ladder (§6.2) |
| Rate limit | parsed error | cooldown + reroute, no attempt consumed |
| Verify fails | non-zero exit | Changes Requested with tail; same worker |
| Rebase conflict | git exit | Changes Requested with conflict list |
| Notion down | HTTP errors after retries | worker finishes locally, retries publish; serve skips cycle |
| Provider CLI missing on host | doctor / spawn error | agent marked offline; reroute |
| Report missing | parser | synthesized report, flagged, reviewed regardless of policy |
| Two `serve` instances | lock row | second refuses |
| Bad task (unroutable type) | router | assigned to top-scoring agent with a note; never left unassigned |

---

## 16. Security and blast radius

- Worker CLIs run with full tool permission **inside a worktree of the project repo only**. Never run `swarm run` from a home directory or with credentials in the tree.
- Secrets are environment variables; `.env` files are gitignored; the prompt never includes them.
- Codex keeps `workspace-write` sandbox by default; Claude's `bypassPermissions` is accepted for a hackathon.
- The runner, not the model, pushes and merges, using the human's `gh` auth.
- Nothing in the harness deletes branches except `gh pr merge --delete-branch` after a successful merge.

---

## 17. Repository layout (`swarm-control`)

```
swarm-control/
├── pyproject.toml                 # python ≥ 3.11; deps: httpx, pyyaml, typer, rich
├── swarm/
│   ├── cli.py                     # typer app: all commands in §12
│   ├── config.py                  # load/validate config.yaml, hosts, agents
│   ├── models.py                  # Task, Question, AgentRow, Report, RunResult dataclasses
│   ├── board/
│   │   ├── base.py                # Board protocol
│   │   ├── notion.py              # Notion implementation (httpx, retries, markdown reports)
│   │   └── memory.py              # in-memory implementation for tests
│   ├── router.py                  # §5
│   ├── prompt.py                  # §7
│   ├── report.py                  # parse/synthesize reports, JSON schema
│   ├── workspace.py               # git worktrees, push, gh pr
│   ├── adapters/                  # §8: base.py claude.py codex.py antigravity.py gemini.py grok.py generic.py
│   ├── runner.py                  # §6
│   ├── serve.py                   # §9
│   ├── reviewer.py                # §10
│   ├── merge.py                   # §11
│   ├── planner.py                 # swarm plan / split
│   ├── usage.py                   # ledger, soft caps, cooldowns
│   └── status.py                  # status page rendering
├── prompts/                       # rules.md, planner.md, reviewer.md
├── template/                      # hackathon-base contents (§13)
├── ORCHESTRATOR.md
├── tests/                         # pytest; fake_agent.sh; tmp git repos
├── docs/
│   ├── RUNBOOK.md                 # game-day, minute by minute
│   └── superpowers/specs/…        # this file
└── README.md
```

Target size: about 3,000 lines of Python plus tests. If it grows past 5,000, something is wrong.

---

## 18. Testing and rehearsal

**Unit** (pytest, no network): config validation; router (every rule in §5 with fixtures); prompt compiler (token budget, doc selection); report parser (schema, file fallback, synthesized); each adapter's command construction and output parsing against recorded sample outputs; Notion client request building and retry logic against a mocked transport; workspace on a real temporary git repo (worktree add/dispose, push to a bare remote).

**End-to-end** (local, no Notion): `InMemoryBoard`, a bare git remote in a temp dir, and `tests/fake_agent.sh` as a `generic` adapter that edits a file and writes a report. Six tasks with dependencies across two fake agents: all reach Done, decisions log written, one task set to `blocked` produces a Question, one fake failure exercises the retry ladder, a stale heartbeat is reaped.

**Notion integration** (opt-in, needs a token): `swarm init` on a throwaway page, create/claim/report/query round trip, board views exist, 429 retry path with a forced small burst.

**Rehearsal** (before Oct 10, both laptops, real CLIs, a toy project from the template): `swarm doctor` green on both hosts; ten tasks over three agents; kill one runner mid-task and confirm reaping and recovery; force a review round; confirm nothing merges twice; measure tokens per task size and adjust `task_limits` and soft caps.

**Acceptance for "ready for the event"**: every item above passes, and the runbook has been executed once end to end in under 90 minutes from kickoff to first merged PR.

---

## 19. Game day (summary; full runbook in `docs/RUNBOOK.md`)

| When | Who | What |
|---|---|---|
| Before | both | `swarm doctor` green on both laptops; Notion page shared; template repo ready; `gh auth` done |
| 0:00–0:30 | humans + orchestrator | write `PLAN.md` (product, demo story, milestones), `docs/DESIGN.md` principles, `docs/CONTRACTS.md` first events |
| 0:30–0:45 | orchestrator | `swarm plan PLAN.md --milestone M1`; review, apply |
| 0:45 | laptop A | `caffeinate -i swarm serve` (laptop A must never sleep); both laptops start `swarm run` |
| 0:45–1:30 | humans | build nothing by hand; watch Questions, answer fast; fix the first verify script issues |
| hourly | orchestrator | `swarm status`; plan the next milestone when the current one is 70 % done |
| T-4h | humans | `review_policy: all`, `merge --manual`; only bugfix/polish tasks |
| T-2h | humans | freeze; demo rehearsal; presentation from `docs/DECISIONS.md` |

---

## 20. Known risks and open items

1. **Antigravity headless flags are unverified.** The adapter is marked experimental and disabled by `doctor` if the smoke test fails. Gemini via API key is the fallback.
2. **Notion API on a free workspace is unverified.** `swarm doctor` tests it first. If it fails, upgrade one workspace to Plus for a month; a GitHub-Issues board backend is a possible second implementation of the `Board` protocol but is out of scope unless this fails.
3. **Free workspace 1,000-block cap with two members.** Use one owner's workspace with the teammate as a page guest; keep reports concise (one markdown block per attempt).
4. **Claude concurrency limits are undocumented.** Caps are configuration; the rehearsal measures the real ceiling.
5. **Model names drift.** All model ids live in config; `doctor` checks that each resolves.
6. **The template must not contain project code.** Only structure, docs skeletons, and scripts, so the HackRU "built during the event" rule is respected.
