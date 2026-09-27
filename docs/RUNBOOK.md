# Game-day runbook

## Before the event (both laptops)

1. Install: `git clone <swarm-control> && cd swarm-control && python3 -m venv .venv && source .venv/bin/activate && pip install -e .`
2. `gh auth login` (the runner pushes and opens PRs as you).
3. Export `NOTION_TOKEN=<internal connection token>` and `SWARM_HOST=<your name in config.hosts>` in your shell profile.
4. One person: create the Notion parent page, add the connection to it (••• → Add connections), invite the teammate as a guest.
5. Instantiate the project: `swarm template ../<project> && cd ../<project> && git init && gh repo create ...` Edit `.swarm/config.yaml` (agents, hosts, models). Commit and push.
6. `swarm init --parent-page <id>` once; commit `.swarm/notion.yaml`.
7. `swarm doctor` on both laptops, then `swarm doctor --smoke <agent>` for every agent you will use. An agent whose smoke fails is disabled: remove it from config or fix the CLI.
8. Laptop A: disable sleep, plug in power.

## 0:00–0:45 — plan

- Humans + orchestrator session (Claude Code opened in the project repo, with `@../swarm-control/ORCHESTRATOR.md` added to `CLAUDE.md` on laptop A): write `PLAN.md` (summary, demo story, milestones, floor and ceiling), sketch `docs/DESIGN.md` principles and the first `docs/CONTRACTS.md` events. Commit.
- `swarm plan PLAN.md --milestone M1`. Read the table. Fix scopes and dependencies. Apply.

## 0:45 — start the loops

- Laptop A: `caffeinate -i swarm serve` in one terminal.
- Both laptops: `swarm run` in one terminal each.
- Open the Notion board (By Status) and the Questions board.

## 0:45–1:30 — first hour rules

- Do not hand-code. Watch Questions, answer fast.
- If `verify_fast.sh` misbehaves on the real project, fix the script (as a task or by hand on main), not the agents.
- `swarm status` at 1:30. Plan M2 when M1 is about 70% done.

## Hourly

- `swarm status`. Cut or split anything a RISK line names. Plan the next milestone at 70%.
- Answer questions. Silence on an fyi question means the agent's choice stands.

## T-4h

- `review_policy: all` in config. Restart `swarm serve --no-merge`. Merge by hand: `gh pr list`, `gh pr merge <n> --squash`.
- Only bugfix and polish tasks.

## T-2h

- Freeze. Demo rehearsal. Pitch notes from `docs/decisions/*.md` and `docs/debt/*.md`.

## Recovery

| What happened | Do this |
|---|---|
| A laptop died or slept | Restart `swarm run`. Its running tasks are requeued by serve within 10 minutes. |
| serve died | Restart it. If it refuses ("another serve is running"), wait for the old heartbeat to go stale (10 min) or run `swarm serve --host <same name>`. |
| Notion is down | Workers keep working and publish when it returns. Nothing to do. |
| A provider is rate limited | `swarm status` shows cooldown. Non-critical work reroutes itself. `swarm reroute` to force it. |
| main is broken | `swarm serve --no-merge`, fix main by hand, restart serve. |
| A task keeps failing | It becomes a Question after 3 attempts. Cut, split, or fix by hand. |
| Two agents fight over files | Give one task a dependency on the other; the next plan lint will warn about overlaps. |
