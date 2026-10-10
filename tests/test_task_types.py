"""agents.<name>.task_types: an allowlist that keeps an agent off task types it must never get, wherever a task can
be handed to it (T-354, a frontend task, went to muse-c twice on Oct 10 although muse-c scores 1 on frontend)."""
import pytest
import yaml
from typer.testing import CliRunner

from swarm.board.memory import InMemoryBoard
from swarm.cli import app
from swarm.config import ConfigError, load_config
from swarm.doctor import task_types_check
from swarm.models import AgentRow, Status, Task, utcnow
from swarm.router import TYPE_OVERRIDE_FLAG, RouteContext, route

from .test_serve import make

runner = CliRunner()


def _restricted(project_dir, sample_config_dict, types=("research", "docs", "eval", "ml")):
    """fake-b becomes a research-only helper that would otherwise win frontend work on strength."""
    fake = sample_config_dict["agents"]["fake-b"]
    fake["strengths"] = {"frontend": 5, "backend": 5, "docs": 5, "research": 5, "tests": 5}
    fake["task_types"] = list(types)
    path = project_dir / ".swarm" / "config.yaml"
    path.write_text(yaml.safe_dump(sample_config_dict))
    return load_config(path)


def test_takes_matches_types_and_families(project_dir, sample_config_dict):
    cfg = _restricted(project_dir, sample_config_dict)
    fake = cfg.agents["fake-b"]
    assert fake.task_types == ["research", "docs", "eval", "ml"]
    assert fake.takes("research") and fake.takes("docs") and fake.takes("ml_audio") and fake.takes("ml_fusion")
    assert not fake.takes("frontend") and not fake.takes("tests") and not fake.takes("backend")
    assert cfg.agents["claude-a"].task_types is None and cfg.agents["claude-a"].takes("frontend")


def test_task_types_must_be_a_list(project_dir, sample_config_dict):
    sample_config_dict["agents"]["fake-b"]["task_types"] = "research"
    path = project_dir / ".swarm" / "config.yaml"
    path.write_text(yaml.safe_dump(sample_config_dict))
    with pytest.raises(ConfigError, match="task_types"):
        load_config(path)


def test_route_never_picks_an_agent_outside_its_allowlist(project_dir, sample_config_dict):
    cfg = _restricted(project_dir, sample_config_dict)
    ctx = RouteContext(now=utcnow())
    assert route(Task(id="T-1", title="", type="frontend"), cfg, ctx)[0] == "claude-a"
    assert route(Task(id="T-2", title="", type="backend"), cfg, ctx)[0] == "codex-a"
    assert route(Task(id="T-3", title="", type="research"), cfg, ctx)[0] == "fake-b"   # still wins its own types
    # deep queues spill only to allowed agents
    ctx = RouteContext(queue_depth={"codex-a": 9, "claude-a": 9}, now=utcnow())
    assert route(Task(id="T-4", title="", type="backend"), cfg, ctx)[0] in ("codex-a", "claude-a")
    # excluding every allowed agent falls back to the allowed ones, never to fake-b
    assert route(Task(id="T-5", title="", type="frontend"), cfg, RouteContext(now=utcnow()),
                 exclude={"claude-a", "codex-a"})[0] in ("claude-a", "codex-a")


def test_route_keeps_a_pinned_task_when_its_host_has_no_allowed_agent(project_dir, sample_config_dict):
    cfg = _restricted(project_dir, sample_config_dict)
    t = Task(id="T-1", title="", type="frontend", agent="claude-a", model="opus", flags=["host:host-b"])
    assert route(t, cfg, RouteContext(now=utcnow())) == ("claude-a", "opus", None)


def test_route_leaves_a_type_nobody_may_take_unassigned(project_dir, sample_config_dict):
    for name in ("claude-a", "codex-a"):
        sample_config_dict["agents"][name]["task_types"] = ["backend"]
    cfg = _restricted(project_dir, sample_config_dict)
    assert route(Task(id="T-1", title="", type="frontend"), cfg, RouteContext(now=utcnow()))[0] is None


def _online(board, *names):
    for n in names:
        board.upsert_agent(AgentRow(name=n, status="idle", last_heartbeat=utcnow()))


def test_rebalance_never_lets_an_idle_restricted_agent_steal(project_dir, sample_config_dict, git_repo, tmp_path):
    cfg = _restricted(project_dir, sample_config_dict)
    srv, board, _ = make(cfg, git_repo, tmp_path)
    _online(board, "claude-a", "codex-a", "fake-b")
    for i in range(3):
        board.create_task(Task(id="", title=f"fe{i}", status=Status.READY, agent="claude-a", type="frontend"))
    for i in range(2):
        board.create_task(Task(id="", title=f"run{i}", status=Status.RUNNING, agent="claude-a", type="frontend"))
    srv.rebalance()
    assert all(t.agent != "fake-b" for t in board.list_tasks())


def test_redistribute_on_return_never_hands_work_to_a_restricted_agent(project_dir, sample_config_dict, git_repo,
                                                                         tmp_path):
    cfg = _restricted(project_dir, sample_config_dict)
    srv, board, _ = make(cfg, git_repo, tmp_path)
    _online(board, "claude-a", "codex-a")
    board.upsert_agent(AgentRow(name="fake-b", status="offline", last_heartbeat=utcnow()))
    srv.redistribute_on_return()
    for i in range(3):
        board.create_task(Task(id="", title=f"fe{i}", status=Status.READY, agent="claude-a", type="frontend"))
    _online(board, "fake-b")
    srv.redistribute_on_return()
    assert all(t.agent == "claude-a" for t in board.list_tasks())


def test_reroute_moves_queued_work_off_a_disallowed_agent_unless_forced(project_dir, sample_config_dict, git_repo,
                                                                        tmp_path):
    cfg = _restricted(project_dir, sample_config_dict)
    srv, board, _ = make(cfg, git_repo, tmp_path)
    _online(board, "claude-a", "codex-a", "fake-b")
    wrong = board.create_task(Task(id="", title="T-354", status=Status.READY, agent="fake-b", type="frontend"))
    forced = board.create_task(Task(id="", title="forced", status=Status.READY, agent="fake-b", type="tests",
                                    flags=[TYPE_OVERRIDE_FLAG]))
    ok = board.create_task(Task(id="", title="research", status=Status.READY, agent="fake-b", type="research"))
    assert srv.reroute() == 1
    assert board.get_task(wrong.id).agent == "claude-a"
    assert board.get_task(forced.id).agent == "fake-b"
    assert board.get_task(ok.id).agent == "fake-b"


def test_promote_and_retry_route_within_the_allowlist(project_dir, sample_config_dict, git_repo, tmp_path):
    cfg = _restricted(project_dir, sample_config_dict)
    srv, board, _ = make(cfg, git_repo, tmp_path)
    _online(board, "claude-a", "codex-a", "fake-b")
    p = board.create_task(Task(id="", title="fe", status=Status.BACKLOG, type="frontend"))
    f = board.create_task(Task(id="", title="fe2", status=Status.FAILED, type="frontend", agent="claude-a",
                               attempts=1, last_error="tests failed"))
    srv.promote()
    srv.retry_failed()
    assert board.get_task(p.id).agent != "fake-b" and board.get_task(f.id).agent != "fake-b"


def _cli(project_dir, board, monkeypatch, *args):
    from swarm import cli as cli_mod
    monkeypatch.setattr(cli_mod, "make_board", lambda cfg, memory=False: board)
    return runner.invoke(app, ["--config", str(project_dir / ".swarm" / "config.yaml"), "--memory", *args])


def test_assign_refuses_a_disallowed_type_unless_forced(project_dir, sample_config_dict, monkeypatch):
    _restricted(project_dir, sample_config_dict)
    board = InMemoryBoard()
    board.create_task(Task(id="T-001", title="fe", status=Status.READY, type="frontend", agent="claude-a"))
    r = _cli(project_dir, board, monkeypatch, "assign", "T-001", "--agent", "fake-b")
    t = board.get_task("T-001")
    assert r.exit_code != 0 and "task_types" in r.output and "--force" in r.output and t.agent == "claude-a"
    r = _cli(project_dir, board, monkeypatch, "assign", "T-001", "--agent", "fake-b", "--force")
    t = board.get_task("T-001")
    assert r.exit_code == 0, r.output
    assert t.agent == "fake-b" and TYPE_OVERRIDE_FLAG in t.flags
    # handing it back to an allowed agent clears the override
    r = _cli(project_dir, board, monkeypatch, "assign", "T-001", "--agent", "claude-a")
    t = board.get_task("T-001")
    assert r.exit_code == 0 and t.agent == "claude-a" and TYPE_OVERRIDE_FLAG not in t.flags
    # allowed types assign without --force
    board.create_task(Task(id="T-002", title="r", status=Status.READY, type="ml_audio", agent="claude-a"))
    r = _cli(project_dir, board, monkeypatch, "assign", "T-002", "--agent", "fake-b")
    assert r.exit_code == 0 and board.get_task("T-002").agent == "fake-b"


def test_add_routes_within_the_allowlist(project_dir, sample_config_dict, monkeypatch):
    _restricted(project_dir, sample_config_dict)
    board = InMemoryBoard()
    r = _cli(project_dir, board, monkeypatch, "add", "a ui task", "--type", "frontend")
    assert r.exit_code == 0, r.output
    r = _cli(project_dir, board, monkeypatch, "add", "a research task", "--type", "research")
    assert r.exit_code == 0, r.output
    by_title = {t.title: t.agent for t in board.list_tasks()}
    assert by_title["a ui task"] == "claude-a" and by_title["a research task"] == "fake-b"


def test_doctor_validates_task_types(project_dir, sample_config_dict):
    cfg = _restricted(project_dir, sample_config_dict)
    c = task_types_check(cfg)
    assert c.ok and "fake-b" in c.detail
    sample_config_dict["agents"]["fake-b"]["task_types"] = ["reserch", "ml"]
    sample_config_dict["agents"]["codex-a"]["task_types"] = []
    sample_config_dict["agents"]["claude-a"]["task_types"] = ["backend"]
    path = project_dir / ".swarm" / "config.yaml"
    path.write_text(yaml.safe_dump(sample_config_dict))
    c = task_types_check(load_config(path))
    assert not c.ok
    assert "fake-b.task_types: unknown reserch" in c.detail
    assert "codex-a.task_types is empty" in c.detail
    assert "no agent may take type frontend" in c.detail
