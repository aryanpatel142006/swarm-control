# SDD ledger — plan: docs/superpowers/plans/2026-09-26-swarm-control.md
Branch: feat/swarm-control-v0 (from master 86e803b). Spec: docs/superpowers/specs/2026-09-26-swarm-control-design.md (read).
Execution: inline (executing-plans), chosen because tasks share tight interfaces held in this context; final fresh-context review at the end.

## Pre-flight (shared interfaces)
- T3 claim_task ↔ T12 Runner: claim_task(board, task, agent, *, sleep, nonce, wait_s) — consistent.
- T4 props ↔ T5 NotionBoard: task_to_props(task, fields) / page_to_task(page) / question_to_props(q, task_page_id=, fields=) — consistent.
- T6 Workspace ↔ T12/13/14: provision(task_id, *, reuse_branch), run_script(path, rel, timeout), push(path, branch, *, force_with_lease), git(cwd, *args, check=) — consistent.
- T7 RunSpec ↔ T12/13/16: same field list everywhere — consistent.
- T8 parse_report/report_to_markdown ↔ T12 — consistent.
- T10 route/context_from_board ↔ T15/16/17 — consistent; context_from_board added in T15 to router.py.
- T13 imports STRUCTURED_PROVIDERS from runner; runner does not import reviewer — no cycle.
- Pre-flight ruling: T15 acquire_lock compares by host, but test_lock uses two Servers on the same host; use an instance "holds lock" flag instead of host comparison — cost if wrong: two serves on one laptop could both run (they would both merge; merges are idempotent per task, low cost).
- Pre-flight ruling: T12 test_cooldown_agent_not_dispatched_unless_critical expects MERGE_READY for a critical task, but policy high_and_above routes critical to REVIEW; fix the test expectation to REVIEW — cost if wrong: none (test-only).
- Pre-flight ruling: T15 FakeReviewer in test_serve must persist status via board.update_task or merge_pending cannot see it — test-double fix — cost: none.
- Pre-flight ruling: Runner gets an injectable log_dir (default ~/.swarm/<project>/runs) so tests do not write to the real home directory — cost if wrong: none.
- Pre-flight ruling: CLI callback uses ctx.invoked_subcommand == "template" to skip config loading (plan's sys.argv sniffing is fragile) — cost: none.
Task 1: complete (commits 51d8504..47282d1, tests: .venv/bin/pytest tests/test_models.py → 6 passed in 0.01s)
Task 0: complete (commits 86e803b..51d8504, tests: .venv/bin/python -c 'import swarm' → ok; task-done script records nothing for silent commands, line added by hand)
Task 2: complete (commits 47282d1..ef62de0, tests: .venv/bin/pytest tests/test_config.py → 6 passed in 0.05s)
Task 3: complete (commits ef62de0..4ac4a56, tests: .venv/bin/pytest tests/test_board_memory.py → 5 passed in 0.01s)
Task 4: complete (commits 4ac4a56..8b9d0be, tests: .venv/bin/pytest tests/test_notion_props.py → 7 passed in 0.01s)
Task 5: complete (commits 8b9d0be..1666566, tests: .venv/bin/pytest tests/test_notion_client.py → 9 passed in 0.02s)
Task 6: complete (commits 1666566..c58e750, tests: .venv/bin/pytest tests/test_workspace.py → 8 passed in 1.75s)
Task 7: complete (commits c58e750..5317e72, tests: .venv/bin/pytest tests/test_adapters.py → 7 passed in 1.08s)
Task 8: complete (commits 5317e72..beebd4b, tests: .venv/bin/pytest tests/test_report.py → 6 passed in 0.01s)
Task 9: complete (commits beebd4b..68ea9a1, tests: .venv/bin/pytest tests/test_prompt.py → 5 passed in 0.03s)
Task 10: Ruling: context_from_board implemented in Task 10 instead of Task 15 (same module, no consumer yet) — cost if wrong: none
Task 10: complete (commits 68ea9a1..122ca24, tests: .venv/bin/pytest tests/test_router.py → 9 passed in 0.07s)
Task 11: complete (commits 122ca24..a374f68, tests: .venv/bin/pytest tests/test_usage.py → 3 passed in 0.01s)
Task 12: Ruling: FakeAdapter captures prompt text at run time (the plan's test read the prompt file after the worktree was disposed) — test-only — cost: none
Task 12: complete (commits a374f68..ef06de6, tests: .venv/bin/pytest tests/test_runner.py → 16 passed in 4.21s)
Task 13: complete (commits ef06de6..b48efa8, tests: .venv/bin/pytest tests/test_reviewer.py → 6 passed in 1.55s)
Task 14: complete (commits b48efa8..1675b28, tests: .venv/bin/pytest tests/test_merge.py → 4 passed in 1.68s)
Task 15: complete (commits 1675b28..503317e, tests: .venv/bin/pytest tests/test_status.py tests/test_serve.py → 9 passed in 0.93s)
Task 16: complete (commits 503317e..dc367d2, tests: .venv/bin/pytest tests/test_planner.py → 5 passed in 0.36s)
Task 17: Ruling: test_template_copies deferred to Task 19 (template dir is that task's deliverable; plan says so) — verified after Task 19 — cost: none
Task 17: complete (commits dc367d2..76700dd, tests: .venv/bin/pytest tests/test_cli.py -k 'not template' → 5 passed, 1 deselected in 0.14s)
Task 18: note: e2e passed on first run (integration over already-tested components; no new production code)
Task 18: complete (commits 76700dd..a9a97b0, tests: .venv/bin/pytest tests/test_e2e.py → 1 passed in 3.96s)
Task 19: complete (commits a9a97b0..8938815, tests: .venv/bin/pytest → 118 passed in 14.76s)
Task 20: note: integration test skipped (no SWARM_NOTION_TOKEN); to be run by the user against a throwaway page before rehearsal
Task 20: complete (commits 8938815..a1d6901, tests: .venv/bin/pytest → 118 passed, 1 skipped in 15.08s)

## Final review (fresh-context reviewer, model fable) — fix pass
Final: fixed gh merge --delete-branch racing the checked-out worktree — test_merge_disposes_worktree_before_gh_and_deletes_remote_branch RED→GREEN
Final: fixed restarted runner stranding Running tasks — test_recover_orphans_on_start + test_reap_orphan_with_fresh_heartbeat RED→GREEN
Final: fixed worker push not forced / push failure ignored — test_push_uses_force_with_lease + test_push_failure_is_failed RED→GREEN
Final: fixed merge_failed permanent and silent — test_gh_merge_failure_retries_then_blocks RED→GREEN (3 failures → Blocked + Question)
Final: fixed one serve step exception aborting the tick; review/merge run in a background thread in `swarm serve` — test_step_exception_does_not_abort_tick RED→GREEN
Final: fixed status:blocked without a question — test_blocked_without_question_synthesizes_one RED→GREEN
Final: fixed Changes Requested tasks never rerouted off offline agents — test_reroute_changes_requested_on_offline_agent RED→GREEN
Final: fixed publish overwriting another claimant's state — test_publish_abandons_when_claim_lost RED→GREEN
Final: fixed Ctrl-C leaving tasks Running — test_stopping_requeues_instead_of_publishing RED→GREEN (parks partial work on the branch)
Final: fixed critical tasks hot-looping on a rate-limited provider — test_cooldown_gates_dispatch_for_every_importance RED→GREEN
Final: fixed hand-made Notion cards without an ID — test_page_without_id_has_empty_id + test_assign_ids_to_handmade_tasks RED→GREEN
Final: fixed .swarm-run/ depending on the project's .gitignore — test_swarm_run_excluded_without_gitignore RED→GREEN (written to .git/info/exclude)
Final: suite 132/132 passed, 1 skipped (Notion integration, needs a token)
Final: Ruling: cooldown now gates dispatch for every importance (plan exempted critical) — a rate-limited provider cannot run anything; the critical exemption stays in routing only — cost if wrong: a critical task waits ≤15 min for the cooldown instead of hammering a 429
Final: Ruling: a Running task with no recorded start time is reaped only on stale heartbeat, never by the orphan rule — cost if wrong: a hand-set Running card without a start waits for the 10-minute stale rule
Final: Ruling: Server.background defaults to False (tests, --once) and `swarm serve` passes True — cost if wrong: none
Final: minor (deferred): `swarm --host nope run` raw traceback; `swarm doctor --help` outside a project fails on config load; `swarm logs` IndexError on empty dir
Final: minor (deferred): `swarm init` not idempotent; "Needs Human" view not created
Final: minor (deferred): relay treats any answer as "go" (answering "cut" re-runs); `swarm cut` does not warn about dependents
Final: minor (deferred): Retry-After in HTTP-date form raises ValueError
Final: minor (deferred): reviewer.py hardcodes `origin/` instead of ws.remote
Final: minor (deferred): runner and serve share worktree_root/<id>; ms-wide race at the Review transition
Final: minor (deferred): heartbeat full-row upsert can clobber a concurrent cooldown write (self-heals on next 429)
Final: minor (deferred): reroute ignores soft cap; per-task get_agent in tick replaced by one list_agents (done), remaining: none
Final: minor (deferred): timeout SIGKILLs the CLI (spec says TERM then KILL); rate-limit regex includes bare `capacity`/`overloaded`
Final: minor (deferred): duplicate-ID race between `swarm add` and a serve follow-up; InMemoryBoard not thread-safe for `--memory run`
Final: minor (deferred): no threaded-runner test (all tests use SyncExecutor)

## Live tryout (Sept 27, 2026) — real Notion, real GitHub, real Claude Code, one laptop
- Scenario 1: 4 tasks (haiku/sonnet/opus by importance) → 4 PRs merged, decision logs on main, dependency promotion worked. Costs: $0.08–$0.31 per task.
- Scenario 2: a task instructed to ask a blocking question → Blocked + Q-002 on the board → `swarm answer` → resumed on its branch (reuse) → merged. A high task → reviewer (sonnet, read-only) approved → merged. 6/6 done.
- Defect found and fixed live: `gh pr merge` right after a force-push races GitHub's mergeability recompute ("not mergeable"); the merger now polls `gh pr view --json mergeable` until it settles and retries once. (test_merge_waits_for_github_mergeability_after_push)
- Defect found and fixed live: answering a merge-failed question re-ran the task instead of retrying the merge; relay now maps "retry"→Merge Ready, "merged"→Done. (test_relay_on_merge_failed_task_retries_merge_or_marks_done)
- Observation: a sleeping Mac pauses every process; 30-second serve ticks stretched to hours. Runbook now says `caffeinate -dims` for both loops and disable sleep.
- Observation: editing the installed package while a live driver runs broke one worker tick with a SyntaxError for ~1 minute. Do not hot-edit during a run.
- Deferred minors fixed after the run: lazy config (--help anywhere), unknown-host and empty-logs errors, HTTP-date Retry-After, cut warns about dependents, reviewer uses the configured remote.
- Still deferred: init idempotency; runner/serve shared worktree path race; heartbeat vs cooldown clobber; reroute ignores soft cap; SIGKILL on timeout; duplicate-ID race; InMemoryBoard thread safety; no threaded-runner test; status board columns come out alphabetical (Notion API cannot order groups).
