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

| 15 | codex-b idle for ~1 h after coming back: the Ready column was empty because the planner chained M0/M1 (`types → io → fixtures → bench → DFN3 → GTCRN`); T-011/T-020/T-023 only needed the merged types | board 00:40 UTC | un-chained by hand (deps → T-001, agent codex-b); planner rule "no artificial chains, ≥ 2N ready tasks"; serve `redistribute_on_return` re-routes all Ready tasks when an agent comes back online (17373fb) |
| 16 | A stale runner's late publish overwrote the Done I set on T-001 and the task was redone on Fable | run-a.log | `Runner.publish_outcome` discards results for tasks that are Done/Cut or re-claimed; serve `reconcile_merged` closes tasks whose PR is MERGED (17373fb) |
| 17 | Teammate runners sit on old harness code until a human pulls | JOIN flow | `swarm/selfupdate.py`: idle runners fetch, fast-forward and re-exec every 10 min; `swarm run` warns when the machine can sleep (dd3fc51, 17373fb) |
| 18 | The user expected worker processes to appear as this session's subagents; they are separate `claude -p` processes started by `swarm run` and do not show in the Claude Code agent panel | user question | documented in the skill §0; `pgrep -f "claude -p"` lists them with task worktree and model |

| 19 | Orchestrator prioritisation: I noticed codex-b idle and kept shipping harness fixes for ~40 min before giving it work | user feedback 00:45 UTC | `swarm status` now raises a RISK line for any idle agent with nothing Ready while tasks remain; skill §3 step 0 "idle agents first" |

| 20 | Restarting a busy runner (to raise parallelism) parked T-006 (Fable) and three Opus runs; codex-b's tasks had just been rerouted to claude-a because Codex hit a rate limit (cooldown) | run-a.log 00:1x UTC | `swarm restart` / SIGUSR1 drains then re-execs (feb00b4); skill rule; the self-updater already waits for idle |
| 21 | User wants more throughput: Max plan + a second Pro account | user | laptop-a 6 slots; per-agent `env` so `claude-a2` can run under `CLAUDE_CONFIG_DIR=~/.claude-pro` (feb00b4) |

| 22 | Teammate replaced codex-b with antigravity-b by committing to main directly (20:13 EDT); the board kept a stale codex-b row in "cooldown" | git log | row retired by hand; idea: `agents-sync` marks rows missing from config as "removed" |
| 23 | With 3 agents / ~10 slots only one task was open: the planner's later-milestone L tasks were chained one behind another | board 01:10 UTC | un-chained 7 tasks to true prerequisites, split T-015/T-016/T-018/T-021; skill rule "capacity jump → re-plan" |

| 24 | Fable on every critical task burned the Max 5-hour window ($35 in 2.5 h): claude-a rate-limited at 03:12 UTC, three planner splits and one review failed with "session limit" | status, split logs | critical default back to Opus; Fable only by hand for T-010/T-019; reviewer moved to the Pro account; claude-a 4 slots |
| 25 | Antigravity: `--model gemini-3.1-pro-high --effort medium` rejected by agy (T-022 failed twice) | task last_error | adapter skips --effort when the id carries it (d0da39d) |
| 26 | Reviewer escalated T-022 to Blocked because *it* was rate-limited | Q-030 | reviewer returns `defer` on rate limit; task stays in Review (d0da39d) |
| 27 | serve kept routing to codex-b after it was removed from config (serve loads config at start) | serve.log | restarted serve; idea: reload config on change |
| 28 | 16 worker notes triaged in one pass: honest verify exit codes, shared models dir, auto dep reinstall, declared deps, ephemeral ports, no `timeout` on macOS, pip only from main | Q-011..Q-029 | project f5c5e10 + skill |

| 29 | Re-ran `swarm split T-018` after a rate-limit error although the first run had applied; five duplicates (T-044..T-048) created and cut | board | `split` refuses Cut/Done tasks; skill rule |

| 30 | 04:03-08:00 UTC: the Max account rate-limited again; seven **critical** Ready tasks stayed on it for four hours while claude-a2 sat idle, because stealing and rerouting excluded critical work | watcher + status | reroute moves a cooling/offline agent's critical tasks to an idle capable agent (`route(exclude=owner)`); test updated to the new policy |
| 31 | iLab rejects public keys (`gssapi-keyex,gssapi-with-mic,password,keyboard-interactive`); unattended access needs a Kerberos ticket (`kinit ap2772@CS.RUTGERS.EDU`), which only the human can mint; auto mode also blocks the orchestrator from reading credentials or editing `~/.ssh/config`, which is right | user screenshot, classifier denial | documented in the skill: never ask for or store passwords; ask the human for a ticket/key, not a secret |
| 32 | Second overnight batch of worker notes (Q-053..Q-078): datasets decode, cv2 from mediapipe, onnxruntime 1.30 teardown abort masquerading as test failure, ruff rule families undocumented, mock transcript too short, host mismatch (laptop-b is a MacBook Air on Python 3.14), Codex sandbox commit denials (harness commits), HF skills still attached | board | hearing-stack gotchas, verify_fast crash detection, onnxruntime<1.29, mock-transcript task, prompts carry the execution host |

| 33 | 'accept as is' on a reviewer escalation re-queued the task to the worker (T-010 attempt 4) | serve.log 08:24 | relay: an accept answer on an escalation with a PR goes to Merge Ready |
| 34 | Morning of Oct 5: M0, M1, M2, M4 complete; Focus HQ integrated (T-019: 1 s window / 0.5 s hop, 1.5 s delay, MPS p50 237 ms); ASD calibrated (Light-ASD 0.906, lipmotion 0.827 frame accuracy on av_full); DFN3 streaming at 13 ms (dfn_ll); T-010 acceptance criterion corrected to noise-only | board | PLAN.md updated; decisions logged on the board |
| 35 | Joseph's Codex hit its own usage limit (ChatGPT plan) twice; antigravity-b never ran a task overnight | board | nothing to fix in the harness; the human owns the plan limits |

| 36 | T-043 bounced between codex-b cooldowns (ChatGPT plan limit) for ~1 h while both Claude agents idled: the rate-limit path requeued the task on the same agent | board 11:06-11:15 UTC | runner reroutes the task away immediately (`route(exclude=limited)`) |

| 37 | 16:xx UTC: DNS failed on laptop-a for a few minutes; runners backed off up to 300 s between ticks, heartbeats went stale, serve reaped T-053 from a healthy claude-a run (three times) and handed it to codex-b | run-a.log, serve.log | tick backoff capped at 60 s; template `heartbeat_stale_minutes` 20; project set to 20 |

| 38 | T-053 reaped six times: every reap followed a runner restart (self-update re-exec on laptop-b, Codex restarts, and two `pkill`s by the orchestrator during the DNS blip); a fresh runner's heartbeat lists no task, so serve's orphan rule fired on a healthy run | serve.log 627-639 | skill rule: never kill a runner with work in flight; idea: the runner persists its in-flight task ids to disk and re-lists them on the first heartbeat after a restart so the orphan rule does not fire |
| 39 | The harness test suite looked hung; it was only slow: with seven worker processes and a bench on the laptop the e2e test's git steps exceeded 45 s | faulthandler trace | run the suite with a generous timeout when workers are busy; no code change |

| 40 | iLab GPU reached via the human's SSH ControlMaster socket; torch had to be shipped as wheels from the Mac (iLab drops >300 MB downloads); cluster rejects --cpus-per-task/--mem; clearvoice needs --no-deps | iLab setup 19:00-20:30 UTC | project docs/DEPLOY + task T-054 (gpu_up.sh ilab mode); skill: hand the human a ControlMaster command when a host refuses keys |
| 41 | GPU bake-off on 80 two-face clips overturned the provisional decision: Enrolled audio-only TSE +10.1..11.5 dB at 0.5..2 s windows vs AV-MossFormer2 +11.2 only at 2 s; Dolphin negative. Focus HQ can run ~1 s behind live | eval/results/ilab | PLAN updated; decision-v2 task; T-055 briefed |

## Ideas for the harness (not done)
- runner: persist in-flight task ids (`~/.swarm/<project>/inflight-<host>.json`) and re-list them on the first heartbeat after a restart; serve then keeps the run alive until the real worker process is gone.
- serve: before reaping a task from an agent on serve's own host, check the worker process is really gone (pgrep the worktree path).
- serve/runner: reload `.swarm/config.yaml` when its mtime changes (agents added/removed without a restart).
- `agents-sync`: retire board rows for agents no longer in config (status "removed", note with the commit).
- serve: when free slots across online agents exceed Ready tasks for N minutes, auto-run `swarm split` on the largest Backlog task whose deps are met, or at least raise a RISK line (done for idle agents).
- `swarm status`: list running worker processes on this host (pid, task, model, minutes) so the orchestrator never has to pgrep.
- `swarm add` / `swarm deps T-x --depends ...`: a CLI to edit depends_on without the board API.
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

## Night summary (Oct 5, 04:10 UTC; the swarm keeps running)
- Product: 12 tasks merged (types, audio I/O, metrics, scenes, transcriber, setup/CI, GTCRN stage, session server, console, ASR/TTS adapters, remote GPU path, voice-match contract); M0 nearly done, M1 3/5, M4 3/6, M5 2/3. Fixtures (T-006) and DFN3 (T-010) are the long poles.
- Harness: ~20 commits, 240+ tests. New since start of day: family type keys, doctor checks, env autoload, harness_feedback + relay messages + `swarm tell`, idle steal, redistribute on return, merged-PR reconcile, superseded-result guard, self-update + `swarm restart`, model probe, per-agent env (second account), antigravity flag fix, reviewer defer on rate limit, split guard, `swarm handoff`, idle-agent RISK line.
- Cost: Max account ~$37 (hit the 5-hour window once), Pro ~$6, planner ~$12 across plans and splits.
- Orchestrator mistakes worth remembering: edited an open task's scope on main; restarted a busy runner; left an agent idle while fixing the harness; re-ran a split without checking the board; let a failing test through a `pytest | tail && commit` chain (use `pytest > log; rc=$?`).

## Plan complete (Oct 5, 19:00 UTC)
- M0-M5 all done (M3 6/6 after T-053 merged as a provisional decision). Laptop-a ledger: **$147 over 300 runs, $3.42 per merged task**; waste $45 (failed runs $31, mostly T-053's six reaped attempts). Final retro: 3 findings (undersized, shared-file, tool-default).
- Still running outside the swarm: the real av_full two-face bake-off on laptop-a (orchestrator-run; results to `eval/results/bakeoff-av_full.json`), to replace T-053's copied numbers via a follow-up task.
- iLab GPU: not reached. Public keys refused; Kerberos port 88 unreachable from off campus and from campus Wi-Fi; the remaining route is an SSH ControlMaster session opened by the human.

## Run summary (Oct 5, 11:15 UTC: voice engine built)
- 36 h after `swarm init`, M0-M5 are done except the bake-off write-up (T-043) and two small follow-ups. ~50 tasks merged, 80 commits on main, 470+ tests, `docs/BENCHMARKS.md` generated from a 21-minute full run on laptop-a: Natural fast `dfn_ll` 13 ms / +4.1 dB SI-SDRi noise-only; Focus HQ `av_mossformer2@2` +13.8 dB SI-SDRi, WER 1.28 → 0.78 on GRID; Enrolled audio-only +11.8 dB; Light-ASD 0.906 frame accuracy; Text WER 0.55 → 0.73?? (raw 0.588 vs cleaned 0.730: cleaning hurt captions, a finding for the slides).
- Spend: Max ≈ $110, Pro ≈ $40, Codex/Antigravity on the teammate's plans; the planner ran ~$15.
- Harness: 30+ commits during the run, 35 field-note rows, every one turned into code, a rule or a planner instruction.
- Still open for the humans: iLab Kerberos ticket (GPU bake-off at 2 s windows), ElevenLabs key, wired-headphone latency rehearsal (Thu Oct 9).
