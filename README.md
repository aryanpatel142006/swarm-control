# swarm-control

A small harness that lets a two-person team run several AI coding agents in parallel across two laptops from one Notion board. Each task runs in a fresh, size-capped CLI session inside its own git worktree; the tool verifies, pushes, opens the PR, and reports. A control loop reviews, merges, recovers dead work, and relays your answers to blocked agents.

Built for HackRU Fall 2026. It is a development tool, not a hackathon project: keep it in its own public repo and list it in your submission.

## Install

```
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
export NOTION_TOKEN=...      # internal connection token
export SWARM_HOST=laptop-a   # your name in config.hosts
```

Requirements: Python 3.11+, `git`, `gh` (logged in), and whichever agent CLIs you use (`claude`, `codex`, `agy`, `gemini`, `grok`, or anything via the generic adapter).

## Ten-line mental model

1. `PLAN.md` is the product. `swarm plan` turns a milestone of it into tasks with types, importance, sizes, scopes, and dependencies.
2. A router picks the strongest configured agent for each task type and the model tier from importance. Critical gets the best model; low gets the cheapest.
3. Tasks live in a Notion database with board views by status and by agent. Humans can drag cards.
4. `swarm run` on each laptop polls for its agents' Ready tasks, claims one, makes a worktree, compiles a prompt from the task card and only the docs that type needs, runs the CLI headless with turn, time, and budget caps, runs `verify_fast.sh`, pushes, opens a PR, and posts the report to the card.
5. The model must end with a structured report: status, summary, files, tests, temporary hacks, decisions, and an optional question.
6. `swarm serve` on one laptop reaps stale tasks, retries failures up a model tier, relays your answers, promotes tasks whose dependencies merged, reviews high-importance work with a different model family, rebases and merges, reroutes around rate limits, and rewrites a status page.
7. Blocked agents ask in the Questions database. You type the answer there or run `swarm answer`. The task resumes on its branch with your answer in the prompt.
8. Decisions and debts are written per task into `docs/decisions/` and `docs/debt/` on the branch, so the pitch has receipts.
9. Every run's tokens and cost go to a local ledger and the Agents database; soft caps and cooldowns keep one subscription from becoming the bottleneck.
10. Everything above is configuration in `.swarm/config.yaml`.

## Quick start

```
swarm template ../myproject && cd ../myproject     # scaffold the project repo
# edit .swarm/config.yaml: agents, hosts, models, event times
swarm init --parent-page <notion page id>          # creates databases + board views, writes .swarm/notion.yaml
swarm doctor && swarm doctor --smoke claude-a      # tokens, CLIs, gh, verify scripts, one-turn smoke run
swarm plan PLAN.md --milestone M1                  # propose + create tasks
caffeinate -i swarm serve                          # laptop A only
swarm run                                          # every laptop
swarm status                                       # any time
```

## Task statuses

`Backlog → Ready → Running → Review → Merge Ready → Done`, with `Changes Requested` (verify failed, reviewer findings, rebase conflict; same worker resumes), `Blocked` (waiting on a Question), `Failed` (retry ladder: same agent, one tier up, then a Question), and `Cut`.

## Routing

`agents.<name>.strengths` is a 1–5 score per task type; the highest wins, ties go to the shortest queue then the cheaper provider. `routing.importance_to_tier` maps importance to a model tier; `type_model_overrides` encodes evidence-based exceptions (Opus 5.5 outranks Fable 5.1 on frontend, so critical frontend runs Opus). Override any task with `swarm assign`, or drag its card in Notion.

## Adding a provider

Use `provider: generic` with a `command_template` such as `mycli --prompt-file {prompt_file} --model {model} --cwd {cwd}`. The model must write its report to `.swarm-run/report.json`. For a first-class adapter, subclass `swarm.adapters.base.Adapter`.

## Testing

`pytest` runs unit tests, adapter tests with a fake CLI, and an end-to-end run against an in-memory board and a temporary git remote. `SWARM_NOTION_TOKEN=... SWARM_NOTION_PARENT=... pytest tests/test_notion_integration.py` exercises a real Notion page.

## Docs

- Design spec: `docs/superpowers/specs/2026-09-26-swarm-control-design.md`
- Visual plan: `docs/plan-visual/SWARM_CONTROL_Plan.pdf`
- Game day: `docs/RUNBOOK.md`
- Orchestrator session: `ORCHESTRATOR.md`
