import json
import os

from typer.testing import CliRunner

from swarm.cli import app, find_config
from swarm.doctor import run_checks

runner = CliRunner()


def test_find_config_walks_up(project_dir):
    sub = project_dir / "frontend" / "src"
    sub.mkdir(parents=True)
    assert find_config(sub) == project_dir / ".swarm" / "config.yaml"


def test_help_lists_commands():
    r = runner.invoke(app, ["--help"])
    assert r.exit_code == 0
    for cmd in ("doctor", "init", "plan", "add", "assign", "answer", "status", "run", "serve", "template"):
        assert cmd in r.output


def test_doctor_offline_checks(cfg):
    class R:
        ok, out, err = True, "v1", ""

    checks = run_checks(cfg, "host-a", offline=True, notion_token="",
                        which=lambda name: None if name == "codex" else f"/usr/bin/{name}",
                        run=lambda args, cwd, timeout=60: R())
    names = {c.name: c for c in checks}
    assert names["config"].ok and names["host"].ok
    assert names["cli:claude-a (claude)"].ok and not names["cli:codex-a (codex)"].ok
    assert not names["notion token"].ok


def test_add_dry_run_prints_route(project_dir):
    r = runner.invoke(app, ["--config", str(project_dir / ".swarm" / "config.yaml"), "--memory", "add",
                            "Build cards", "--type", "frontend", "--importance", "critical", "--size", "M"])
    assert r.exit_code == 0, r.output
    assert "claude-a" in r.output and "opus" in r.output and "T-001" in r.output


def test_status_memory(project_dir):
    r = runner.invoke(app, ["--config", str(project_dir / ".swarm" / "config.yaml"), "--memory", "status"])
    assert r.exit_code == 0 and "SWARM STATUS" in r.output


def test_template_copies(tmp_path):
    dest = tmp_path / "newproj"
    r = runner.invoke(app, ["template", str(dest)])
    assert r.exit_code == 0, r.output
    assert (dest / "AGENTS.md").exists() and (dest / ".swarm" / "config.yaml").exists()
    assert (dest / "scripts" / "verify_fast.sh").exists()
    assert os.access(dest / "scripts" / "verify_fast.sh", os.X_OK)
    assert (dest / ".claude" / "skills" / "test-driven-development" / "SKILL.md").exists()
    assert (dest / ".agents" / "skills").is_symlink()   # Codex reads .agents/skills; one source of truth
    assert (dest / ".agents" / "skills" / "verification-before-completion" / "SKILL.md").exists()


def test_unknown_host_is_a_clean_error(project_dir):
    r = runner.invoke(app, ["--config", str(project_dir / ".swarm" / "config.yaml"), "--memory", "--host", "nope", "run", "--once"])
    assert r.exit_code == 1 and "nope" in r.output and "Traceback" not in r.output


def test_logs_without_runs_is_a_clean_error(project_dir, monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    r = runner.invoke(app, ["--config", str(project_dir / ".swarm" / "config.yaml"), "--memory", "logs", "T-001"])
    assert r.exit_code == 1 and "no logs" in r.output


def test_subcommand_help_needs_no_config(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    r = runner.invoke(app, ["doctor", "--help"])
    assert r.exit_code == 0 and "offline" in r.output


def test_cut_warns_about_dependents(project_dir):
    base = ["--config", str(project_dir / ".swarm" / "config.yaml"), "--memory"]
    # --memory boards do not persist between invocations, so drive the command function directly
    from swarm import cli as cli_mod
    from swarm.board.memory import InMemoryBoard
    from swarm.config import load_config
    from swarm.models import Status, Task
    board = InMemoryBoard()
    board.create_task(Task(id="T-001", title="base", status=Status.READY))
    board.create_task(Task(id="T-002", title="dep", status=Status.BACKLOG, depends_on=["T-001"]))
    cli_mod.state.cfg = load_config(project_dir / ".swarm" / "config.yaml")
    cli_mod.state.memory = True
    cli_mod.make_board = lambda cfg, memory=False: board
    r = runner.invoke(app, base + ["cut", "T-001"])
    assert r.exit_code == 0 and "T-002" in r.output and "depend" in r.output


def test_assign_defaults_model_to_the_tasks_tier(project_dir):
    from swarm import cli as cli_mod
    from swarm.board.memory import InMemoryBoard
    from swarm.config import load_config
    from swarm.models import Status, Task
    board = InMemoryBoard()
    board.create_task(Task(id="T-001", title="hi", status=Status.READY, importance="high", type="backend"))
    cli_mod.state.cfg = load_config(project_dir / ".swarm" / "config.yaml")
    cli_mod.make_board = lambda cfg, memory=False: board
    r = runner.invoke(app, ["--config", str(project_dir / ".swarm" / "config.yaml"), "--memory", "assign", "T-001", "--agent", "codex-a"])
    assert r.exit_code == 0 and board.get_task("T-001").model == "gpt-6-astra" and board.get_task("T-001").effort == "high"


def test_doctor_checks_required_plugins(cfg):
    cfg.plugins_required = ["frontend-design", "humanizer"]

    class R:
        ok, err = True, ""
        out = json.dumps([{"id": "frontend-design@claude-plugins-official", "enabled": True, "installPath": "/x"},
                          {"id": "other@m", "enabled": True, "installPath": "/y"}])

    checks = run_checks(cfg, "host-a", offline=True, notion_token="", which=lambda n: f"/usr/bin/{n}",
                        run=lambda args, cwd, timeout=60: R())
    names = {c.name: c for c in checks}
    assert names["plugin:frontend-design"].ok and not names["plugin:humanizer"].ok
    assert "swarm tools install" in names["plugin:humanizer"].detail


def test_agents_sync_creates_never_seen_agents_as_offline(project_dir, monkeypatch):
    from swarm.board.memory import InMemoryBoard
    from swarm.cli import agents_sync, state
    from swarm.models import AgentRow
    board = InMemoryBoard()
    board.upsert_agent(AgentRow(name="claude-a", status="running", current_task="T-1"))
    monkeypatch.setattr("swarm.cli.make_board", lambda cfg, memory: board)
    state.memory, state.cfg = True, None
    monkeypatch.chdir(project_dir)
    agents_sync()
    assert board.get_agent("codex-a").status == "offline"      # never seen: no heartbeat yet
    assert board.get_agent("claude-a").status == "running"     # existing rows keep their state


def test_doctor_warns_when_the_laptop_can_sleep(cfg):
    """Four runner drops in the rehearsal were a sleeping laptop. doctor reads pmset on macOS and says so."""
    class R:
        def __init__(self, out): self.ok, self.out, self.err = True, out, ""

    def run(args, cwd, timeout=60):
        if args[:2] == ["pmset", "-g"]:
            return R(" System-wide power settings:\n sleep                10 (sleep prevented by caffeinate)\n disksleep 10\n")
        return R("")
    checks = {c.name: c for c in run_checks(cfg, "host-a", offline=True, notion_token="", which=lambda n: f"/usr/bin/{n}",
                                            run=run, platform="darwin")}
    assert checks["sleep"].ok and "caffeinate" in checks["sleep"].detail

    def run2(args, cwd, timeout=60):
        return R(" System-wide power settings:\n sleep                10\n") if args[:2] == ["pmset", "-g"] else R("")
    checks = {c.name: c for c in run_checks(cfg, "host-a", offline=True, notion_token="", which=lambda n: f"/usr/bin/{n}",
                                            run=run2, platform="darwin")}
    assert not checks["sleep"].ok and "caffeinate -dims" in checks["sleep"].detail
    assert "sleep" not in {c.name for c in run_checks(cfg, "host-a", offline=True, notion_token="",
                                                       which=lambda n: f"/usr/bin/{n}", run=run2, platform="linux")}


def test_template_ignores_the_planner_scratch_file():
    from swarm.cli import TEMPLATE_DIR
    assert ".swarm/tasks.proposed.json" in (TEMPLATE_DIR / ".gitignore").read_text()


def test_doctor_flags_broken_doc_refs_and_type_keys(project_dir, sample_config_dict):
    import yaml
    from swarm.config import load_config
    sample_config_dict["docs_by_type"] = {"_all": ["PLAN.md#summary", "PLAN.md#ground-rules"],
                                          "ml": ["docs/ARCHITECTURE.md"], "frontend": ["docs/NOPE.md"]}
    sample_config_dict["skills_by_type"] = {"ml": ["x"], "ml_audoi": ["y"]}
    (project_dir / ".swarm" / "config.yaml").write_text(yaml.safe_dump(sample_config_dict))
    (project_dir / "PLAN.md").write_text("# P\n\n## Summary\nhello\n\n## Ground rules\n- a\n")
    (project_dir / "docs").mkdir(exist_ok=True)
    (project_dir / "docs" / "ARCHITECTURE.md").write_text("# A\n")
    cfg = load_config(project_dir / ".swarm" / "config.yaml")

    class R:
        ok, out, err = True, "v1", ""

    checks = {c.name: c for c in run_checks(cfg, "host-a", offline=True, notion_token="x",
                                             which=lambda n: f"/usr/bin/{n}", run=lambda a, cwd, timeout=60: R())}
    assert not checks["docs refs"].ok
    assert "PLAN.md#ground-rules" in checks["docs refs"].detail and "docs/NOPE.md" in checks["docs refs"].detail
    assert "PLAN.md#summary" not in checks["docs refs"].detail
    assert not checks["type keys"].ok and "ml_audoi" in checks["type keys"].detail and "ml" not in checks["type keys"].detail.split()


def test_codex_smoke_initializes_git_and_uses_worker_arguments(cfg, tmp_path, monkeypatch):
    import subprocess
    from swarm.doctor import smoke_agent
    from swarm.models import RunResult, Usage
    cfg.agents["codex-a"].extra_args = ["-c", "sandbox_workspace_write.network_access=true"]
    smoke_dir = tmp_path / "fresh-smoke"
    class InspectAdapter:
        def run(self, spec):
            assert subprocess.run(["git", "rev-parse", "--is-inside-work-tree"],
                                  cwd=spec.cwd, capture_output=True, text=True).stdout.strip() == "true"
            assert spec.extra_args == cfg.agents["codex-a"].extra_args
            assert not (spec.cwd / ".swarm-run/report.json").exists()
            return RunResult(ok=True, exit_code=0, stdout="", stderr="", structured_output={"status": "done", "summary": "smoke ok"}, usage=Usage())
    monkeypatch.setattr("swarm.adapters.get_adapter", lambda a: InspectAdapter())
    assert smoke_agent(cfg, "codex-a", smoke_dir).ok
    (smoke_dir / ".swarm-run/report.json").write_text('{"status":"done"}')
    assert smoke_agent(cfg, "codex-a", smoke_dir).ok


def test_codex_smoke_git_failure_does_not_call_model(cfg, tmp_path, monkeypatch):
    from swarm.doctor import smoke_agent
    from swarm.workspace import CmdResult
    monkeypatch.setattr("swarm.doctor.run_cmd", lambda *a, **kw: CmdResult(1, "", "git init failed"))
    def unexpected(a):
        raise AssertionError("model must not run after git preparation fails")
    monkeypatch.setattr("swarm.adapters.get_adapter", unexpected)
    result = smoke_agent(cfg, "codex-a", tmp_path / "smoke")
    assert not result.ok and "git init failed" in result.detail
