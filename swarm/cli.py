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
    ws.hook_options = hook_options(cfg)
    return ws


def hook_options(cfg: Config) -> dict | None:
    """install_hooks() arguments from config `hooks:`; None when the project turned the hooks off."""
    h = cfg.hooks
    if not h.strip_trailers and not h.pre_push:
        return None
    return {"main": h.strip_trailers_main, "pre_push": h.pre_push, "pre_push_verify": h.pre_push_verify,
            "setup": cfg.verify.setup_worktree, "main_branch": cfg.main_branch}


def configure_verify_slots(ws, cfg: Config, host: str | None, log=print) -> None:
    """Every verify this process runs (runner, merger, reviewer) shares the machine's verify slots with the other
    swarm processes and the workers' own wrapped verifies (swarm/hostlock.py, Q-175/Q-185/Q-190)."""
    from .hostlock import lock_dir, verify_slot
    slots = cfg.hosts[host].max_parallel_verify if host in cfg.hosts else 2
    ws.verify_slot = lambda label, quick_wait_s=None: verify_slot(
        lock_dir(cfg.project), slots, log=log, label=label, exclusive_cap_min=cfg.verify.exclusive_max_minutes,
        quick_cap_s=quick_wait_s)
    ws.script_env = {**cfg.project_env(), **cfg.scratch_env(host)}


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
                                                              "record which ones work on this laptop's board row"),
           dupes: bool = typer.Option(False, "--dupes", help="only list task/question ids held by more than one "
                                                             "board page")):
    """Check tokens, CLIs, git, gh, verify scripts."""
    if dupes:
        from .status import duplicate_ids
        board = make_board(_cfg(), state.memory)
        found = duplicate_ids(board.list_tasks(), board.list_questions())
        if not found:
            console.print("no duplicate ids on the board")
            raise typer.Exit(code=0)
        for k, rows in found.items():
            console.print(f"[red]{k}[/red] on {len(rows)} pages:")
            for r in rows:
                if isinstance(r, Task):
                    row = f"{r.status.value:<14} {r.agent or '-':<14} {r.title[:60]}"
                else:
                    row = f"{r.status:<14} {r.kind:<14} {r.text[:60]}"
                console.print(f"  page {r.page_id}  " + " ".join(row.split()), soft_wrap=True, markup=False)
        console.print("Rename the newer page's ID (e.g. T-284-dup-of-T-290) or cut it; `swarm add` and question "
                      "creation renumber new rows themselves after the Oct 10 fix.")
        raise typer.Exit(code=1)
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
def retro(dry_run: bool = typer.Option(False, "--dry-run", help="print the findings; write and commit nothing"),
          milestone: str = typer.Option("", "--milestone", help="only this milestone's tasks (serve does this when "
                                                                 "a milestone completes); default: the whole board")):
    """Learn from this board: lessons into docs/LESSONS.md, safe config changes into .swarm/tuning.yaml, report in docs/retro/."""
    from .retro import run_retro
    from .usage import Ledger, default_ledger_path
    cfg = _cfg()
    ledger = Ledger(default_ledger_path(cfg.project), board=(cfg.notion.tasks_ds or "")[:8])
    found = run_retro(cfg, make_board(cfg, state.memory), _workspace(cfg), ledger=ledger, log=console.print, dry_run=dry_run,
                      milestone=milestone or None)
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
    from .relay import cli_sender, orchestrator_host
    from .relay import tell as _tell
    board = make_board(_cfg(), state.memory)
    host = state.host or os.environ.get("SWARM_HOST", "")
    q = _tell(board, task_id, text, sender=cli_sender(), host=host, orch_host=orchestrator_host(_cfg(), board))
    if q is None:
        raise typer.Exit(code=_fail(f"{task_id} not found or already closed"))
    console.print(f"{q.id}: {q.text}")


_VALUE_OPTS = ("--config", "--host")


def runner_pids(ps_output: str, me: int) -> list[int]:
    """Pids of `swarm run` processes in `ps -axo pid=,command=` output: a Python or `swarm` executable whose swarm
    subcommand is `run`. Never caffeinate (SIGUSR1 would kill it), a shell whose command line merely contains the
    words, or this process (the old `pgrep -f "swarm run"` matched all of those)."""
    pids = []
    for line in ps_output.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) != 2 or not parts[0].isdigit() or int(parts[0]) == me:
            continue
        argv = parts[1].split()
        exe = os.path.basename(argv[0])
        if exe == "swarm":
            rest = argv[1:]
        elif exe.startswith("python"):
            i = next((k for k, tok in enumerate(argv[1:], 1)
                      if os.path.basename(tok) == "swarm" or tok in ("swarm.cli", "swarm")), None)
            if i is None:
                continue
            rest = argv[i + 1:]
        else:
            continue
        skip = False
        for tok in rest:
            if skip:
                skip = False
                continue
            if tok in _VALUE_OPTS:
                skip = True
                continue
            if tok.startswith("-"):
                continue
            if tok == "run":
                pids.append(int(parts[0]))
            break
    return pids


@app.command()
def restart():
    """Gracefully restart the runner(s) on this laptop: they finish in-flight work, then re-exec on the current code."""
    import signal
    import subprocess
    r = subprocess.run(["ps", "-axo", "pid=,command="], capture_output=True, text=True)
    pids = runner_pids(r.stdout, os.getpid())
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
        if row.status == "removed":   # back in config (or never really gone): offline until its next heartbeat
            row.status, row.current_task, row.cooldown_until, row.note = "offline", "", None, ""
        elif row.note == "removed from config":   # a self-heartbeating agent kept the retirement note (muse-b, Oct 10)
            row.note = ""
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
    from .policy import apply_task_lint, main_exists, open_task_collisions
    open_tasks = [o for o in board.list_tasks() if o.status not in (Status.DONE, Status.CUT)]
    for n in apply_task_lint(t, None if state.memory else main_exists(_workspace(_cfg())), open_tasks):
        console.print(f"lint: {n}")
    for other, files in open_task_collisions(t, open_tasks):
        console.print(f"[yellow]lint: {other.id} is open and names {', '.join(files)}: add `--depends {other.id}` "
                      "or put the edit in one task (one owner per shared file at a time; Q-288)[/yellow]")
    from .policy import shell_expansion_hints
    for n in shell_expansion_hints(f"{title}\n{description}\n{acceptance}"):
        console.print(f"[yellow]lint: {n}[/yellow]")
    t.status = Status.READY if all(d in done for d in t.depends_on) else Status.BACKLOG
    t.agent, t.model, t.effort = route(t, _cfg(), context_from_board(board, _cfg()))
    if not t.agent:
        console.print(f"[yellow]no configured agent may take type {type}{' on ' + host if host else ''} "
                      "(agents' task_types / --host): the task is unassigned; use `swarm assign`[/yellow]")
    t = board.create_task(t)
    console.print(f"{t.id} {t.status.value} → {t.agent} / {t.model} / {t.effort}: {t.title}")


@app.command()
def assign(task_id: str, agent: str = typer.Option(..., "--agent"), model: str = typer.Option(None),
           effort: str = typer.Option(None),
           force: bool = typer.Option(False, "--force", help="If another agent is already running the task, stop "
                                                             "that run and re-queue it for the new agent. Also "
                                                             "overrides the agent's task_types allowlist.")):
    """Override routing for one task."""
    board = make_board(_cfg(), state.memory)
    t = board.get_task(task_id)
    if not t:
        raise typer.Exit(code=_fail(f"{task_id} not found"))
    if agent not in _cfg().agents:
        raise typer.Exit(code=_fail(f"unknown agent {agent}"))
    from .router import TYPE_OVERRIDE_FLAG
    fields = ["agent", "model", "effort"]
    allowed = _cfg().agents[agent].takes(t.type)
    if not allowed and not force:
        raise typer.Exit(code=_fail(
            f"{agent} only takes task types {', '.join(_cfg().agents[agent].task_types or [])} (task_types in "
            f"config); {t.id} is type {t.type}. Pick another agent, or re-run with --force to override."))
    if not allowed and TYPE_OVERRIDE_FLAG not in t.flags:
        t.flags = t.flags + [TYPE_OVERRIDE_FLAG]      # serve's reroute leaves a forced assignment where it is
        fields.append("flags")
        console.print(f"[yellow]{t.id}: type {t.type} is outside {agent}'s task_types; assigned anyway (--force)[/yellow]")
    elif allowed and TYPE_OVERRIDE_FLAG in t.flags:
        t.flags = [f for f in t.flags if f != TYPE_OVERRIDE_FLAG]
        fields.append("flags")
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
    new_host = _cfg().agents[agent].host
    if t.pinned_host and t.pinned_host != new_host:
        # a pin to another host would leave the task unclaimed forever (T-362/T-364/T-354 sat 40 min, Oct 10)
        console.print(f"[yellow]{t.id}: pin host:{t.pinned_host} replaced by host:{new_host} ({agent}'s host)[/yellow]")
        t.flags = [f for f in t.flags if not f.startswith("host:")] + [f"host:{new_host}"]
        if "flags" not in fields:
            fields.append("flags")
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
           follow_up: bool = typer.Option(False, "--follow-up", help="fyi questions: create a follow-up task"),
           kind: str = typer.Option("", "--kind", help="pick one row when two questions share an id"),
           task: str = typer.Option("", "--task", help="pick one row when two questions share an id"),
           all_rows: bool = typer.Option(False, "--all", help="answer every row with this id"),
           keep_blocked: bool = typer.Option(False, "--keep-blocked", help="blocking questions: close the question "
                                             "but leave the task Blocked (serve does not resume it)")):
    """Answer a question (same as typing in Notion)."""
    board = make_board(_cfg(), state.memory)
    qs = [q for q in board.list_questions() if q.id == question_id]
    if not qs:
        raise typer.Exit(code=_fail(f"{question_id} not found"))
    picked = [q for q in qs if (not kind or q.kind == kind) and (not task or q.task_id == task)]
    if not picked:
        raise typer.Exit(code=_fail(f"no {question_id} row matches --kind {kind or '*'} --task {task or '*'}"))
    open_rows = [q for q in picked if q.status == "Open" and not q.answer]
    if len(picked) > 1 and not all_rows and len(open_rows) == 1:
        # the cross-laptop id race (Q-1978 twice, Oct 11): the other rows are answered already, so the open one is
        # the only one this answer can be for
        others = ", ".join(f"{q.task_id or '-'} ({q.status})" for q in picked if q is not open_rows[0])
        console.print(f"{question_id} is shared with closed rows ({others}); answering the open one on "
                      f"{open_rows[0].task_id or '-'}")
        picked = open_rows
    if len(picked) > 1 and not all_rows:
        # two rows once shared Q-209 and `answer` closed only the first (Oct 6): never guess which one
        for q in picked:
            console.print(f"  {q.id} · kind={q.kind} · task={q.task_id or '-'} · {q.status} · {q.text[:100]}")
        raise typer.Exit(code=_fail(f"{len(picked)} questions share {question_id}: add --kind/--task, or --all"))
    from .relay import KEEP_BLOCKED_MARK, cli_sender, keeps_blocked, sender_tag
    by = sender_tag(cli_sender(), state.host or os.environ.get("SWARM_HOST", ""))   # serve labels the relay with it
    if keep_blocked and not keeps_blocked(text):
        text = f"{KEEP_BLOCKED_MARK} {text}"
    for q in picked:
        q.answer, q.needs_follow_up, q.answered_by = text, follow_up, by
        board.update_question(q, ["answer", "needs_follow_up", "answered_by"])
    console.print(f"{question_id} answered ({len(picked)} row{'s' if len(picked) > 1 else ''}); serve will relay it "
                  f"within {_cfg().serve_seconds}s")


@app.command()
def status():
    """Print the status page."""
    from .status import render_status
    board = make_board(_cfg(), state.memory)
    console.print(render_status(_cfg(), board.list_tasks(), board.list_agents(), board.list_questions(),
                                utcnow()))


@app.command()
def hooks(install: bool = typer.Option(False, "--install", help="install or refresh the hooks (config `hooks:`)"),
          main: bool = typer.Option(None, "--main/--no-main", help="also strip AI trailers in the main checkout"),
          pre_push: bool = typer.Option(None, "--pre-push/--no-pre-push",
                                        help="pushes to main refuse commits that carry AI trailers"),
          verify: bool = typer.Option(None, "--verify/--no-verify",
                                      help="the pre-push gate also runs verify.fast on the pushed commit")):
    """Git hooks: the commit-msg hook that strips AI attribution trailers (every worktree; the main checkout with
    --main) and the opt-in pre-push gate for main. Without --install: show what is installed."""
    from .githooks import hook_status, install_hooks
    cfg = _cfg()
    if install:
        opts = hook_options(cfg) or {"main": None, "pre_push": False, "pre_push_verify": None,
                                     "setup": cfg.verify.setup_worktree, "main_branch": cfg.main_branch}
        if main is not None:
            opts["main"] = main
        if pre_push is not None:
            opts["pre_push"] = pre_push
        if verify is not None:
            opts["pre_push_verify"] = cfg.verify.fast if verify else None
            opts["pre_push"] = opts["pre_push"] or verify
        install_hooks(cfg.repo_root, **opts)
    st = hook_status(cfg.repo_root)
    console.print(f"hooks dir: {st['dir']}")
    console.print(f"commit-msg (strip AI trailers): {'installed' if st['commit-msg'] else 'NOT installed'}"
                  f" · main checkout: {'yes' if st['main'] else 'no (worktrees only)'}")
    console.print(f"pre-push gate on {cfg.main_branch}: " + ("NOT installed" if not st["pre-push"] else
                  "refuses AI trailers" + (f", runs {st['pre-push-verify']}" if st["pre-push-verify"] else "")))
    if not install and not st["commit-msg"]:
        console.print("run `swarm hooks --install` (runners also install it before every worktree)")


def _dispatcher(cfg: Config):
    from . import dispatch as d
    return d.Dispatcher(cfg, _workspace(cfg), adapter_factory=d.get_adapter, log=console.print)


@app.command()
def dispatch(name: str = typer.Option(..., "--name", help="job name: letters, digits, . _ - (its branch is job/<name>)"),
             prompt_file: str = typer.Option(..., "--prompt-file",
                                             help="file with the prompt, or - for stdin (never pass it on argv)"),
             account: str = typer.Option(None, "--account", help="config `accounts:` name or an agent name"),
             model: str = typer.Option(None, "--model"), effort: str = typer.Option(None, "--effort"),
             push: str = typer.Option(None, "--push", help="branch (default) | main | none"),
             verify: bool = typer.Option(None, "--verify/--no-verify", help="verify.fast before a push to main"),
             cwd: str = typer.Option(None, "--cwd", help="run in this directory instead of a fresh worktree "
                                                         "(research jobs; nothing is committed or pushed)"),
             replace: bool = typer.Option(False, "--replace", help="reuse the name of a finished job"),
             foreground: bool = typer.Option(False, "--foreground", help="run here and wait (default: detached)")):
    """Run one headless agent job outside the board: own worktree, prompt via file/stdin, per-job logs, account and
    model fallback, waits out a limit and retries. `swarm jobs` lists them."""
    import sys
    cfg = _cfg()
    text = sys.stdin.read() if prompt_file == "-" else Path(prompt_file).expanduser().read_text()
    d = _dispatcher(cfg)
    try:
        job = d.create(name, text, account=account, model=model, effort=effort, push=push, verify=verify, cwd=cwd,
                       replace=replace)
    except ValueError as e:
        raise typer.Exit(code=_fail(str(e)))
    where = d.job_dir(job.name)
    if foreground:
        job = d.run(job, install_signals=True)
        console.print(f"{job.name}: {job.status}" + (f" · {job.pushed_to}" if job.pushed_to else "")
                      + (f" · {job.error}" if job.error else "") + f" · report {where / 'report.md'}")
        raise typer.Exit(code=0 if job.status == "done" else 1)
    pid = d.start_background(job, host=state.host)
    console.print(f"{job.name} started (pid {pid}) on {job.account}/{job.model} · push {job.push} · logs {where}")


@app.command()
def jobs(name: str = typer.Argument(None, help="one job: its details, final report and log tail"),
         stop: bool = typer.Option(False, "--stop", help="stop that job (its CLI and supervisor); work is kept"),
         limit: int = typer.Option(20, "--limit")):
    """Running and finished `swarm dispatch` jobs with their final reports."""
    cfg = _cfg()
    d = _dispatcher(cfg)
    if name:
        try:
            job = d.load(name)
        except OSError:
            raise typer.Exit(code=_fail(f"no job {name} under {d.root}"))
        if stop:
            console.print(f"{name}: stop sent" if d.stop(name) else f"{name} is not running")
            return
        console.print(f"{job.name} · {job.live_status()} · {job.account}/{job.model} · push {job.push} · "
                      f"created {job.created} · finished {job.finished or '-'}", markup=False)
        for a in job.attempts:
            console.print(f"  attempt {a.get('n')}: {a.get('account')}/{a.get('model')} {a.get('started', '')} → "
                          f"{a.get('finished', '…')} exit {a.get('exit', '-')}"
                          + (f" limited until {a['limited_until']}" if a.get("limited_until") else "")
                          + (f" {a['error'][:80]}" if a.get("error") else ""), markup=False)
        if job.retry_at:
            console.print(f"  waiting for a limit; retry at {job.retry_at}")
        if job.pushed_to:
            console.print(f"  pushed to {job.pushed_to}: " + "; ".join(job.commits[:5]), markup=False)
        if job.error:
            console.print(f"  error: {job.error}", markup=False)
        rep = d.job_dir(name) / "report.md"
        console.rule("report")
        console.print(rep.read_text() if rep.exists() else (job.report or "(no report yet)"), markup=False,
                      highlight=False)
        return
    rows = d.list_jobs()[:limit]
    if not rows:
        console.print(f"no jobs under {d.root}")
        return
    table = Table("job", "status", "account/model", "created", "attempts", "result")
    for j in rows:
        st = j.live_status()
        result = (j.pushed_to or "") + (f" retry {j.retry_at[11:16]}" if st == "waiting_limit" else "")
        first = (j.report or j.error or "").strip().splitlines()[:1]
        table.add_row(j.name, st, f"{j.account}/{j.model}", j.created[5:16].replace("T", " "),
                      str(len(j.attempts)), (result + (" · " if result and first else "") + (first[0] if first else ""))[:70])
    console.print(table)


@app.command()
def watch(period: int = typer.Option(45, "--period", help="seconds between board reads"),
          min_task: int = typer.Option(0, "--min-task", help="only tasks numbered at least this (T-129 → 129)"),
          mute_host: list[str] = typer.Option([], "--mute-host", help="hide this host's task moves and chatter; "
                                              "blocking/harness questions and asks to the orchestrator still show"),
          always: list[str] = typer.Option([], "--always", help="a word that marks a question as addressed to you "
                                                                "(e.g. your name); repeatable"),
          once: bool = typer.Option(False, "--once", help="print the armed line and stop (tests, cron)")):
    """One line per new/reopened question and task status change, for a Monitor. Never drops a question addressed
    to the orchestrator."""
    import time as _time
    from .relay import orchestrator_host
    from .watch import Watcher, snapshot
    cfg = _cfg()
    board = make_board(cfg, state.memory)
    w = Watcher(cfg, local_host=state.host or "", mute_hosts=mute_host, always=always,
                orch_host=orchestrator_host(cfg, board, fallback=state.host or ""))
    prev = snapshot(board.list_tasks(), board.list_questions(), min_task=min_task)
    open_q = [q for q in prev.questions.values() if q.status == "Open"]
    must = [q for q in open_q if w.must_show(q)]
    console.print(f"watch armed: {len(prev.tasks)} tasks, {len(prev.questions)} questions; open: {len(open_q)}"
                  + (f"; for you: {', '.join(f'{q.id}({q.kind},{q.task_id or chr(45)})' for q in must[:12])}"
                     if must else ""), markup=False, highlight=False, soft_wrap=True)
    if once:
        return
    while True:
        _time.sleep(period)
        try:
            cur = snapshot(board.list_tasks(), board.list_questions(), min_task=min_task)
        except Exception as e:  # noqa: BLE001 - a board hiccup is one line, never the end of the watch
            console.print(f"watch error: {type(e).__name__}: {str(e)[:120]}", markup=False)
            continue
        for line in w.diff(prev, cur):
            console.print(line, markup=False, highlight=False, soft_wrap=True)
        prev = cur


@app.command()
def hold(reason: str = typer.Argument(..., help='why, e.g. "live test"'),
         minutes: float = typer.Option(60, "--minutes", help="the hold ends by itself after this long"),
         scope: str = typer.Option(None, "--scope", help="comma list of locks,claims,reviews (default: config "
                                                         "`hold.scope`, which defaults to all three)")):
    """Live test hold on this machine: swarm-lock waits (locks), the runner here starts no new task (claims) and
    serve here starts no review or merge (reviews), so nothing disturbs a human test."""
    import socket
    from .hostlock import HOLD_SCOPES, hold_text, lock_dir, read_hold, write_hold
    d = lock_dir(_cfg().project)
    parts = [x.strip() for x in scope.split(",") if x.strip()] if scope else list(_cfg().hold_scope)
    bad = [x for x in parts if x not in HOLD_SCOPES]
    if bad:
        raise typer.Exit(code=_fail(f"--scope: unknown {', '.join(bad)}; use {','.join(HOLD_SCOPES)}"))
    write_hold(d, reason, minutes, by=state.host or socket.gethostname(), scope=parts)
    console.print(f"HOLD set on this machine: {hold_text(read_hold(d))}. `swarm release` ends it.")


@app.command()
def release():
    """End the live test hold."""
    from .hostlock import clear_hold, lock_dir
    console.print("hold released" if clear_hold(lock_dir(_cfg().project)) else "no hold was set")


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
        from .report import earlier_questions, earlier_questions_note
        try:
            asked = earlier_questions_note(earlier_questions(r.board.list_questions(), first.id))
        except Exception:  # noqa: BLE001 - a dry run without the earlier answers is still useful
            asked = ""
        console.print(compile_prompt(first, _cfg(), rules_text=load_rules(), deps_summaries={},
                                     structured_output_supported=True,
                                     skills=_cfg().skills_for(first.type, first.importance),
                                     skill_tool=provider == "claude", questions_note=asked))
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
                 log=console.print, host=state.host or "serve", background=not once, main_health=True)
    if once:
        srv.acquire_lock()
        summary = srv.tick()
        srv.join_reviews()   # reviewer.parallel > 1 reviews in threads; --once waits for them
        console.print(summary)
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
