import subprocess

from swarm.board.memory import InMemoryBoard
from swarm.merge import Merger
from swarm.models import Status, Task
from swarm.workspace import CmdResult, Workspace


def _git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True).stdout


def prep(cfg, git_repo, tmp_path, gh_ok=True, verify_ok=True, seen=None):
    calls = []

    def gh(args, cwd):
        calls.append(args)
        if seen is not None and args[:2] == ["pr", "merge"]:
            seen["worktree_exists"] = (tmp_path / "wt" / args[2].split("/")[-1]).exists()
            seen["args"] = list(args)
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


def test_merge_conflict_goes_back(cfg, git_repo, tmp_path):
    m, board, t, dep, calls = prep(cfg, git_repo, tmp_path)
    (git_repo / "feature.txt").write_text("conflict\n")
    _git(git_repo, "add", "feature.txt")
    _git(git_repo, "commit", "-qm", "main feature")
    _git(git_repo, "push", "-q", "origin", "main")
    assert m.merge(t) is False
    stored = board.get_task(t.id)
    assert stored.status is Status.CHANGES_REQUESTED and "feature.txt" in stored.feedback
    assert "resume" in stored.flags
    # the worker is never told to rebase or fetch (the Codex sandbox cannot, Q-140): the runner merges for it
    assert "Do not run `git rebase`" in stored.feedback and "git rebase origin" not in stored.feedback


def test_merger_merges_main_into_a_branch_that_holds_a_conflict_resolution(cfg, git_repo, tmp_path):
    """A rebase would replay the conflict the worker already resolved in a merge commit; a merge does not."""
    m, board, t, dep, calls = prep(cfg, git_repo, tmp_path)
    (git_repo / "feature.txt").write_text("main side\n")
    _git(git_repo, "add", "feature.txt"); _git(git_repo, "commit", "-qm", "main feature"); _git(git_repo, "push", "-q", "origin", "main")
    wt = m.ws.provision(t.id, reuse_branch=True)
    ok, conflicts, _ = m.ws.merge_main(wt, keep_conflicts=True)
    assert not ok and conflicts == ["feature.txt"]
    (wt / "feature.txt").write_text("resolved\n")
    m.ws.commit_all(wt, "resolve")            # what the worker (or the harness for a sandboxed one) commits
    m.ws.push(wt, t.branch, force_with_lease=True)
    m.ws.dispose(wt)
    (git_repo / "other.txt").write_text("more\n")
    _git(git_repo, "add", "other.txt"); _git(git_repo, "commit", "-qm", "main moves again"); _git(git_repo, "push", "-q", "origin", "main")
    assert m.merge(t) is True and board.get_task(t.id).status is Status.DONE


def test_verify_failure_after_merging_main_goes_back(cfg, git_repo, tmp_path):
    m, board, t, dep, calls = prep(cfg, git_repo, tmp_path, verify_ok=False)
    assert m.merge(t) is False
    stored = board.get_task(t.id)
    assert stored.status is Status.CHANGES_REQUESTED and "FASTFAIL" in stored.feedback


def test_gh_merge_failure_flags(cfg, git_repo, tmp_path):
    m, board, t, dep, calls = prep(cfg, git_repo, tmp_path, gh_ok=False)
    assert m.merge(t) is False
    stored = board.get_task(t.id)
    assert stored.status is Status.MERGE_READY and any(f.startswith("merge_failed") for f in stored.flags)
    assert "not mergeable" in stored.last_error


def test_gh_merge_failure_retries_then_blocks(cfg, git_repo, tmp_path):
    m, board, t, dep, calls = prep(cfg, git_repo, tmp_path, gh_ok=False)
    for _ in range(2):
        assert m.merge(board.get_task(t.id)) is False
        assert board.get_task(t.id).status is Status.MERGE_READY
    assert m.merge(board.get_task(t.id)) is False
    assert board.get_task(t.id).status is Status.BLOCKED
    assert board.list_questions(status="Open")[0].task_id == t.id


def test_merge_disposes_worktree_before_gh_and_deletes_remote_branch(cfg, git_repo, tmp_path):
    seen = {}
    m, board, t, dep, calls = prep(cfg, git_repo, tmp_path, seen=seen)
    assert m.merge(t) is True
    assert seen["worktree_exists"] is False
    assert "--delete-branch" not in seen["args"]
    assert not _git(git_repo, "ls-remote", "--heads", "origin", t.branch).strip()


def test_merge_waits_for_github_mergeability_after_push(cfg, git_repo, tmp_path):
    """GitHub recomputes mergeability asynchronously after a force-push; merging before it settles fails."""
    state = {"views": 0, "merges": 0}

    def gh(args, cwd):
        if args[:2] == ["pr", "view"]:
            state["views"] += 1
            status = "UNKNOWN" if state["views"] < 3 else "MERGEABLE"
            return CmdResult(0, f'{{"mergeable":"{status}","mergeStateStatus":"{"UNKNOWN" if status == "UNKNOWN" else "CLEAN"}"}}', "")
        if args[:2] == ["pr", "merge"]:
            state["merges"] += 1
            if state["views"] < 3:
                return CmdResult(1, "", "GraphQL: Pull Request is not mergeable (mergePullRequest)")
            _git(git_repo, "fetch", "-q", "origin")
            _git(git_repo, "merge", "-q", "--no-edit", f"origin/{args[2]}")
            _git(git_repo, "push", "-q", "origin", "main")
            return CmdResult(0, "merged", "")
        return CmdResult(0, "", "")

    board = InMemoryBoard()
    ws = Workspace(git_repo, tmp_path / "wt", gh=gh)
    t = board.create_task(Task(id="", title="Feature", status=Status.MERGE_READY, agent="codex-a"))
    wt = ws.provision(t.id)
    (wt / "feature.txt").write_text("f\n")
    ws.commit_all(wt, "feature")
    ws.push(wt, t.branch)
    ws.dispose(wt)
    sleeps = []
    m = Merger(cfg, board, ws, log=lambda *a: None, sleep=sleeps.append)
    assert m.merge(t) is True
    assert state["merges"] == 1 and state["views"] >= 3 and len(sleeps) >= 1
    assert board.get_task(t.id).status is Status.DONE


def _merge_setup(cfg, git_repo, tmp_path, gh, pr_url="https://gh/pr/21"):
    board = InMemoryBoard()
    ws = Workspace(git_repo, tmp_path / "wt", gh=gh)
    t = board.create_task(Task(id="", title="Feature", status=Status.MERGE_READY, agent="codex-a", pr_url=pr_url))
    wt = ws.provision(t.id)
    (wt / "feature.txt").write_text("f\n")
    ws.commit_all(wt, "feature")
    ws.push(wt, t.branch)
    ws.dispose(wt)
    return board, ws, board.get_task(t.id)


def test_merge_reopens_a_pr_when_the_linked_one_is_stale(cfg, git_repo, tmp_path):
    """A runner on old code links a merged PR from an earlier project under the same branch name. The merger
    opens a fresh PR for the branch and merges that, instead of failing or deleting anyone's work."""
    state = {"created": False, "merged": False}

    def gh(args, cwd):
        if args[:2] == ["pr", "view"]:
            if args[2] == "https://gh/pr/1":
                return CmdResult(0, '{"state":"MERGED","headRefOid":"0000000","url":"https://gh/pr/1"}', "")
            st = "MERGED" if state["merged"] else "OPEN"
            sha = _git(git_repo, "ls-remote", "--heads", "origin", "task/T-001").split()[0] if not state["merged"] else "x"
            return CmdResult(0, f'{{"state":"{st}","headRefOid":"{sha}","url":"https://gh/pr/40","mergeable":"MERGEABLE","mergeStateStatus":"CLEAN"}}', "")
        if args[:2] == ["pr", "list"]:
            return CmdResult(0, "[]", "")
        if args[:2] == ["pr", "create"]:
            state["created"] = True
            return CmdResult(0, "https://gh/pr/40\n", "")
        if args[:2] == ["pr", "merge"]:
            assert args[2] == "https://gh/pr/40"
            _git(git_repo, "fetch", "-q", "origin")
            _git(git_repo, "merge", "-q", "--no-edit", "origin/task/T-001")
            _git(git_repo, "push", "-q", "origin", "main")
            state["merged"] = True
            return CmdResult(0, "merged", "")
        return CmdResult(0, "", "")

    board, ws, t = _merge_setup(cfg, git_repo, tmp_path, gh, pr_url="https://gh/pr/1")
    m = Merger(cfg, board, ws, log=lambda *a: None, sleep=lambda s: None)
    m.merge(t)
    stored = board.get_task(t.id)
    assert state["created"] and stored.status is Status.DONE and stored.pr_url == "https://gh/pr/40"


def test_merge_by_url_then_confirms_and_deletes_branch(cfg, git_repo, tmp_path):
    state = {"merged": False}

    def gh(args, cwd):
        if args[:2] == ["pr", "view"]:
            assert args[2] == "https://gh/pr/21"
            sha = _git(git_repo, "ls-remote", "--heads", "origin", "task/T-001").split()[0] if not state["merged"] else "x"
            st = "MERGED" if state["merged"] else "OPEN"
            return CmdResult(0, f'{{"state":"{st}","headRefOid":"{sha}","url":"https://gh/pr/21","mergeable":"MERGEABLE","mergeStateStatus":"CLEAN"}}', "")
        if args[:2] == ["pr", "merge"]:
            assert args[2] == "https://gh/pr/21"
            _git(git_repo, "fetch", "-q", "origin")
            branch = [b for b in _git(git_repo, "branch", "-r").split() if "task/" in b][0].replace("origin/", "")
            _git(git_repo, "merge", "-q", "--no-edit", f"origin/{branch}")
            _git(git_repo, "push", "-q", "origin", "main")
            state["merged"] = True
            return CmdResult(0, "merged", "")
        return CmdResult(0, "", "")

    board, ws, t = _merge_setup(cfg, git_repo, tmp_path, gh)
    m = Merger(cfg, board, ws, log=lambda *a: None, sleep=lambda s: None)
    m.merge(t)
    assert board.get_task(t.id).status is Status.DONE
    assert not _git(git_repo, "ls-remote", "--heads", "origin", t.branch).strip()   # branch deleted after confirmation


def test_squash_message_lists_branch_commits_without_ai_attribution(cfg, git_repo, tmp_path):
    seen = {}
    m, board, t, dep, calls = prep(cfg, git_repo, tmp_path, seen=seen)
    assert m.merge(t) is True
    args = seen["args"]
    assert "--subject" in args and "--body" in args
    assert args[args.index("--subject") + 1].startswith(t.title_with_id())
    body = args[args.index("--body") + 1]
    assert "feature" in body and "Claude" not in body
