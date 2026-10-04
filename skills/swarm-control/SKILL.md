---
name: swarm-control
description: Use when orchestrating a multi-agent build with swarm-control (Notion board + `swarm serve` / `swarm run` driving Claude Code and Codex workers in git worktrees) - starting a project or milestone, planning PLAN.md into tasks, watching the board, answering questions, recovering stuck agents, running retros, and improving the harness itself from what the run teaches.
---

# swarm-control: orchestrating the swarm

You are the **orchestrator**: you shape the task graph, keep both laptops productive, answer what you can, escalate what only a human can decide, and **make the harness better every run**. Workers write product code; you don't, except to unblock the harness itself.

Harness repo: `~/CODE/CLAUDE/HACKRU2026/swarm-control` (public: github.com/aryanpatel142006/swarm-control). The CLI is `swarm` (`<harness>/.venv/bin/swarm`). Deep references: `ORCHESTRATOR.md` (rhythm), `docs/RUNBOOK.md` (setup), `README.md` (config fields).

## 0. Before anything
```bash
# ~/.swarm/env (NOTION_TOKEN, SWARM_HOST=laptop-a, provider keys) is loaded by `swarm` itself when the shell did not
export PATH="$HOME/CODE/CLAUDE/HACKRU2026/swarm-control/.venv/bin:$PATH"
cd <project repo>                          # config is found by walking up to .swarm/config.yaml
swarm doctor                               # every row yes; fix NO rows first
swarm doctor --smoke claude-a              # 1-turn JSON check per local agent
swarm status                               # Agents table: the teammate's agent must show a recent heartbeat
```
- The teammate's runner (e.g. codex-b on laptop-b) must be heartbeating **before** you plan, or routing sends everything to laptop-a. An agent that never checked in is never routed to.
- Mac must not sleep: run every loop under `caffeinate -dims`, on AC power, lid open.

## 1. Start the loops (laptop-a)
Run both as background processes that survive across turns (Bash `run_in_background: true`), logging to files you can grep:
```bash
caffeinate -dims swarm serve  > ~/.swarm/<project>/serve.log 2>&1     # ONE serve across all laptops
caffeinate -dims swarm run    > ~/.swarm/<project>/run-a.log 2>&1     # this laptop's workers
```
Never hot-edit the installed harness while these run (see §5 for how to ship a harness fix).

## 2. Plan a milestone
```bash
swarm plan PLAN.md --milestone M0            # planner model from config (Fable here); writes .swarm/tasks.proposed.json
# read the proposal table: titles, scopes, deps, sizes, types, agent routing
swarm apply-proposals                        # or `swarm plan ... --apply` when you trust it (non-TTY needs --apply)
```
Review rules before applying (these come from real failures):
- One owner per shared doc (CONTRACTS/ARCHITECTURE/DESIGN/README) per milestone; others read only.
- Two tasks touching the same files → one depends on the other.
- Contracts/types tasks first; consumers depend on them.
- Anything with a page, a server, or a browser/server check is **M**, not S. Never route frontend to Haiku.
- Acceptance criteria must be checkable by the worker (no "reload with the server stopped").
- Critical = the demo dies without it. Don't inflate.
- Detail only the current milestone; plan the next one at ~70 % done.
- Fix a bad proposal by editing `.swarm/tasks.proposed.json` and `swarm apply-proposals`, or after applying with `swarm cut` / `swarm split` / `swarm add` / `swarm assign`.

Model choice is yours: `swarm assign T-012 --agent claude-a --model opus --effort high` for a hard one, a cheaper model for mechanical work. Respect credit budgets in config comments (e.g. a teammate's Codex limit that must last until the event).

## 3. Watch loop (the long-running part)
Every 10-15 minutes (use Monitor on `serve.log` / ScheduleWakeup / a sleep-free poll, not a tight loop):
1. `swarm status` → read RISK lines first, then Running/Blocked/Review.
2. **Open questions**: answer reversible ones yourself with `swarm answer Q-007 "..."` (add `--follow-up` on an fyi that changes the agent's choice). Only escalate to the human what truly needs them (API keys, recordings, money, product taste). Tell the user in one line what you need. **`[harness]` notes** are worker feedback about the harness: triage them per §5 and answer the question with what you did.
3. **Stuck work**: a task Running far past its size limit → `swarm logs T-xxx`; a failed task with 3 attempts → read the decision log, then cut + re-add a better-specified task (don't just retry). Offline agent → `swarm reroute`.
4. **Merges**: serve reviews (policy in config) and merges. Pull main locally after merges if you need to read code.
5. **Costs**: `swarm usage --since 5h`. Waste (retries, failed attempts) above ~30 % means tasks are badly specified, so fix the planning, not the workers.
6. Milestone ~70 % done → plan the next milestone. Milestone 100 % → serve runs the retro automatically; read `docs/retro/<stamp>.md` and the new `docs/LESSONS.md` lines.

## 4. Human touchpoints
- The Notion dashboard ("Swarm · <project>") is the human control plane: Agents table, Questions for you, Tasks board, Status block.
- When the user is AFK, keep going: answer what you can, park what you can't (the task waits as Blocked; other work proceeds), and leave a short summary in `docs/` of the harness repo or the project for when they return.
- Teammate actions (pull + restart runner, log in, plug in) go to the user as one ready-to-forward message.

## 5. Self-improvement loop (mandatory, every run)
**Contract, known to every agent:** the harness, its prompts, `.swarm/` config, the vendored skills and this skill are self-updating, and the orchestrator (you, the human's agent on laptop-a) is the only writer. Workers never edit them; they report problems in `harness_feedback` in their report, which the runner files as an `[harness]` fyi question on the board and in `docs/decisions/<task>.md`. Both laptops' notes land on the same board. You triage every note, ship the fix, and tell the human what changed (one line per change, plus the field-notes file). This is written into `swarm/prompts/rules.md` (rule 10), the template `AGENTS.md`, and `ORCHESTRATOR.md`, so Claude and Codex workers both see it.

The harness gets better each time it is used. Whenever something surprises you (a bug, a stall, a wasteful pattern, a missing feature, a confusing message) or a `[harness]` note arrives, do all of:
1. **Record** it in `swarm-control/docs/field-notes/<YYYY-MM-DD>-<project>.md`: what happened, evidence (task id, log line), cost/time impact, fix or idea.
2. **Fix the harness** when it is a harness problem:
   - Write a failing test in `swarm-control/tests/`, fix it, `.venv/bin/pytest -q` green, commit to swarm-control `main`, push.
   - Ship it safely: wait until no task is Running on the affected laptop (or accept a resume), stop `swarm run`/`serve`, `pip install -e .` (already editable: just restart), restart the loops.
   - If laptop-b must pick it up, give the user the forward-ready message: "pull swarm-control, restart `swarm run`". A runner on stale code heartbeats but never claims.
3. **Tune the project** when it is a project problem: `swarm retro` writes `docs/LESSONS.md` + `.swarm/tuning.yaml`; add skills/MCP that helped to `skills_by_type` / `mcp_by_type`; fix the planner rules in `swarm/prompts/planner.md` if a planning mistake repeats.
4. **Update this skill**: add a one-line rule to "Field rules" below (or edit an existing one) and commit it in the swarm-control repo. Keep the list short: merge duplicates and delete rules the code now enforces.
5. At the end of the run, write a short summary into the field-notes file: tasks done, cost, top 3 findings, what was fixed.

Do not let improvement work starve the product: harness fixes happen between milestones or while workers are busy, and anything larger than ~30 minutes becomes a note for later.

## Model roster (verify ids before the event; update this list when a provider ships a new model)
| Provider | best | high | mid | low | Notes (Oct 4 2026) |
|---|---|---|---|---|---|
| Claude Code | `fable` (long-horizon; planner, orchestrator) | `opus` (frontend #1 on WebDev Arena) | `sonnet` | `haiku` (never on frontend pages) | ids are aliases accepted by `claude --model` |
| Codex | `gpt-6-astra` | `gpt-6-astra` | `gpt-6-sol` — **a newer Sol release exists; confirm its exact id on the Codex laptop and put it in `models.mid`** | `gpt-6-luna` | ids come from the Codex model picker on the laptop that runs it |
| Gemini / Antigravity | parked (login issues, quota) | | | | see rehearsal notes |
Change a tier in `.swarm/config.yaml` → `agents.<name>.models`; running tasks keep their model, new claims use the new one; the teammate's runner needs a `git pull` + restart to see config changes.

## 6. Command reference
| Command | Use |
|---|---|
| `swarm status` | status page; RISK lines |
| `swarm plan PLAN.md --milestone Mx [--apply]` / `swarm apply-proposals` | decompose a milestone |
| `swarm add "title" --type T --importance I --size S --scope "glob" --depends T-003 --acceptance "..."` | one task |
| `swarm assign T-x --agent A [--model M --effort E]` | override routing |
| `swarm cut T-x` / `swarm split T-x [--apply]` | remove / break up |
| `swarm answer Q-x "text" [--follow-up]` | answer a question |
| `swarm reroute` | move Ready tasks off offline/cooling agents |
| `swarm logs T-x [--attempt N]` | prompt/stdout/stderr of a run on this laptop |
| `swarm usage --since 5h` | spend, cache, waste |
| `swarm retro [--dry-run]` | lessons + tuning + report |
| `swarm tools [--install]` | plugins/skills/MCP per task type |
| `swarm run --dry-run` | print the next compiled worker prompt |
| `swarm agents-sync` | after editing agents in config |

Task types: frontend, backend, realtime, ml_audio, ml_vision, ml_fusion, eval, tests, docs, research, bugfix, integration, infra. In `*_by_type` maps, a family key (`ml`) covers its subtypes (`ml_audio`, ...).

## Field rules
Learned from real runs; newest last. Each is one line: rule (evidence).
- Branch names repeat across projects (`task/T-001`); never reuse a GitHub repo across boards (Roomcast, Sep 28: 4 "merged" tasks never landed).
- Sleep kills runs; `caffeinate -dims` on every loop, check `swarm doctor` sleep row (Sep 28 laptop-b drops).
- Restart the teammate's runner after every harness or board change; stale runners heartbeat but never claim (night of Sep 28).
- Critical tasks must never route to an agent that has not heartbeated (night cycle 3 stalled 75 min).
- One owner per shared doc per milestone (Roomcast T-003/T-011/T-013 rebase conflicts, $5.58 task).
- Haiku never on frontend pages; a page that calls an API is M (Roomcast T-013, 3 turn-limit failures).
- `*_by_type` keys must match task types; `ml` used to match nothing until family keys were added (Oct 4, selective-hearing setup).
- `docs_by_type` section refs match the heading text (`PLAN.md#Ground rules`, not `#ground-rules`); `swarm doctor` now flags refs that resolve to nothing and unknown `*_by_type` keys (Oct 4).
- `swarm` loads `~/.swarm/env` itself when `NOTION_TOKEN`/`SWARM_HOST` are missing (Oct 4); the shell's values win, so `source` it only when you want to override.
