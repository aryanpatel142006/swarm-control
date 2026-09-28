"""End to end: real runner, reviewer, merger, and server against an in-memory board, a temp git remote,
and a bash fake agent driven through the real GenericAdapter."""
import subprocess
from pathlib import Path

import yaml

from swarm.board.memory import InMemoryBoard
from swarm.config import load_config
from swarm.merge import Merger
from swarm.models import Status, Task
from swarm.reviewer import Reviewer
from swarm.runner import Runner, SyncExecutor
from swarm.serve import Server
from swarm.usage import Ledger
from swarm.workspace import CmdResult, Workspace

FAKE = Path(__file__).parent / "fake_agent.sh"


def e2e_config(project_dir, sample_config_dict):
    d = sample_config_dict
    d["hosts"] = {"lab": {"max_parallel": {"generic": 3}}}
    template = f"bash {FAKE} {{prompt_file}} {{model}} {{cwd}}"
    d["agents"] = {
        "fe": {"provider": "generic", "host": "lab", "parallel": 1,
               "models": {"best": "big", "high": "big", "mid": "mid", "low": "small"},
               "strengths": {"frontend": 5, "backend": 2}, "command_template": template},
        "be": {"provider": "generic", "host": "lab", "parallel": 2,
               "models": {"best": "big", "high": "big", "mid": "mid", "low": "small"},
               "strengths": {"frontend": 2, "backend": 5}, "command_template": template},
    }
    d["reviewer"] = {"agent": "be", "model": "mid"}
    d["planner"] = {"agent": "fe", "model": "big"}
    d["review_policy"] = "high_and_above"
    (project_dir / ".swarm" / "config.yaml").write_text(yaml.safe_dump(d))
    return load_config(project_dir / ".swarm" / "config.yaml")


def _git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True).stdout


def test_six_tasks_end_to_end(project_dir, sample_config_dict, git_repo, tmp_path, monkeypatch):
    cfg = e2e_config(project_dir, sample_config_dict)
    cfg.repo_root = git_repo
    monkeypatch.setenv("FAKE_STATE_DIR", str(tmp_path / "state"))
    (tmp_path / "state").mkdir()

    def gh(args, cwd):
        if args[:2] == ["pr", "merge"]:
            branch = args[2].replace("https://gh/pr/", "")   # merges go by PR url; the fake maps it back
            _git(git_repo, "fetch", "-q", "origin")
            _git(git_repo, "merge", "-q", "--no-edit", f"origin/{branch}")
            _git(git_repo, "push", "-q", "origin", "main")
            return CmdResult(0, "merged", "")
        if args[:2] == ["pr", "view"]:
            return CmdResult(1, "", "none")
        if args[:2] == ["pr", "create"]:
            return CmdResult(0, f"https://gh/pr/{args[3]}\n", "")
        return CmdResult(0, "", "")

    board = InMemoryBoard()
    ws = Workspace(git_repo, tmp_path / "wt", gh=gh)

    def quiet(*a, **k):
        return None

    runner = Runner(cfg, board, "lab", ws, ledger=Ledger(tmp_path / "usage.jsonl"), sleep=lambda s: None,
                    executor=SyncExecutor(), log=quiet, rules_text="rules", log_dir=tmp_path / "logs")
    srv = Server(cfg, board, ws, reviewer=Reviewer(cfg, board, ws, log=quiet, prompt_text="review rules"),
                 merger=Merger(cfg, board, ws, log=quiet), sleep=lambda s: None, log=quiet)

    t1 = board.create_task(Task(id="", title="Contract", type="backend", importance="critical", size="S"))
    t2 = board.create_task(Task(id="", title="API", type="backend", importance="high", size="M", depends_on=[t1.id]))
    t3 = board.create_task(Task(id="", title="UI", type="frontend", importance="normal", size="M", depends_on=[t1.id]))
    t4 = board.create_task(Task(id="", title="Docs BLOCKME", type="docs", importance="low", size="S"))
    t5 = board.create_task(Task(id="", title="Flaky FAILME", type="backend", importance="normal", size="S"))
    t6 = board.create_task(Task(id="", title="Integration", type="integration", importance="high", size="L",
                                depends_on=[t2.id, t3.id]))
    finished = (t1.id, t2.id, t3.id, t5.id, t6.id)

    assert srv.acquire_lock()
    for _ in range(12):
        srv.tick()
        runner.tick()
        statuses = {t.id: t.status for t in board.list_tasks()}
        if statuses[t4.id] is Status.BLOCKED and all(statuses[t] is Status.DONE for t in finished):
            break
    statuses = {t.id: t.status for t in board.list_tasks()}
    assert all(statuses[t] is Status.DONE for t in finished), statuses
    assert board.get_task(t5.id).attempts == 2
    assert statuses[t4.id] is Status.BLOCKED
    q = board.list_questions(status="Open")[0]
    assert q.task_id == t4.id and q.text == "which way?"

    # answer the question; relay unblocks; the fake finishes
    q.answer = "a"
    board.update_question(q, ["answer"])
    for _ in range(4):
        srv.tick()
        runner.tick()
    assert board.get_task(t4.id).status is Status.DONE
    assert board.list_questions()[0].status == "Applied"

    # main has every task's file and decision log
    _git(git_repo, "pull", "-q", "origin", "main")
    for t in (t1, t2, t3, t4, t5, t6):
        assert (git_repo / "src" / f"{t.id}.txt").exists()
        assert (git_repo / "docs" / "decisions" / f"{t.id}.md").exists()
    # reviewed tasks got a review round on their card; normal ones did not
    assert any(h.startswith("Review") for h, _ in board.reports[t2.id])
    assert not any(h.startswith("Review") for h, _ in board.reports[t3.id])
    assert "SWARM STATUS" in board.status_page
