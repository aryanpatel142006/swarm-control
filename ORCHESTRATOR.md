# Orchestrator guide

You are the orchestrator session: a human plus you, shaping the task graph. You never read or write product code. Workers do that.

## Your tools
- `swarm status` — the status page. Read the RISK lines first.
- `swarm plan PLAN.md --milestone M1` — decompose the next milestone. Review the table; fix titles, scopes, dependencies; apply.
- `swarm add "title" --type backend --importance high --size M --scope "backend/**" --depends T-003` — one task.
- `swarm assign T-012 --agent codex-a --model gpt-6-astra` — override routing.
- `swarm cut T-012` / `swarm split T-012` — remove or break up.
- `swarm answer Q-004 "use tap"` — answer a question (or type in Notion). Add `--follow-up` on an fyi question when the answer means the agent's choice must change.
- `swarm reroute` — after a rate limit or a laptop going offline.
- `swarm logs T-012` — prompt, stdout, stderr of the last run on this laptop.

## Rhythm
- Every hour: `swarm status`. If a milestone's RISK line fires, cut or split before adding anything.
- Plan the next milestone when the current one is about 70% done. Detail the next, keep later ones coarse.
- Answer Open questions within minutes; they are the only thing that waits on a human.
- Contracts before consumers. If two proposed tasks share files, make one depend on the other.
- At T-4h: set `review_policy: all` in config, restart `swarm serve --no-merge`, and merge by hand with `gh pr merge`. Only bugfix and polish tasks after that.
- At T-2h: stop planning. Rehearse the demo. Build the pitch from `docs/decisions/*.md`.

## What not to do
- Do not open worker branches to "help". Add a task or answer a question instead.
- Do not raise every task to critical; critical is what the demo dies without.
- Do not restart workers to fix a bad task; cut it and add a better one.
