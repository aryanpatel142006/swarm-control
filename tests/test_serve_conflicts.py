"""serve catches a branch that conflicts with main before a review is spent on it (Oct 9 2026: T-150 sat in Review
with its PR already CONFLICTING on GitHub)."""
import subprocess

from swarm.board.memory import InMemoryBoard
from swarm.models import AgentRow, Status, Task, utcnow
from swarm.serve import Server
from swarm.workspace import CmdResult, Workspace


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout


class CountingReviewer:
    def __init__(self):
        self.seen = []

    def process(self, task):
        self.seen.append(task.id)
        return task


def make(cfg, git_repo, tmp_path, gh=None):
    board = InMemoryBoard()
    ws = Workspace(git_repo, tmp_path / "wt", gh=gh or (lambda a, c: CmdResult(0, "", "")))
    logs: list[str] = []
    srv = Server(cfg, board, ws, reviewer=CountingReviewer(), merger=None, now=utcnow, sleep=lambda s: None,
                 log=lambda *a: logs.append(" ".join(str(x) for x in a)))
    return srv, board, logs


def branch(repo, task_id, files: dict[str, str], msg="work"):
    git(repo, "checkout", "-q", "-B", f"task/{task_id}", "origin/main")
    for name, text in files.items():
        (repo / name).write_text(text)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", f"{task_id}: {msg}")
    git(repo, "push", "-q", "-f", "origin", f"task/{task_id}")
    git(repo, "checkout", "-q", "main")


def main_commit(repo, files: dict[str, str], msg):
    for name, text in files.items():
        (repo / name).write_text(text)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", msg)
    git(repo, "push", "-q", "origin", "main")


def test_conflicting_review_task_goes_back_with_files_clean_one_untouched(cfg, git_repo, tmp_path):
    srv, board, logs = make(cfg, git_repo, tmp_path)
    bad = board.create_task(Task(id="", title="bad", status=Status.REVIEW, agent="claude-a", claim_nonce="old"))
    ok = board.create_task(Task(id="", title="ok", status=Status.REVIEW, agent="claude-a"))
    ready = board.create_task(Task(id="", title="mr", status=Status.MERGE_READY, agent="claude-a"))
    branch(git_repo, bad.id, {"README.md": "# branch version\n"})
    branch(git_repo, ok.id, {"other.txt": "new file\n"})
    branch(git_repo, ready.id, {"README.md": "# merge-ready version\n"})
    main_commit(git_repo, {"README.md": "# main moved on\n"}, "T-154 rewrite readme")
    assert srv.check_conflicts() == 2
    s = board.get_task(bad.id)
    assert s.status is Status.CHANGES_REQUESTED and s.claim_nonce == "" and "resume" in s.flags
    assert "README.md" in s.feedback and "T-154 rewrite readme" in s.feedback and "Caught by serve" in s.feedback
    assert board.get_task(ready.id).status is Status.CHANGES_REQUESTED
    assert board.get_task(ok.id).status is Status.REVIEW and board.get_task(ok.id).feedback == ""
    assert any(f"[{bad.id}] conflict with main (README.md) → changes requested" in ln for ln in logs)
    assert srv.check_conflicts() == 0          # the clean one is remembered for this (branch, main) pair


def test_changes_requested_and_busy_tasks_are_skipped(cfg, git_repo, tmp_path):
    srv, board, logs = make(cfg, git_repo, tmp_path)
    cr = board.create_task(Task(id="", title="cr", status=Status.CHANGES_REQUESTED, agent="claude-a",
                                feedback="reviewer said x"))
    mid = board.create_task(Task(id="", title="mid", status=Status.REVIEW, agent="claude-a"))
    for t in (cr, mid):
        branch(git_repo, t.id, {"README.md": f"# {t.id}\n"})
    main_commit(git_repo, {"README.md": "# main\n"}, "main change")
    board.upsert_agent(AgentRow(name="claude-a", current_task=mid.id))   # its runner is still publishing
    assert srv.check_conflicts() == 0
    assert board.get_task(cr.id).feedback == "reviewer said x"
    assert board.get_task(mid.id).status is Status.REVIEW
    board.upsert_agent(AgentRow(name="claude-a", current_task=""))
    srv._busy_ids.add(mid.id)                                           # a review of it is running here
    assert srv.check_conflicts() == 0
    srv._busy_ids.clear()
    assert srv.check_conflicts() == 1 and board.get_task(mid.id).status is Status.CHANGES_REQUESTED


def test_tick_checks_before_reviewing(cfg, git_repo, tmp_path):
    srv, board, logs = make(cfg, git_repo, tmp_path)
    t = board.create_task(Task(id="", title="t", status=Status.REVIEW, agent="claude-a"))
    branch(git_repo, t.id, {"README.md": "# branch\n"})
    main_commit(git_repo, {"README.md": "# main\n"}, "main change")
    summary = srv.tick()
    assert summary["conflicts"] == 1 and srv.reviewer.seen == []
    assert board.get_task(t.id).status is Status.CHANGES_REQUESTED


def test_github_fallback_when_merge_tree_cannot_tell(cfg, git_repo, tmp_path, monkeypatch):
    calls = []

    def gh(args, cwd):
        calls.append(args)
        return CmdResult(0, '{"state": "OPEN", "mergeable": "CONFLICTING"}', "")

    srv, board, logs = make(cfg, git_repo, tmp_path, gh=gh)
    t = board.create_task(Task(id="", title="t", status=Status.REVIEW, agent="claude-a", pr_url="https://x/pr/7"))
    branch(git_repo, t.id, {"other.txt": "x\n"})
    real_git = srv.ws.git

    def git_no_merge_tree(cwd, *args, **kw):
        if args and args[0] == "merge-tree":
            return CmdResult(129, "", "unknown option")
        return real_git(cwd, *args, **kw)

    monkeypatch.setattr(srv.ws, "git", git_no_merge_tree)
    assert srv.check_conflicts() == 1
    s = board.get_task(t.id)
    assert s.status is Status.CHANGES_REQUESTED and "CONFLICTING" in s.feedback
    assert calls and calls[0][:3] == ["pr", "view", "https://x/pr/7"]
