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
- No machine in the swarm may sleep while its loop runs: `caffeinate -dims swarm run|serve` on macOS, sleep disabled elsewhere, plugged in, lid open. `swarm run` now warns at startup when sleep is possible; tell every teammate this before they start (codex-b went offline twice on Oct 4 from sleep).

Workers are separate headless CLI processes started by `swarm run` on each laptop (one per task, model chosen from the task's importance tier), not subagents of this session: they never appear in the Claude Code agent panel. See them with `pgrep -fl "claude -p"` (laptop-a) or the Agents table (any laptop).

## 1. Start the loops (laptop-a)
Run both as background processes that survive across turns (Bash `run_in_background: true`), logging to files you can grep:
```bash
caffeinate -dims swarm serve  > ~/.swarm/<project>/serve.log 2>&1     # ONE serve across all laptops
caffeinate -dims swarm run    > ~/.swarm/<project>/run-a.log 2>&1     # this laptop's workers
```
Never hot-edit the installed harness while these run (see §5 for how to ship a harness fix). To pick up new code or config, use **`swarm restart`** (SIGUSR1): the runner stops claiming, finishes in-flight runs, then re-execs. Never `pkill` a busy runner: it parks half-done work and a Fable run was lost that way on Oct 4. Runners older than feb00b4 have no SIGUSR1 handler and would die on it; restart those only when `pgrep -f 'claude -p'` shows nothing in flight.

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
0. **Idle agents first.** Any agent `idle` while tasks remain open is the top priority, above harness work, docs or anything else: give it work within minutes (`swarm status` prints a RISK line for it). If the Ready column is empty, the plan is chained too tightly: un-chain tasks that only need merged contracts, or split a running L task. An agent that just came back online is re-routed by serve automatically; check that it actually picked something up.
1. `swarm status` → read RISK lines first, then Running/Blocked/Review. **When capacity jumps** (a new agent, more slots), re-plan immediately: un-chain Backlog tasks to their true prerequisites and `swarm split --apply` the coarse L tasks of later milestones *before* they become Ready, so the new slots fill within minutes (Oct 4: three agents, one open task).
2. **Open questions**: answer reversible ones yourself with `swarm answer Q-007 "..."` (add `--follow-up` on an fyi that changes the agent's choice). Only escalate to the human what truly needs them (API keys, recordings, money, product taste). Tell the user in one line what you need. **`[harness]` notes** are worker feedback about the harness: triage them per §5 and answer the question with what you did.
3. **Stuck work**: a task Running far past its size limit → `swarm logs T-xxx`; a failed task with 3 attempts → read the decision log, then cut + re-add a better-specified task (don't just retry). Offline agent → `swarm reroute`.
4. **Merges**: serve reviews (policy in config) and merges. Pull main locally after merges if you need to read code.
5. **Costs**: `swarm usage --since 5h`. Waste (retries, failed attempts) above ~30 % means tasks are badly specified, so fix the planning, not the workers.
6. Milestone ~70 % done → plan the next milestone. Milestone 100 % → serve runs the retro automatically; read `docs/retro/<stamp>.md` and the new `docs/LESSONS.md` lines.

## 3b. Orchestrator failover (the orchestrator's own account can hit a limit too)
The swarm never depends on this session: serve and the runners keep reviewing, merging, re-routing and self-updating without it. But planning, triage and harness fixes stop when the orchestrator's account is rate-limited (Oct 5 2026: the Max account hit its 5-hour window at 03:12; planner, reviewer and the orchestrator all stalled together). Rules:
- Watch the orchestrator account's share of `$/5h` in `swarm status` (its workers are the proxy). At **~70 %**, or on the first "session limit" error from any tool call, run `swarm handoff` (writes `.swarm/HANDOFF.md`, commit it) and tell the human: *"open a new orchestrator session on the other account: `CLAUDE_CONFIG_DIR=~/.claude-pro claude` in the project repo, say 'resume as orchestrator from .swarm/HANDOFF.md'"*. The new session reads the handoff and this skill and continues; the old one stops acting.
- Keep planner and reviewer on different accounts from the orchestrator when possible (`planner.agent`, `reviewer.agent` in config), so a limit on one does not freeze all three.
- One exhausted account must never stall the board: configure `reviewer.fallback_agents` (an agent on another account, same host as serve). serve then fails the review over at once (`reviewer failover A → B until HH:MM UTC` in serve.log) and returns to the primary after the reset; `swarm status` prints the active reviewer. `RISK: reviewer exhausted, no fallback` means every candidate is limited: add an agent to `reviewer.fallback_agents` and restart serve (it does not reload config), or review by hand (Oct 6: nine tasks waited two hours in Review).
- Never let the orchestrator's account also run the bulk of the workers when a second account exists: spread parallel slots across accounts.

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

## Several accounts on one laptop
A second Claude account runs as its own agent with its own config dir: log it in once in a real terminal (`CLAUDE_CONFIG_DIR=$HOME/.claude-pro claude`, then `/login`), then add an agent with `env: {CLAUDE_CONFIG_DIR: /Users/<you>/.claude-pro}` and raise the host's `max_parallel.claude`. Give the cheaper plan the normal/low tiers (Sonnet) and keep critical work on the Max account.

## Model roster (verify ids before the event; update this list when a provider ships a new model)
| Provider | best | high | mid | low | Notes (Oct 4 2026) |
|---|---|---|---|---|---|
| Claude Code | `fable` (long-horizon; planner, orchestrator) | `opus` (frontend #1 on WebDev Arena) | `sonnet` | `haiku` (never on frontend pages) | ids are aliases accepted by `claude --model` |
| Codex | `gpt-6-astra` | `gpt-6-astra` | `gpt-6.1-sol` (confirmed Oct 4 2026 via the teammate's PR; `gpt-6-sol` is the older release) | `gpt-6-luna` | ids come from the Codex model picker on the laptop that runs it |
| Gemini / Antigravity | parked (login issues, quota) | | | | see rehearsal notes |
Change a tier in `.swarm/config.yaml` → `agents.<name>.models`; running tasks keep their model, new claims use the new one; the teammate's runner needs a `git pull` + restart to see config changes.

## 6. Command reference
| Command | Use |
|---|---|
| `swarm status` | status page; RISK lines |
| `swarm plan PLAN.md --milestone Mx [--apply]` / `swarm apply-proposals` | decompose a milestone |
| `swarm add "title" --type T --importance I --size S --scope "glob" --depends T-003 --acceptance "..."` | one task |
| `swarm assign T-x --agent A [--model M --effort E] [--force]` | override routing; a task already Running on another agent is refused unless `--force` (which stops that run within 30 s and re-queues it) |
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
- An idle agent now steals a Ready task from a saturated agent even when weaker (strength ≥ 3); before Oct 4 claude-a idled while T-002 waited behind codex-b's single slot. If you still see an idle agent next to a Ready task, check `swarm status` for `cooldown`/`offline` and `swarm reroute`.
- Workers' `harness_feedback` arrives as `[harness]` fyi questions; `swarm doctor --models <agent>` records which model ids a laptop accepts in its Agents row; the first heartbeat writes the CLI version + tier map there too (Oct 4).
- `swarm status` is a snapshot: a task shown Ready may be claimed by the time you `assign` it. Assigning a Running task to another agent is refused by default (the old runner would keep working, as T-062 did on Oct 5); decide whether the lost minutes are worth it and use `--force`, or assign only tasks still Ready/Backlog.
- Changing `agents.<name>.models` in config does not touch already-routed tasks: re-point them with `swarm assign T-x --agent A --model M` (Oct 4: eight critical tasks moved to Fable by hand).
- Never commit to `main` inside an open task's Scope, and never delete tracked files a live branch still carries: T-002's approved PR hit a rebase conflict because the orchestrator edited `scripts/setup_worktree.sh` (in its scope) and removed `hearing.egg-info` on main (Oct 4). Fix shared infra by `swarm tell` to the owning task, or wait for its merge, or cut it and re-add.
- A project `.gitignore` must list `.venv` **without** a slash as well as `.venv/`: the slash form does not match the symlink `setup_worktree.sh` creates, `git add -A` committed one, and the next `git pull` turned the main checkout's venv into a link to itself (Oct 4, two review rounds lost). The harness now also writes `.venv` / `node_modules` to `info/exclude` for every worktree, strips its own venv from `PATH` when running project scripts, and fails a task fast when `setup_worktree.sh` exits non-zero.
- The template no longer ships `GEMINI.md` or a placeholder `frontend/` (Oct 4): one `AGENTS.md` plus a one-line `CLAUDE.md`; add a Gemini pointer only when a Gemini agent is configured. Never commit `*.egg-info/`.
- When a human merges a PR by hand, the task on the board stays Running/Review until serve notices; mark it Done (`board.update_task`) if its runner is silent, or the reaper will hand merged work back out (T-001, Oct 4).
- `context7` needs an interactive OAuth login; headless workers cannot complete it. Leave `mcp_by_type` empty for workers unless a pre-authorised server config exists (Q-001, Oct 4).
- An idle agent next to an empty Ready column is a **planning** problem: check `depends_on` for artificial chains and un-chain tasks that only need merged contracts (board API or cut + `swarm add --depends T-001`), so every agent always has work (Oct 4: T-011/T-020/T-023 handed to codex-b this way). Serve now also re-routes every Ready task when an agent comes back online and closes tasks whose PR was merged by hand; runners discard results for tasks closed meanwhile and update themselves when idle.
- Budget the top model: Fable on every critical task exhausted a Max 5-hour window in 2.5 h and stalled planner, reviewer and workers together (Oct 4-5). Default critical to Opus; `swarm assign --model fable` only for the one or two tasks that need it; put the reviewer on a different account than the workers; watch `$/5h` in `swarm status`.
- Agents removed from config keep a board row until `swarm agents-sync` (now retires them) and serve keeps the old config until restarted: after any config change to agents, run `swarm agents-sync`, restart serve, `swarm restart` runners.
- Before re-running a `swarm split` (after a rate limit, say), check the board: the first run may have applied even though its log ended in an error. `swarm split` now refuses Cut/Done tasks; five duplicate bake-off tasks had to be cut on Oct 5.
- Credentials: never ask for, read or store a human's password, even when offered. Ask for the thing the harness can hold safely: an SSH key (if the host allows it), a Kerberos ticket the human mints (`kinit user@REALM`, ~10 h, renew with `kinit -R`), or an API token in `~/.swarm/env`. Rutgers iLab allows no public keys; GSSAPI only (Oct 5).
- A rate-limited owner must not hold critical Ready work while anyone capable is idle; serve now moves it. If you see critical tasks waiting on a `cooldown` agent, `swarm status` RISK should say so; if not, reassign by hand (Oct 5: four hours lost).
- A rate-limited agent's task is now rerouted at the moment of the limit (runner `_rate_limited`), not parked on the same agent until its cooldown ends; before Oct 5 T-043 bounced for an hour. If you see a task cycling Ready→Running→Ready on one agent, check `swarm usage` for that agent's provider limits.
- A restarted runner cannot re-adopt runs its previous process owned: its first heartbeat lists no tasks, serve reaps the in-flight task as an orphan, and the next agent starts over. So **never `pkill` a runner that may have work in flight**, even during a network blip (the runner retries by itself within 60 s); use `swarm restart` (drain) or wait. Oct 5: T-053 lost six attempts this way, two of them to the orchestrator's own restarts.
- Generated fixtures, downloads and benchmark results live **outside the worktree**, at a path the project exports by env (selective-hearing: `HEARING_MODELS_DIR`, `HEARING_RESULTS_DIR` from `scripts/_py.sh`); worktrees are deleted with the task, and T-024's raw JSON and av_full fixtures vanished that way. The template's `setup_worktree.sh` now links `eval/fixtures/generated` and `eval/results` from the main checkout (`SWARM_SHARED_DIRS` overrides). Unit tests must never glob generated dirs (T-059 fixed `test_faces` after five notes).
- Task text is what workers trust: write only facts you checked and mark the rest `(unverified: <source>)`. Copy host/GPU names from logs, flags from `--help`, behaviour from merged code (Q-093, Q-110, Q-121, Q-125). `swarm add` now adds files named in `--acceptance` to the scope and appends "Not on main" notes for paths that do not exist; read its `lint:` lines. Workers may edit out-of-scope files the acceptance needs and must name them in the report.
- `swarm add --host laptop-a` (planner field `host`) pins a task to one host's agents; routing and rebalancing respect it. Use it for anything that needs that machine's GPU/MPS, weights or saved results (T-040, T-053 ran on a CPU-only laptop).
- `swarm tell` text: put a command on one line in backticks and say "run it inline"; never suggest storing it in a `$VAR` (zsh does not word-split; Q-116 lost a round trip). When PLAN.md changes a decision a running task's description depends on, `swarm tell` that task the same minute.
- The harness now (Oct 5): resumes a retried task from `origin/task/<id>` when it has unmerged commits, retries `git worktree add` on lock errors, merges current main into the branch before verify (merge, never rebase, since Oct 6), puts the worktree first on the worker's `PYTHONPATH` (scripts from `/tmp` imported the main checkout: six notes), keeps a draft `.swarm-run/report.json` from a cut-off run and resumes it, and never truncates a reviewer finding's text (Q-080). Do not re-add rules for these to project docs.
- The Codex worker sandbox on macOS cannot launch Chromium/Playwright (`bootstrap_check_in` denied): route tasks whose acceptance needs a browser screenshot to claude-* agents (T-063, Oct 6).
- A teammate's ChatGPT/Codex plan can run out mid-run ("You've hit your usage limit"). The harness now cools that agent down until the reset time in the message (3 h if none), routes nothing to it, critical work included, and `swarm status` shows `codex-b usage-limit until HH:MM` (UTC). Tell the human their plan is exhausted; `swarm assign --force` only tasks still stuck on it (codex-b, Oct 6: a 15-minute cooldown handed it two more tasks).
- A board outage (DNS, Wi-Fi) looks exactly like a dead worker; serve now refuses to reap on a stale heartbeat within one stale window of its own transport failure. If you still see `reaped … rerouted` right after `transport` errors in serve.log, move the task back by hand (T-061 went to the CPU laptop that way, Oct 6).
- No worker ever runs `git rebase`/`fetch`/`pull`/`merge` (rule 17): the runner merges main into a task branch and, on a conflict, leaves the markers and lists the files in the prompt; the worker edits, `git add`s, commits. Never `swarm tell` a worker to rebase; merge commits on `task/*` branches are expected (PRs squash-merge). After pulling this change, restart serve so the merger merges too (Q-140, Q-144, Q-146, Oct 6).
- Worker turns now have an effort floor (60/100/150 for medium/high/xhigh), so an S task at medium effort may hit its 20-minute limit before its turns: a `timeout` flag on S tasks means size them M, not raise turns (Q-142, Q-143, Oct 6).
- A benchmark longer than its task's time limit never finishes inside a worker: make it its own task with incremental results, or pre-run it yourself (Q-135, Q-139: 40-minute Mac bake-off).
- Read a question's full text in `swarm status` or its `Details` column on the board; the title is only a prefix. Questions filed before Oct 6 (up to Q-153) were cut at ~190 chars and cannot be recovered: ask the task's agent again with `swarm tell` if the cut part mattered.
- A runner that always has work used to never self-update (laptop-a ran day-old code on Oct 6 and mixed rebase instructions into merge-era prompts). It now drains and restarts once its code is 30 min behind; until a runner has that code, check it yourself: a task's `~/.swarm/<project>/runs/<task>/attempt-N/prompt.md` should carry as many worker rules as `swarm/prompts/rules.md`; if not, `swarm restart` that runner (drain, never kill).
- The runner rejects placeholder tokens (`TBD`, `XXX`, `TODO(fill)`, `{{`, `<fill`, lorem) in lines a task adds to docs/text files. A doc that must quote such a token puts it in backticks or a fenced block; a project that wants it off sets `verify.placeholders: []`.
- Worker notes (`.swarm-run/notes.md`) and a cut-off attempt's draft report reach the next attempt only on the same laptop (they live in its run log dir); a retry routed to another laptop sees them only in the board's attempt report. Keep retries of measurement tasks on one host (`--host`).
- `swarm assign --force` no longer lets the old run delete the new owner's worktree: a runner disposes or provisions a task worktree only while it still owns the claim (Q-200, Oct 6). Runners pick this up on their next drain; until `~/.swarm/<project>/runs/<task>/attempt-N/prompt.md` carries rule 18, force-assign only tasks whose old run is already gone.
- Verify runs share per-host slots (`hosts.<host>.max_parallel_verify`, default 2). A slow board while many tasks finish is verifies queueing (runner/serve logs say "verify slots … busy; waiting"), not a hang. For a project to take part with its workers' own verifies, its `scripts/verify_fast.sh` needs the template's three-line `swarm-lock` re-exec at the top. Raise the slot count only on a machine with idle cores; for a measurement you take yourself, run it under `~/.swarm/<project>/bin/swarm-lock --exclusive -- <cmd>`.
- A `[test-hygiene]` harness note means verify_full failed on a test the task did not cause (flaky on rerun, or failing on main too); the task was not sent back. Create ONE task that owns that test and `swarm tell` any running task that reported it to leave it alone (Q-196: two branches fixed the same Kokoro test and conflicted).
- Shared data paths for workers belong in the project config's `env:` block (`HEARING_RESULTS_DIR: "{repo_root}/eval/results"`), not only in a script workers must remember to source (Q-198).
- Write `swarm add` text in single quotes or a quoted heredoc; in double quotes zsh expands `$HEARING_RESULTS_DIR` to nothing and the worker reads `/demo_regress_<stamp>.json` (Q-186). `swarm add` now warns about such paths: read its yellow `lint:` lines.
- Latency acceptance and idle-machine measurements: write "not worse than the baseline re-measured in the same run under `swarm-lock --exclusive`"; a number that needs an idle Mac is a human/rehearsal step, or pause the runners (`swarm restart` drains) and take it yourself (Q-177, Q-195).
- `swarm answer Q-x` refuses an id two rows share (before Oct 6 a publish could file two questions with one id; Q-209): read the rows it prints and add `--kind harness|fyi|…`, `--task T-x` or `--all`.
- Measurement queues (Oct 6, Q-229/Q-238/Q-241): new verifies now queue behind a waiting `swarm-lock --exclusive`, the wait line names who holds the machine, and the runner adds a worker's lock waits back to its time limit (up to half). Still size measurement tasks M+ and avoid scheduling several at once on one laptop. `swarm-lock --snippet` prints the three lines a project's `scripts/verify_fast.sh` needs to take part (Q-224).
- When a merged task changes a default fixture, clip or metric, `swarm tell` every running task that measures on it to re-baseline on the new one (Q-232: T-096's targets were for the clip T-097 replaced mid-run). When you answer a question that points at another task's fixture, say whether the copy already in the shared fixtures dir counts (Q-225).
- Task text you write with `swarm add`: cite only merged work (or add `--depends`); the worker prompt now lists cited tasks that are not Done and tells the worker not to build on them (Q-236, Q-238). Gitignored files in the main checkout are now reported by the lint as "Not tracked in git … at <abs path>", not "Not on main" (Q-227); still name them by env var.
- A verify failure right after the runner merged main cleanly now says it may be a semantic merge conflict and lists main's commits and files (Q-228); a resumed attempt lists files written to the config `env:` dirs since the previous attempt started (Q-241). Neither needs a rule in project docs.
- Routing (serve's reroute, rebalance, redistribute and the runner's rate-limit reroute) now moves only Ready/Backlog tasks, plus an unclaimed Changes Requested task whose agent is offline (agent only, its status stays). It never touches Review, Merge Ready, Blocked, Running or Done, and it re-reads the row before it writes. A run cut by a rate limit goes back to the status it was claimed from, so a merge-conflict round stays Changes Requested. When every capable agent is cooling, serve leaves the task where it is and logs `all agents cooling; T-x waits on A until HH:MM UTC` once; that is expected, so do not reassign it by hand. Before Oct 6, T-095 (approved, PR #88) and T-102 (PR #93) swapped between claude-a and claude-a2 on every tick and ended Ready (field note 85). Restart serve and the runners to pick this up.
- Never hand-append text to a task's description: the last block is the lint's `Harness notes:` list, so appended work items end up inside its last bullet. A round that is already running or in review never sees the edit, and its reviewer then judges it against criteria the worker never had (T-100: items 5-6 were appended after attempt 1, Q-243). To add work, put the criterion in `acceptance` and `swarm tell` the task with the numbered items. The worker gets them under "address every item".
- A `[test-hygiene]` note with no test id now names the failing step in its title (`no test id; failing step: demo replay regressed or failed`), and its Details keep the head and the tail of the output (Q-244). When that step is a measurement (demo replay, bench), it is load-sensitive. The project needs a task that makes that step re-measure or report instead of failing verify_full under load, so a code task is not blocked by it.
- `swarm restart` drains on liveness now (field note 90, Oct 6): a run counts only while its worker CLI pid is alive or its thread is still setting up or publishing; the runner logs `draining: waiting on T-x (agent, phase, pid N alive, M min) · gives up in K min` every minute and restarts anyway after one size limit (a CLI still alive then is stopped; its task resumes from its branch). A silent drain is a bug: read `run-*.log` before you `pkill` anything.
- Gitignored shared inputs that sit beside committed files (demo clips and stems next to a committed `manifest.json`) go in the project config's `worktree_links: [eval/fixtures/demo]`; the runner symlinks each gitignored file from the main checkout into every fresh worktree (nine tasks lost runs to 'no demo clip', Q-250…Q-278). Pure data dirs keep using `env:`.
- Bug tasks you write quote the raw output of the failing command (probe JSON, log lines) and write a cause only as "possible cause, unverified"; quoted numbers carry the flags they were measured under (locked vs `--no-lock`) (Q-266, Q-268, Q-275).
- Runners re-read `.swarm/config.yaml`, `tuning.yaml` and `notion.yaml` when they change (field note 97): the next run uses the new `worktree_links`, `env`, skills, limits or routing at once, and a change to `agents`, `hosts`, the repo or the board restarts the runner (draining when busy). serve still reads its config once: after an edit it logs `config changed on disk since serve started …`; restart serve yourself when routing, reviewer or policy keys changed. Before Oct 7 a config edit reached no runner until it restarted (T-110, T-112 ran without `worktree_links`; Q-286, Q-288, Q-289).
- `swarm add` and the planner now print `lint: T-x is open and names <file>` when a new task names a file an open task names with no dependency either way, and the worker gets the same list in its Harness notes. Take the hint: add `--depends T-x` or fold the edit into one task; T-110 and T-111 both rewrote `scripts/gpu_up.sh`'s srun lines in M9 and cost a conflict round (Q-288).
- Retros run per completed milestone on that milestone's tasks only (`swarm retro --milestone M9`; plain `swarm retro` still reads the whole board), count shared files within a milestone, and list only tools not already default. Every retro from M4 to M9 had re-reported the same five all-time findings.
- A resumed worker no longer re-files the previous attempt's harness notes (the runner drops a note whose words mostly match one already filed for that task, Q-288 repeated Q-286). Several notes from different tasks that report the same thing are still separate questions: answer each with the same commit.
- Tasks you write cite the run behind a quoted number (results file or session id, scorer flags) and put clock facts (job end, reset) in the description with their source and read time, not in the title (Q-286). Name the telemetry field a count comes from and what it means by contract (Q-281: top-level `tier` vs `tiers.active`).
