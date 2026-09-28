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

Requirements: Python 3.11+, `git`, `gh` (logged in), and whichever agent CLIs you use (`claude`, `codex`, `gemini`, `agy`, `grok`, or anything via the generic adapter). Gemini: `brew install gemini-cli`, run `gemini` once in a real terminal and pick "Login with Google" (the AI Pro subscription gives Flash; Pro needs a billed API key), then `swarm doctor --smoke gemini-a`.

## For a teammate joining (second laptop)

```
git clone https://github.com/aryanpatel142006/swarm-control.git && cd swarm-control
python3 -m venv .venv && source .venv/bin/activate && pip install -e ".[dev]"
pytest                                   # should be all green with no setup
```

Then, to work the same board as the rest of the team:

1. Get the Notion token from a teammate privately (never through git or chat logs) and put it in your shell profile
   along with your laptop's name from the project's `.swarm/config.yaml`:
   ```
   export NOTION_TOKEN=ntn_...
   export SWARM_HOST=laptop-b
   ```
2. Accept the invite to the project repo, clone it next to swarm-control, and run `swarm doctor` inside it.
   Then `swarm tools --install` there: it installs the plugins the project's config asks for (and Playwright's browser). On a Codex laptop also register the MCP servers named in config with `codex mcp add` (docs/RUNBOOK.md step 7).
   Every row must say yes except CLIs you don't have.
3. `swarm doctor --smoke <your agent>` once per agent you'll run (Claude Code, Codex, Antigravity...).
4. `caffeinate -dims swarm run` and leave it. Cards assigned to your agents start moving on the shared board.

Only one laptop runs `swarm serve`; ask before starting a second one.

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

## The Notion board

`swarm init` builds one dashboard page per project under your parent page, plus the Tasks board as a full page next to it (so a task opens full-screen). The dashboard reads top to bottom: a how-to callout, the Agents table (name, status, last heartbeat, cooldown, current task, spend), Questions for you (open questions first), then two columns: a tile that opens the Tasks board and the live Status block that `swarm serve` rewrites every few minutes. Column order, board card fields, icons and covers are set by init; delete a finished project's pages yourself when you are done with them (the harness never deletes).

## Task statuses

`Backlog → Ready → Running → Review → Merge Ready → Done`, with `Changes Requested` (verify failed, reviewer findings, rebase conflict; same worker resumes), `Blocked` (waiting on a Question), `Failed` (retry ladder: same agent, one tier up, then a Question), and `Cut`.

## Routing

`agents.<name>.strengths` is a 1–5 score per task type; the highest wins, ties go to the shortest queue then the cheaper provider. `routing.importance_to_tier` maps importance to a model tier; `type_model_overrides` encodes evidence-based exceptions (Opus 5.5 outranks Fable 5.1 on frontend, so critical frontend runs Opus). Override any task with `swarm assign`, or drag its card in Notion.

## Toolbox (plugins, skills, MCP servers)

Workers get the tools their task type needs and nothing else, on both CLIs:

- **Plugins (Claude Code).** `plugins_required` are installed and enabled on every laptop (`swarm tools --install`, checked by `swarm doctor`). `plugins_by_type` are installed but may stay disabled globally: the runner loads them for one run with `--plugin-dir`. Every other plugin enabled on that laptop is switched off for the run with `--settings`, which halves a worker's base context (measured 7.6k → 3.9k tokens).
- **Skills.** `swarm template` vendors seven skills into the project's `.claude/skills` (TDD, verification, debugging, Hugging Face model choice, local inference, transformers.js); `.agents/skills` symlinks there for Codex. `skills_by_type` / `skills_by_importance` name the ones the prompt tells the worker to invoke.
- **MCP servers.** `mcp_by_type` / `mcp_by_importance` / `agents.<name>.mcp` pick servers per run. Claude gets exactly those via `--mcp-config … --strict-mcp-config` (definitions come from `claude mcp list`, installed plugins, or inline `mcp_servers`); Codex gets `-c mcp_servers.<name>.enabled=true` for servers registered once with `codex mcp add`.
- **Feedback loop.** Every report carries `tools_used` (skill / MCP server / plugin, helped or not), which the harness writes into `docs/decisions/<id>.md` next to the worker's decisions, so after a few tasks you can see which tools earn their context and promote them in config. Critical tasks are also told they may install a further official plugin mid-run and must log it there. `swarm tools` shows the whole picture for this laptop.

## Self-improvement

The swarm learns from its own runs. When every task of a milestone is Done or Cut, `swarm serve` runs a retro (`swarm retro` runs it by hand, `--dry-run` only prints). It reads the board, the usage ledger, main's history and the decision logs, applies fixed rules and writes two things to main: `docs/LESSONS.md`, a short list that every worker brief and the planner include (undersized tasks, files fought over by several tasks, review-heavy work), and `.swarm/tuning.yaml`, safe config changes merged over `config.yaml` on load (a cheap model that keeps hitting the turn limit on a task type raises that type's floor; a tool that helped on two tasks of a type becomes its default). Each retro also leaves a report in `docs/retro/`. Delete a line from `tuning.yaml` to undo a change.

## Adding a provider

Use `provider: generic` with a `command_template` such as `mycli --prompt-file {prompt_file} --model {model} --cwd {cwd}`. The model must write its report to `.swarm-run/report.json`. For a first-class adapter, subclass `swarm.adapters.base.Adapter`.

## Testing

`pytest` runs unit tests, adapter tests with a fake CLI, and an end-to-end run against an in-memory board and a temporary git remote. `SWARM_NOTION_TOKEN=... SWARM_NOTION_PARENT=... pytest tests/test_notion_integration.py` exercises a real Notion page.

## Docs

- Design spec: `docs/superpowers/specs/2026-09-26-swarm-control-design.md`
- Visual plan: `docs/plan-visual/SWARM_CONTROL_Plan.pdf`
- Game day: `docs/RUNBOOK.md`
- Orchestrator session: `ORCHESTRATOR.md`
