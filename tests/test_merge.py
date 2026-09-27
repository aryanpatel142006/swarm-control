import subprocess

from swarm.board.memory import InMemoryBoard
from swarm.merge import Merger
from swarm.models import Status, Task
from swarm.workspace import CmdResult, Workspace


def _git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True).stdout


def prep(cfg, git_repo, tmp_path, gh_ok=True, verify_ok=True):
    calls = []

    def gh(args, cwd):
        calls.append(args)
        if args[:2] == ["pr", "merge"]:
            if gh_ok:
                # emulate GitHub's squash-merge by merging the branch into main on the remote
                _git(git_repo, "fetch", "-q", "origin")
                _git(git_repo, "merge", "-q", "--no-edit", f"origin/{args[2]}")
                _git(git_repo, "push", "-q", "origin", "main")
                return CmdResult(0, "merged", "")
            return CmdResult(1, "", "not mergeable")
        return CmdResult(0, "", "")

    board = InMemoryBoard()
    ws = Workspace(git_repo, tmp_path / "wt", gh=gh)
    t = board.create_task(Task(id="", title="Feature", status=Status.MERGE_READY, agent="codex-a"))
    dep = board.create_task(Task(id="", title="Dependent", status=Status.BACKLOG, depends_on=[t.id]))
    wt = ws.provision(t.id)
    (wt / "feature.txt").write_text("f\n")
    if not verify_ok:
        (wt / "scripts" / "verify_fast.sh").write_text("#!/bin/sh\necho FASTFAIL; exit 1\n")
    ws.commit_all(wt, "feature")
    ws.push(wt, t.branch)
    ws.dispose(wt)
    return Merger(cfg, board, ws, log=lambda *a: None), board, t, dep, calls


def test_merge_success_marks_done(cfg, git_repo, tmp_path):
    m, board, t, dep, calls = prep(cfg, git_repo, tmp_path)
    assert m.merge(t) is True
    assert board.get_task(t.id).status is Status.DONE
    assert any(a[:2] == ["pr", "merge"] for a in calls)
    assert board.reports[t.id][-1][0] == "Merged"


def test_rebase_conflict_goes_back(cfg, git_repo, tmp_path):
    m, board, t, dep, calls = prep(cfg, git_repo, tmp_path)
    (git_repo / "feature.txt").write_text("conflict\n")
    _git(git_repo, "add", "feature.txt")
    _git(git_repo, "commit", "-qm", "main feature")
    _git(git_repo, "push", "-q", "origin", "main")
    assert m.merge(t) is False
    stored = board.get_task(t.id)
    assert stored.status is Status.CHANGES_REQUESTED and "feature.txt" in stored.feedback
    assert "resume" in stored.flags


def test_verify_failure_after_rebase_goes_back(cfg, git_repo, tmp_path):
    m, board, t, dep, calls = prep(cfg, git_repo, tmp_path, verify_ok=False)
    assert m.merge(t) is False
    stored = board.get_task(t.id)
    assert stored.status is Status.CHANGES_REQUESTED and "FASTFAIL" in stored.feedback


def test_gh_merge_failure_flags(cfg, git_repo, tmp_path):
    m, board, t, dep, calls = prep(cfg, git_repo, tmp_path, gh_ok=False)
    assert m.merge(t) is False
    stored = board.get_task(t.id)
    assert stored.status is Status.MERGE_READY and "merge_failed" in stored.flags
    assert "not mergeable" in stored.last_error
