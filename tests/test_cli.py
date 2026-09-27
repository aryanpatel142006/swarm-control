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
