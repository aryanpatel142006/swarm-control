# Game-day runbook

## Before the event (both laptops)

1. Install: `git clone <swarm-control> && cd swarm-control && python3 -m venv .venv && source .venv/bin/activate && pip install -e .`
2. `gh auth login` (the runner pushes and opens PRs as you).
3. Export `NOTION_TOKEN=<internal connection token>` and `SWARM_HOST=<your name in config.hosts>` in your shell profile.
4. One person: create the Notion parent page, add the connection to it (••• → Add connections), invite the teammate as a guest.
5. Instantiate the project: `swarm template ../<project> && cd ../<project> && git init && gh repo create ...` Edit `.swarm/config.yaml` (agents, hosts, models). Commit and push.
6. `swarm init --parent-page <id>` once; commit `.swarm/notion.yaml`.
7. Gemini laptop (optional third agent): `brew install gemini-cli`, run `gemini` once in Terminal and log in with Google, then `swarm doctor --smoke gemini-a`; expect Flash only and a small quota (the harness cools it down and reroutes when it runs out).
7b. Perplexity agent (optional, `provider: perplexity`): add the config block from the README's "Perplexity" section (teammate joining), set `cli`/`args_template`/`approve_args` to the real command (the defaults are unverified), then `swarm doctor --smoke perplexity-b`.
8. `swarm tools --install` on both laptops (installs the plugins in config and Playwright's Chromium). A Codex laptop also registers the MCP servers named in config once: `codex mcp add context7 -- npx -y @upstash/context7-mcp` and `codex mcp add playwright -- npx @playwright/mcp@latest --headless --isolated`, then `enabled = false` on each in `~/.codex/config.toml` (the runner enables them per run). Then `swarm doctor` on both laptops, then `swarm doctor --smoke <agent>` for every agent you will use. An agent whose smoke fails is disabled: remove it from config or fix the CLI.
8. Both laptops: plug in power and disable sleep (System Settings → Battery → Options → Prevent automatic sleeping on power adapter). `caffeinate -dims` in front of every swarm command is the belt to that suspender.

## 0:00–0:45 — plan

- Humans + orchestrator session (Claude Code opened in the project repo, with `@../swarm-control/ORCHESTRATOR.md` added to `CLAUDE.md` on laptop A): write `PLAN.md` (summary, demo story, milestones, floor and ceiling), sketch `docs/DESIGN.md` principles and the first `docs/CONTRACTS.md` events. Commit.
- `swarm plan PLAN.md --milestone M1`. Read the table. Fix scopes and dependencies. Apply.

## 0:45 — start the loops

- Laptop A: `caffeinate -dims swarm serve` in one terminal.
- Both laptops: `caffeinate -dims swarm run` in one terminal each. Every process pauses when a Mac sleeps; during the tryout a closed lid stretched 30-second serve ticks into hours.
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
| A laptop died or slept | Restart `swarm run`. On start it requeues its own Running tasks with their branches intact (drilled: recovered within seconds). Serve also reaps them within 10 minutes if the runner never comes back. Ctrl-C or `kill <pid>` parks in-flight work on the branch and requeues it. |
| serve died | Restart it. If it refuses ("another serve is running"), wait for the old heartbeat to go stale (10 min) or run `swarm serve --host <same name>`. |
| Notion is down | Workers keep working and publish when it returns. Nothing to do. |
| A provider is rate limited | `swarm status` shows cooldown. Non-critical work reroutes itself. `swarm reroute` to force it. |
| main is broken | `swarm serve --no-merge`, fix main by hand, restart serve. |
| A task keeps failing | It becomes a Question after 3 attempts. Cut, split, or fix by hand. |
| One laptop idle while the other has a queue | serve rebalances one task per tick to an idle agent of equal strength; to move a specific card, `swarm assign T-012 --agent <name>` or drag it in Notion. |
| Two agents fight over files | Give one task a dependency on the other; the next plan lint will warn about overlaps. |
