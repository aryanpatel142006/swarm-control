"""The `swarm` command line."""
from __future__ import annotations

import json
import os
import re
from datetime import datetime
import shutil
import stat
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from .config import Config, ConfigError, load_config, save_notion_ids
from .models import IMPORTANCES, SIZES, TASK_TYPES, AgentRow, Status, Task, utcnow

app = typer.Typer(help="swarm-control: Notion board + git worktrees + headless coding agents.",
                  no_args_is_help=True)
console = Console()
TEMPLATE_DIR = Path(__file__).resolve().parent / "template"   # package data: ships with pip install
SKILLS_DIR = Path(__file__).resolve().parent / "skills"       # vendored worker skills; copied into <project>/.claude/skills


class State:
    cfg: Config | None = None
    config_path: Path | None = None
    memory: bool = False
    host: str | None = None


state = State()


def _cfg() -> Config:
    if state.cfg is None:
        try:
            state.cfg = load_config(state.config_path or find_config(Path.cwd()))
        except (ConfigError, typer.BadParameter, OSError) as e:
            raise typer.Exit(code=_fail(str(e)))
    return state.cfg


def find_config(start: Path) -> Path:
    cur = Path(start).resolve()
    for p in [cur, *cur.parents]:
        cand = p / ".swarm" / "config.yaml"
        if cand.exists():
            return cand
    raise typer.BadParameter("no .swarm/config.yaml found in this directory or its parents")


def _fail(msg: str) -> int:
    console.print(f"[red]error:[/red] {msg}")
    return 1


def make_board(cfg: Config, memory: bool = False):
    if memory:
        from .board.memory import InMemoryBoard
        return InMemoryBoard()
    from .board.notion import NotionBoard, NotionClient
    token = os.environ.get("NOTION_TOKEN")
    if not token:
        raise typer.Exit(code=_fail("NOTION_TOKEN is not set"))
    if not cfg.notion.tasks_ds:
        raise typer.Exit(code=_fail("Notion ids missing; run `swarm init --parent-page <id>` first"))
    return NotionBoard(NotionClient(token), cfg.notion)


def _workspace(cfg: Config):
    from .workspace import Workspace
    ws = Workspace(cfg.repo_root, cfg.worktree_root, cfg.main_branch)
    configure_verify_slots(ws, cfg, state.host, log=console.print)
    return ws


def configure_verify_slots(ws, cfg: Config, host: str | None, log=print) -> None:
    """Every verify this process runs (runner, merger, reviewer) shares the machine's verify slots with the other
    swarm processes and the workers' own wrapped verifies (swarm/hostlock.py, Q-175/Q-185/Q-190)."""
    from .hostlock import lock_dir, verify_slot
    slots = cfg.hosts[host].max_parallel_verify if host in cfg.hosts else 2
    ws.verify_slot = lambda label: verify_slot(lock_dir(cfg.project), slots, log=log, label=label)
    ws.script_env = cfg.project_env()


def _ledger(cfg: Config):
    from .usage import Ledger, default_ledger_path
    return Ledger(default_ledger_path(cfg.project), board=(cfg.notion.tasks_ds or "")[:8])


@app.callback()
def main(ctx: typer.Context,
         config: Path = typer.Option(None, "--config", help="path to .swarm/config.yaml"),
         memory: bool = typer.Option(False, "--memory", help="use an in-memory board (dry runs)"),
         host: str = typer.Option(None, "--host", help="this laptop's name in config.hosts (or SWARM_HOST)")):
    from .env import load_env_file
    load_env_file()                      # ~/.swarm/env fills in NOTION_TOKEN / SWARM_HOST when the shell did not
    state.cfg = None
    state.config_path = config
    state.memory = memory
    state.host = host or os.environ.get("SWARM_HOST")


@app.command()
def doctor(offline: bool = typer.Option(False, "--offline", help="skip network checks"),
           smoke: str = typer.Option(None, "--smoke", help="agent name to smoke-test with a 1-turn prompt"),
           models: str = typer.Option(None, "--models", help="agent name: try every configured model id once and "
                                                              "record which ones work on this laptop's board row")):
    """Check tokens, CLIs, git, gh, verify scripts."""
    from .doctor import probe_models, run_checks, smoke_agent
    checks = run_checks(_cfg(), state.host, offline=offline)
    if smoke:
        checks.append(smoke_agent(_cfg(), smoke, _cfg().worktree_root / "_smoke"))
    if models:
        board = None if offline else make_board(_cfg(), state.memory)
        checks.extend(probe_models(_cfg(), models, _cfg().worktree_root / "_smoke", board=board))
    table = Table("check", "ok", "detail")
    for c in checks:
        table.add_row(c.name, "[green]yes[/green]" if c.ok else "[red]NO[/red]", c.detail)
    console.print(table)
    raise typer.Exit(code=0 if all(c.ok for c in checks) else 1)


@app.command()
def tools(install: bool = typer.Option(False, "--install", help="install missing plugins and enable disabled ones")):
    """Show the plugins, skills and MCP servers the swarm uses; --install sets this laptop up."""
    from .tools import all_mcp_servers, ensure_playwright_browser, ensure_plugins, installed_plugins, plugin_ref
    cfg = _cfg()
    wanted = list(dict.fromkeys(list(cfg.plugins_required)
                                + [p for names in cfg.plugins_by_type.values() for p in names]))
    if install:
        if cfg.plugins_required:
            missing = ensure_plugins(cfg.plugins_required, log=console.print, enable=True)
            if missing:
                console.print(f"[red]still unavailable: {', '.join(missing)}[/red]")
        by_type = [p for p in wanted if p not in cfg.plugins_required]
        if by_type:
            missing = ensure_plugins(by_type, log=console.print, enable=False)
            if missing:
                console.print(f"[red]still unavailable: {', '.join(missing)}[/red]")
        mcp_names = {m for v in cfg.mcp_by_type.values() for m in v} | {m for v in cfg.mcp_by_importance.values() for m in v}
        if "playwright" in mcp_names or any(plugin_ref(p)[0] == "playwright" for p in wanted):
            ensure_playwright_browser(log=console.print)
    have = installed_plugins()
    table = Table("plugin", "state", "used for")
    uses = {p: [t for t, names in cfg.plugins_by_type.items() if p in names] for p in wanted}
    for name in wanted:
        short, full = plugin_ref(name)
        info = have.get(short)
        state_txt = ("[green]enabled[/green]" if info and info.enabled else
                     "[yellow]disabled[/yellow]" if info else "[red]not installed[/red]")
        table.add_row(full, state_txt, ", ".join(uses[name]) or "every task type")
    console.print(table)
    servers = {**all_mcp_servers(), **cfg.mcp_servers}
    t2 = Table("MCP server", "known on this laptop", "task types", "importance")
    names = list(dict.fromkeys([m for v in cfg.mcp_by_type.values() for m in v]
                               + [m for v in cfg.mcp_by_importance.values() for m in v]
                               + [m for a in cfg.agents.values() for m in a.mcp]))
    for m in names:
        t2.add_row(m, "[green]yes[/green]" if m in servers else "[red]no[/red]",
                   ", ".join(t for t, v in cfg.mcp_by_type.items() if m in v),
                   ", ".join(i for i, v in cfg.mcp_by_importance.items() if m in v))
    console.print(t2)
    if cfg.skills_by_type or cfg.skills_by_importance:
        t3 = Table("skill", "task types", "importance")
        skills = list(dict.fromkeys([x for v in cfg.skills_by_type.values() for x in v]
                                    + [x for v in cfg.skills_by_importance.values() for x in v]))
        for sk in skills:
            t3.add_row(sk, ", ".join(t for t, v in cfg.skills_by_type.items() if sk in v),
                       ", ".join(i for i, v in cfg.skills_by_importance.items() if sk in v))
        console.print(t3)


@app.command()
def usage(since: str = typer.Option(None, "--since", help="only runs after this UTC time (e.g. 2026-10-10T12:00) "
                                                            "or a window like 5h / 2d"),
          all_boards: bool = typer.Option(False, "--all-boards", help="include runs from earlier boards of this project")):
    """Where the tokens went: spend by role, per task, cache write vs read, and waste (retries, re-reviews)."""
    from datetime import timedelta, timezone
    from .usage import Ledger, default_ledger_path, usage_report
    cutoff = None
    if since:
        m = re.fullmatch(r"(\d+(?:\.\d+)?)([hd])", since.strip())
        if m:
            cutoff = utcnow() - timedelta(hours=float(m.group(1)) * (24 if m.group(2) == "d" else 1))
        else:
            cutoff = datetime.fromisoformat(since)
            cutoff = cutoff if cutoff.tzinfo else cutoff.replace(tzinfo=timezone.utc)
    board = make_board(_cfg(), state.memory)
    done = [t.id for t in board.list_tasks(status=[Status.DONE])]
    board = (_cfg().notion.tasks_ds or "")[:8]
    r = usage_report(Ledger(default_ledger_path(_cfg().project), board=board), done=done, since=cutoff,
                     board=None if all_boards else board)
    console.print(f"[bold]${r['total_cost']:.2f}[/bold] over {r['runs']} runs · "
                  + " · ".join(f"{k} ${v:.2f}" for k, v in r["by_role"].items())
                  + (f" · ${r['cost_per_done_task']:.2f} per merged task" if r["cost_per_done_task"] is not None else ""))
    w = r["waste"]
    console.print(f"waste ${w['total']:.2f}: failed runs ${w['failed_runs']:.2f} · extra attempts ${w['extra_attempts']:.2f}"
                  f" · extra review rounds ${w['extra_review_rounds']:.2f}")
    c = r["cache"]
    if c["write"] or c["read"]:
        console.print(f"cache: {c['write']:,} tokens written · {c['read']:,} read "
                      f"({100 * c['read'] / max(1, c['write'] + c['read']):.0f}% of prompt tokens served from cache)")
    table = Table("task", "cost", "worker runs", "failed", "reviews", "models", "minutes")
    for t in sorted(r["tasks"], key=lambda x: x["task"]):
        table.add_row(t["task"], f"${t['cost']:.2f}", str(t["worker_runs"]), str(t["failed_runs"]), str(t["review_runs"]),
                      ", ".join(t["models"]), f"{t['seconds'] / 60:.1f}")
    console.print(table)


@app.command()
def retro(dry_run: bool = typer.Option(False, "--dry-run", help="print the findings; write and commit nothing")):
    """Learn from this board: lessons into docs/LESSONS.md, safe config changes into .swarm/tuning.yaml, report in docs/retro/."""
    from .retro import run_retro
    from .usage import Ledger, default_ledger_path
    cfg = _cfg()
    ledger = Ledger(default_ledger_path(cfg.project), board=(cfg.notion.tasks_ds or "")[:8])
    found = run_retro(cfg, make_board(cfg, state.memory), _workspace(cfg), ledger=ledger, log=console.print, dry_run=dry_run)
    console.print(f"{len(found)} findings" + (" (dry run)" if dry_run else ""))


@app.command()
def night(cycles: int = typer.Option(6, "--cycles"), minutes: int = typer.Option(75, "--minutes", help="time cap per cycle"),
          start: int = typer.Option(1, "--start", help="first cycle number (milestone N<start>)"),
          no_improve: bool = typer.Option(False, "--no-improve", help="build and retro only; a person reviews docs/night/cycle-*.md")):
    """Unattended loop: build one small app per cycle, answer questions, retro, then improve the harness from the log."""
    from .night import Night
    n = Night(_cfg().repo_root, log=console.print, cycles=cycles, minutes=minutes, improve=not no_improve)
    reports = n.run(start_cycle=start)
    console.print(f"night over: {sum(r.done for r in reports)} tasks done in {len(reports)} cycles, "
                  f"{sum(1 for r in reports if r.merged)} harness improvements merged")


@app.command()
def init(parent_page: str = typer.Option(..., "--parent-page", help="Notion page id that holds the databases")):
    """Create the Notion databases, board views, and status page; save ids to .swarm/notion.yaml."""
    from .board.notion import NotionBoard, NotionClient
    token = os.environ.get("NOTION_TOKEN")
    if not token:
        raise typer.Exit(code=_fail("NOTION_TOKEN is not set"))
    ids = NotionBoard.init(NotionClient(token), parent_page, list(_cfg().agents), project=_cfg().project)
    path = save_notion_ids(_cfg(), ids)
    console.print(f"created databases; ids saved to {path}")
    if not str(ids.get("views_ok", "")).startswith("true"):
        console.print("[yellow]board views could not be created via the API; add them by hand: "
                      "open each database → + view → Board → group by Status / Agent.[/yellow]")
    state.cfg = load_config(_cfg().path)
    agents_sync()


@app.command()
def tell(task_id: str, text: str):
    """Send a message into a task's next prompt (orchestrator → worker); logged as a relay note on the board."""
    from .relay import tell as _tell
    q = _tell(make_board(_cfg(), state.memory), task_id, text)
    if q is None:
        raise typer.Exit(code=_fail(f"{task_id} not found or already closed"))
    console.print(f"{q.id}: {q.text}")


@app.command()
def restart():
    """Gracefully restart the runner(s) on this laptop: they finish in-flight work, then re-exec on the current code."""
    import signal
    import subprocess
    r = subprocess.run(["pgrep", "-f", "swarm run"], capture_output=True, text=True)
    pids = [int(x) for x in r.stdout.split() if x.isdigit() and int(x) != os.getpid()]
    for pid in pids:
        try:
            os.kill(pid, signal.SIGUSR1)
        except OSError:
            pass
    console.print(f"restart requested for {len(pids)} runner process(es); they restart once idle")


@app.command()
def handoff(out: Path = typer.Option(None, "--out", help="where to write (default .swarm/HANDOFF.md)")):
    """Write everything a new orchestrator session needs to take over: status page, open questions, running
    tasks, pending human inputs and the resume steps. Use it when this orchestrator's account nears its limit."""
    from .status import render_status
    board = make_board(_cfg(), state.memory)
    tasks, agents, questions = board.list_tasks(), board.list_agents(), board.list_questions()
    status = render_status(_cfg(), tasks, agents, questions, utcnow())
    open_q = [q for q in questions if q.status == "Open"]
    lines = [f"# Orchestrator handoff · {_cfg().project} · {utcnow().strftime('%Y-%m-%d %H:%M UTC')}", "",
             "Written by `swarm handoff`. A new orchestrator session (any account, any laptop) resumes from here:",
             "1. open Claude Code in this repo with the `swarm-control` skill available; the harness keeps running meanwhile",
             "2. read this file, then `swarm status`; answer Open questions; check RISK lines",
             "3. keep the loop in the skill §3; append to swarm-control/docs/field-notes/ as you go", "",
             "## Status page", "```", status, "```", "",
             "## Open questions"] + [f"- {q.id} [{q.kind}] {q.task_id}: {q.text}" for q in open_q] + ["",
             "## Running / Ready tasks"] + [f"- {t.id} [{t.status.value}] {t.agent}/{t.model}: {t.title}" for t in tasks
                                            if t.status in (Status.RUNNING, Status.READY, Status.REVIEW, Status.MERGE_READY)] + ["",
             "## Harness loops on this laptop", "- `caffeinate -dims swarm serve` (one per project) and `caffeinate -dims swarm run [--agent X]` per account",
             "- logs: ~/.swarm/<project>/{serve,run-*}.log · restart runners with `swarm restart`", ""]
    path = out or (_cfg().repo_root / ".swarm" / "HANDOFF.md")
    path.write_text("\n".join(lines))
    console.print(f"handoff written to {path}")


@app.command("agents-sync")
def agents_sync():
    """Refresh Agent rows (and the Agent select options) from config."""
    board = make_board(_cfg(), state.memory)
    for a in _cfg().agents.values():
        row = board.get_agent(a.name) or AgentRow(name=a.name, status="offline")   # offline until its first heartbeat
        row.provider, row.host = a.provider, a.host
        board.upsert_agent(row)
    for row in board.list_agents():      # agents dropped from config must not look alive or routable
        if row.name != "serve" and row.name not in _cfg().agents and row.status != "removed":
            row.status, row.current_task, row.cooldown_until = "removed", "", None
            row.note = "removed from config"
            board.upsert_agent(row)
    if not state.memory:
        from .board import notion_props as np
        board.c.request("PATCH", f"/data_sources/{_cfg().notion.tasks_ds}",
                        json={"properties": {"Agent": np.TASKS_SCHEMA(list(_cfg().agents))["Agent"]}})
    console.print(f"synced {len(_cfg().agents)} agents")


def _print_proposals(proposals):
    table = Table("#", "title", "type", "imp", "size", "ms", "deps", "scope")
    for i, p in enumerate(proposals):
        table.add_row(str(i), p["title"], p["type"], p["importance"], p["size"], p["milestone"],
                      ", ".join(p.get("depends_on", [])), ", ".join(p.get("scope", [])))
    console.print(table)


@app.command()
def plan(plan_file: Path = typer.Argument(Path("PLAN.md")), milestone: str = typer.Option(None, "--milestone"),
         apply: bool = typer.Option(False, "--apply", help="create the tasks without asking")):
    """Decompose PLAN.md into tasks with the planner model, then create them."""
    from .planner import Planner
    board = make_board(_cfg(), state.memory)
    from .usage import Ledger, default_ledger_path
    pl = Planner(_cfg(), board, _workspace(_cfg()), log=console.print,
                 ledger=Ledger(default_ledger_path(_cfg().project), board=(_cfg().notion.tasks_ds or "")[:8]))
    proposals = pl.propose(plan_file, milestone=milestone)
    _print_proposals(proposals)
    if not proposals:
        raise typer.Exit(code=1)
    if apply or typer.confirm("create these tasks?", default=True):
        pl.apply(proposals)


@app.command("apply-proposals")
def apply_proposals(file: Path = typer.Argument(Path(".swarm/tasks.proposed.json"))):
    """Create tasks from an edited tasks.proposed.json."""
    from .planner import Planner
    board = make_board(_cfg(), state.memory)
    Planner(_cfg(), board, _workspace(_cfg()), log=console.print).apply(json.loads(file.read_text()))


@app.command()
def add(title: str, type: str = typer.Option("backend", "--type"), importance: str = typer.Option("normal"),
        size: str = typer.Option("M"), milestone: str = typer.Option(""), description: str = typer.Option(""),
        acceptance: str = typer.Option(""), depends: list[str] = typer.Option([], "--depends"),
        scope: list[str] = typer.Option([], "--scope"), priority: int = typer.Option(100),
        host: str = typer.Option("", "--host", help="pin to agents on this host (its GPU/MPS, weights, results)")):
    """Add one task, routed."""
    from .router import context_from_board, route
    if type not in TASK_TYPES or importance not in IMPORTANCES or size not in SIZES:
        raise typer.Exit(code=_fail(f"type/importance/size must be in {TASK_TYPES}/{IMPORTANCES}/{SIZES}"))
    board = make_board(_cfg(), state.memory)
    done = {t.id for t in board.list_tasks(status=[Status.DONE])}
    t = Task(id="", title=title, description=description, acceptance=acceptance, type=type, importance=importance,
             size=size, milestone=milestone, depends_on=list(depends), scope=list(scope), priority=priority,
             flags=[f"host:{host}"] if host else [])
    from .policy import apply_task_lint, main_exists
    for n in apply_task_lint(t, None if state.memory else main_exists(_workspace(_cfg()))):
        console.print(f"lint: {n}")
    from .policy import shell_expansion_hints
    for n in shell_expansion_hints(f"{title}\n{description}\n{acceptance}"):
        console.print(f"[yellow]lint: {n}[/yellow]")
    t.status = Status.READY if all(d in done for d in t.depends_on) else Status.BACKLOG
    t.agent, t.model, t.effort = route(t, _cfg(), context_from_board(board, _cfg()))
    t = board.create_task(t)
    console.print(f"{t.id} {t.status.value} → {t.agent} / {t.model} / {t.effort}: {t.title}")


@app.command()
def assign(task_id: str, agent: str = typer.Option(..., "--agent"), model: str = typer.Option(None),
           effort: str = typer.Option(None),
           force: bool = typer.Option(False, "--force", help="If another agent is already running the task, stop "
                                                             "that run and re-queue it for the new agent.")):
    """Override routing for one task."""
    board = make_board(_cfg(), state.memory)
    t = board.get_task(task_id)
    if not t:
        raise typer.Exit(code=_fail(f"{task_id} not found"))
    if agent not in _cfg().agents:
        raise typer.Exit(code=_fail(f"unknown agent {agent}"))
    fields = ["agent", "model", "effort"]
    # A Running task is held by a claim nonce the runner re-reads every 30 s; changing only the agent field
    # leaves the old runner working (T-062 kept running on claude-a after `assign claude-a2`, Oct 5 2026).
    if t.status is Status.RUNNING and t.agent != agent:
        if not force:
            raise typer.Exit(code=_fail(
                f"{t.id} is Running on {t.agent} (attempt {t.attempts}); its runner would keep the work. "
                f"Re-run with --force to stop that run and hand the task to {agent}, or leave it."))
        t.status, t.claim_nonce = Status.READY, ""
        fields += ["status", "claim_nonce"]
        console.print(f"[yellow]{t.id}: stopping {t.agent}'s run (it notices within 30 s) and re-queueing[/yellow]")
    from .router import model_for, tier_for
    t.agent = agent
    default_model, default_effort = model_for(_cfg().agents[agent], tier_for(t, _cfg()), t.type, _cfg())
    t.model = model or default_model
    t.effort = effort or default_effort
    board.update_task(t, fields)
    console.print(f"{t.id} → {t.agent} / {t.model} / {t.effort}")


@app.command()
def cut(task_id: str):
    """Mark a task Cut (never run again)."""
    board = make_board(_cfg(), state.memory)
    t = board.get_task(task_id)
    if not t:
        raise typer.Exit(code=_fail(f"{task_id} not found"))
    t.status = Status.CUT
    board.update_task(t, ["status"])
    dependents = [x.id for x in board.list_tasks() if task_id in x.depends_on and x.status is not Status.DONE]
    console.print(f"{t.id} cut")
    if dependents:
        console.print(f"[yellow]warning: {', '.join(dependents)} depend on {t.id} and will never be promoted; "
                      f"edit their dependencies or cut them too.[/yellow]")


@app.command()
def split(task_id: str, apply: bool = typer.Option(False, "--apply")):
    """Ask the planner to split a task into smaller ones; cuts the original when applied."""
    from .planner import Planner
    board = make_board(_cfg(), state.memory)
    t = board.get_task(task_id)
    if not t:
        raise typer.Exit(code=_fail(f"{task_id} not found"))
    if t.status in (Status.CUT, Status.DONE):   # a second split of a cut task made five duplicates (Oct 5 2026)
        raise typer.Exit(code=_fail(f"{task_id} is {t.status.value}: it was already split or finished; check the board"))
    pl = Planner(_cfg(), board, _workspace(_cfg()), log=console.print)
    proposals = pl.propose(_cfg().repo_root / "PLAN.md", split_of=t)
    _print_proposals(proposals)
    if proposals and (apply or typer.confirm("create these and cut the original?", default=True)):
        pl.apply(proposals)
        t.status = Status.CUT
        board.update_task(t, ["status"])


@app.command()
def answer(question_id: str, text: str,
           follow_up: bool = typer.Option(False, "--follow-up", help="fyi questions: create a follow-up task")):
    """Answer a question (same as typing in Notion)."""
    board = make_board(_cfg(), state.memory)
    qs = [q for q in board.list_questions() if q.id == question_id]
    if not qs:
        raise typer.Exit(code=_fail(f"{question_id} not found"))
    q = qs[0]
    q.answer, q.needs_follow_up = text, follow_up
    board.update_question(q, ["answer", "needs_follow_up"])
    console.print(f"{q.id} answered; serve will relay it within {_cfg().serve_seconds}s")


@app.command()
def status():
    """Print the status page."""
    from .status import render_status
    board = make_board(_cfg(), state.memory)
    console.print(render_status(_cfg(), board.list_tasks(), board.list_agents(), board.list_questions(),
                                utcnow()))


@app.command()
def reroute():
    """Re-run routing for Ready tasks whose agent is offline, cooling down, or unknown."""
    from .serve import Server
    board = make_board(_cfg(), state.memory)
    srv = Server(_cfg(), board, _workspace(_cfg()), reviewer=None, merger=None, log=console.print)
    console.print(f"rerouted {srv.reroute()} tasks")


@app.command()
def run(agent: str = typer.Option(None, "--agent", help="only this agent"),
        once: bool = typer.Option(False, "--once"),
        dry_run: bool = typer.Option(False, "--dry-run", help="compile the next prompt and stop")):
    """Worker loop for every agent on this host."""
    from .prompt import compile_prompt, load_rules
    from .runner import Runner, SyncExecutor
    if not state.host:
        raise typer.Exit(code=_fail("set SWARM_HOST or pass --host"))
    if state.host not in _cfg().hosts:
        raise typer.Exit(code=_fail(f"host '{state.host}' is not in config.hosts ({', '.join(_cfg().hosts)})"))
    from .doctor import sleep_warning
    if (w := sleep_warning(cwd=_cfg().repo_root)):
        console.print(f"[yellow]WARNING: {w}[/yellow]")
    board = make_board(_cfg(), state.memory)
    r = Runner(_cfg(), board, state.host, _workspace(_cfg()), ledger=_ledger(_cfg()),
               log=console.print, executor=SyncExecutor() if once else None)
    if agent:
        if agent not in r.agents:
            raise typer.Exit(code=_fail(f"{agent} is not an agent on host {state.host}"))
        r.agents = {agent: r.agents[agent]}
        r.active = {agent: set()}
    if dry_run:
        tasks = r.pending_tasks()
        if not tasks:
            console.print("no pending tasks")
            raise typer.Exit()
        first = tasks[0]
        provider = r.agents[first.agent].provider if first.agent in r.agents else "claude"
        console.print(compile_prompt(first, _cfg(), rules_text=load_rules(), deps_summaries={},
                                     structured_output_supported=True,
                                     skills=_cfg().skills_for(first.type, first.importance),
                                     skill_tool=provider == "claude"))
        raise typer.Exit()
    r.loop(once=once)


@app.command()
def serve(no_review: bool = typer.Option(False, "--no-review"), no_merge: bool = typer.Option(False, "--no-merge"),
          once: bool = typer.Option(False, "--once")):
    """Control loop: reap, retry, relay answers, promote, review, merge, reroute, status page."""
    from .merge import Merger
    from .reviewer import Reviewer
    from .serve import Server
    board = make_board(_cfg(), state.memory)
    ws = _workspace(_cfg())

    class NoReview:
        def process(self, task):
            return task

    class NoMerge:
        def merge(self, task):
            return False

    srv = Server(_cfg(), board, ws,
                 reviewer=NoReview() if no_review else Reviewer(_cfg(), board, ws, log=console.print, ledger=_ledger(_cfg())),
                 merger=NoMerge() if no_merge else Merger(_cfg(), board, ws, log=console.print),
                 log=console.print, host=state.host or "serve", background=not once)
    if once:
        srv.acquire_lock()
        console.print(srv.tick())
        return
    srv.loop()


@app.command()
def logs(task_id: str, attempt: int = typer.Option(None, "--attempt")):
    """Show prompt/stdout/stderr for a task's run on this laptop."""
    base = Path.home() / ".swarm" / _cfg().project / "runs" / task_id
    if not base.exists():
        raise typer.Exit(code=_fail(f"no logs under {base}"))
    attempts = sorted(base.glob("attempt-*"))
    if not attempts:
        raise typer.Exit(code=_fail(f"no logs under {base}"))
    d = base / f"attempt-{attempt}" if attempt else attempts[-1]
    for name in ("prompt.md", "stdout.txt", "stderr.txt"):
        f = d / name
        if f.exists():
            console.rule(name)
            console.print(f.read_text()[-6000:], markup=False, highlight=False)


@app.command()
def template(dest: Path):
    """Copy the hackathon-base template into a new directory."""
    if dest.exists() and any(dest.iterdir()):
        raise typer.Exit(code=_fail(f"{dest} is not empty"))
    shutil.copytree(TEMPLATE_DIR, dest, dirs_exist_ok=True, symlinks=True)
    # skills live outside the template in the package (wheels drop dot-directories and symlinks): Claude reads
    # .claude/skills, Codex reads .agents/skills, so one copy plus a relative symlink
    shutil.copytree(SKILLS_DIR, dest / ".claude" / "skills", dirs_exist_ok=True)
    (dest / ".agents").mkdir(exist_ok=True)
    link = dest / ".agents" / "skills"
    if not link.exists() and not link.is_symlink():
        os.symlink(Path("..") / ".claude" / "skills", link)
    for script in (dest / "scripts").glob("*.sh"):
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
    console.print(f"template copied to {dest}. Next: edit .swarm/config.yaml, then `swarm doctor`.")


if __name__ == "__main__":
    app()
