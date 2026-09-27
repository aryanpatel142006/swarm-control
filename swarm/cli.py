"""The `swarm` command line."""
from __future__ import annotations

import json
import os
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
TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "template"


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
    return Workspace(cfg.repo_root, cfg.worktree_root, cfg.main_branch)


def _ledger(cfg: Config):
    from .usage import Ledger, default_ledger_path
    return Ledger(default_ledger_path(cfg.project))


@app.callback()
def main(ctx: typer.Context,
         config: Path = typer.Option(None, "--config", help="path to .swarm/config.yaml"),
         memory: bool = typer.Option(False, "--memory", help="use an in-memory board (dry runs)"),
         host: str = typer.Option(None, "--host", help="this laptop's name in config.hosts (or SWARM_HOST)")):
    state.cfg = None
    state.config_path = config
    state.memory = memory
    state.host = host or os.environ.get("SWARM_HOST")


@app.command()
def doctor(offline: bool = typer.Option(False, "--offline", help="skip network checks"),
           smoke: str = typer.Option(None, "--smoke", help="agent name to smoke-test with a 1-turn prompt")):
    """Check tokens, CLIs, git, gh, verify scripts."""
    from .doctor import run_checks, smoke_agent
    checks = run_checks(_cfg(), state.host, offline=offline)
    if smoke:
        checks.append(smoke_agent(_cfg(), smoke, _cfg().worktree_root / "_smoke"))
    table = Table("check", "ok", "detail")
    for c in checks:
        table.add_row(c.name, "[green]yes[/green]" if c.ok else "[red]NO[/red]", c.detail)
    console.print(table)
    raise typer.Exit(code=0 if all(c.ok for c in checks) else 1)


@app.command()
def init(parent_page: str = typer.Option(..., "--parent-page", help="Notion page id that holds the databases")):
    """Create the Notion databases, board views, and status page; save ids to .swarm/notion.yaml."""
    from .board.notion import NotionBoard, NotionClient
    token = os.environ.get("NOTION_TOKEN")
    if not token:
        raise typer.Exit(code=_fail("NOTION_TOKEN is not set"))
    ids = NotionBoard.init(NotionClient(token), parent_page, list(_cfg().agents))
    path = save_notion_ids(_cfg(), ids)
    console.print(f"created databases; ids saved to {path}")
    if not str(ids.get("views_ok", "")).startswith("true"):
        console.print("[yellow]board views could not be created via the API; add them by hand: "
                      "open each database → + view → Board → group by Status / Agent.[/yellow]")
    state.cfg = load_config(_cfg().path)
    agents_sync()


@app.command("agents-sync")
def agents_sync():
    """Refresh Agent rows (and the Agent select options) from config."""
    board = make_board(_cfg(), state.memory)
    for a in _cfg().agents.values():
        row = board.get_agent(a.name) or AgentRow(name=a.name)
        row.provider, row.host = a.provider, a.host
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
    pl = Planner(_cfg(), board, _workspace(_cfg()), log=console.print)
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
        scope: list[str] = typer.Option([], "--scope"), priority: int = typer.Option(100)):
    """Add one task, routed."""
    from .router import context_from_board, route
    if type not in TASK_TYPES or importance not in IMPORTANCES or size not in SIZES:
        raise typer.Exit(code=_fail(f"type/importance/size must be in {TASK_TYPES}/{IMPORTANCES}/{SIZES}"))
    board = make_board(_cfg(), state.memory)
    done = {t.id for t in board.list_tasks(status=[Status.DONE])}
    t = Task(id="", title=title, description=description, acceptance=acceptance, type=type, importance=importance,
             size=size, milestone=milestone, depends_on=list(depends), scope=list(scope), priority=priority)
    t.status = Status.READY if all(d in done for d in t.depends_on) else Status.BACKLOG
    t.agent, t.model, t.effort = route(t, _cfg(), context_from_board(board, _cfg()))
    t = board.create_task(t)
    console.print(f"{t.id} {t.status.value} → {t.agent} / {t.model} / {t.effort}: {t.title}")


@app.command()
def assign(task_id: str, agent: str = typer.Option(..., "--agent"), model: str = typer.Option(None),
           effort: str = typer.Option(None)):
    """Override routing for one task."""
    board = make_board(_cfg(), state.memory)
    t = board.get_task(task_id)
    if not t:
        raise typer.Exit(code=_fail(f"{task_id} not found"))
    if agent not in _cfg().agents:
        raise typer.Exit(code=_fail(f"unknown agent {agent}"))
    from .router import model_for, tier_for
    t.agent = agent
    default_model, default_effort = model_for(_cfg().agents[agent], tier_for(t, _cfg()), t.type, _cfg())
    t.model = model or default_model
    t.effort = effort or default_effort
    board.update_task(t, ["agent", "model", "effort"])
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
        console.print(compile_prompt(tasks[0], _cfg(), rules_text=load_rules(), deps_summaries={},
                                     structured_output_supported=True))
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
                 reviewer=NoReview() if no_review else Reviewer(_cfg(), board, ws, log=console.print),
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
    shutil.copytree(TEMPLATE_DIR, dest, dirs_exist_ok=True)
    for script in (dest / "scripts").glob("*.sh"):
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
    console.print(f"template copied to {dest}. Next: edit .swarm/config.yaml, then `swarm doctor`.")


if __name__ == "__main__":
    app()
