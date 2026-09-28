import subprocess
from pathlib import Path

from swarm.workspace import CmdResult, Workspace, run_cmd


def _git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True).stdout


def test_run_cmd_captures():
    r = run_cmd(["sh", "-c", "echo hi; echo err 1>&2; exit 3"], cwd=Path("."))
    assert r.code == 3 and r.out.strip() == "hi" and r.err.strip() == "err" and not r.ok
    assert "err" in r.tail(100)


def test_provision_commit_push_and_reuse(git_repo, tmp_path):
    ws = Workspace(git_repo, tmp_path / "wt")
    path = ws.provision("T-001")
    assert path.exists() and (path / ".swarm-run").is_dir()
    assert _git(path, "rev-parse", "--abbrev-ref", "HEAD").strip() == "task/T-001"
    (path / "a.txt").write_text("hello\n")
    assert ws.commit_all(path, "T-001: add a") is True
    assert ws.commit_all(path, "nothing") is False
    assert ws.push(path, "task/T-001").ok
    assert ws.changed_files(path) == ["a.txt"]
    assert "a.txt" in ws.diff_stat(path)
    ws.dispose(path)
    assert not path.exists()
    # reuse picks up the pushed commit
    path2 = ws.provision("T-001", reuse_branch=True)
    assert (path2 / "a.txt").read_text() == "hello\n"
    ws.dispose(path2)
    # fresh provision resets to main
    path3 = ws.provision("T-001", reuse_branch=False)
    assert not (path3 / "a.txt").exists()
    ws.dispose(path3)


def test_provision_replaces_stale_worktree(git_repo, tmp_path):
    ws = Workspace(git_repo, tmp_path / "wt")
    p1 = ws.provision("T-002")
    (p1 / "junk.txt").write_text("x")
    p2 = ws.provision("T-002")  # not disposed first
    assert p2 == p1 and not (p2 / "junk.txt").exists()
    ws.dispose(p2)


def test_rebase_conflict_detection(git_repo, tmp_path):
    ws = Workspace(git_repo, tmp_path / "wt")
    path = ws.provision("T-003")
    (path / "README.md").write_text("branch version\n")
    ws.commit_all(path, "branch change")
    ws.push(path, "task/T-003")
    # conflicting change on main
    (git_repo / "README.md").write_text("main version\n")
    _git(git_repo, "commit", "-qam", "main change")
    _git(git_repo, "push", "-q", "origin", "main")
    ok, conflicts = ws.rebase_onto_main(path)
    assert ok is False and conflicts == ["README.md"]
    assert _git(path, "status", "--porcelain").strip() == ""  # aborted cleanly
    ws.dispose(path)


def test_rebase_clean(git_repo, tmp_path):
    ws = Workspace(git_repo, tmp_path / "wt")
    path = ws.provision("T-004")
    (path / "new.txt").write_text("n\n")
    ws.commit_all(path, "new")
    (git_repo / "other.txt").write_text("o\n")
    _git(git_repo, "add", "other.txt")
    _git(git_repo, "commit", "-qm", "main other")
    _git(git_repo, "push", "-q", "origin", "main")
    ok, conflicts = ws.rebase_onto_main(path)
    assert ok and conflicts == [] and (path / "other.txt").exists()
    ws.dispose(path)


def test_run_script_missing_and_present(git_repo, tmp_path):
    ws = Workspace(git_repo, tmp_path / "wt")
    path = ws.provision("T-005")
    assert ws.run_script(path, "scripts/nope.sh", 10) is None
    r = ws.run_script(path, "scripts/verify_fast.sh", 10)
    assert r is not None and r.ok
    ws.dispose(path)


def test_pr_create_then_update_and_merge(git_repo, tmp_path):
    calls = []

    def fake_gh(args, cwd):
        calls.append(args)
        if args[:2] == ["pr", "list"]:
            if len(calls) == 1:
                return CmdResult(0, "[]", "")
            assert "open" in args and "task/T-006" in args
            return CmdResult(0, '[{"url":"https://gh/pr/9"}]', "")
        if args[:2] == ["pr", "create"]:
            return CmdResult(0, "https://gh/pr/9\n", "")
        if args[:2] == ["pr", "edit"]:
            return CmdResult(0, "", "")
        if args[:2] == ["pr", "merge"]:
            return CmdResult(0, "merged", "")
        return CmdResult(1, "", "unexpected")

    ws = Workspace(git_repo, tmp_path / "wt", gh=fake_gh)
    assert ws.pr_create_or_update("task/T-006", "T-006 · title", "body") == "https://gh/pr/9"
    assert ws.pr_create_or_update("task/T-006", "T-006 · title", "body2") == "https://gh/pr/9"
    assert any(a[:2] == ["pr", "edit"] for a in calls)
    assert ws.pr_merge("task/T-006").ok


def test_pr_lookup_excludes_merged_rehearsal_prs(git_repo, tmp_path):
    calls = []
    def gh(args, cwd):
        calls.append(args)
        if args[:2] == ["pr", "list"]:
            assert args[args.index("--state") + 1] == "open"
            return CmdResult(0, "[]", "")
        if args[:2] == ["pr", "create"]:
            return CmdResult(0, "https://gh/pr/20\n", "")
        raise AssertionError(args)
    ws = Workspace(git_repo, tmp_path / "wt", gh=gh)
    assert ws.pr_create_or_update("task/T-002", "JSON API", "new rehearsal") == "https://gh/pr/20"


def test_main_worktree(git_repo, tmp_path):
    ws = Workspace(git_repo, tmp_path / "wt")
    m = ws.main_worktree()
    assert (m / "README.md").exists()
    assert _git(m, "rev-parse", "--abbrev-ref", "HEAD").strip() == "swarm-main"
    assert ws.main_worktree() == m


def test_swarm_run_excluded_without_gitignore(git_repo, tmp_path):
    (git_repo / ".gitignore").unlink()
    _git(git_repo, "commit", "-qam", "drop gitignore")
    _git(git_repo, "push", "-q", "origin", "main")
    ws = Workspace(git_repo, tmp_path / "wt")
    path = ws.provision("T-009")
    (path / ".swarm-run" / "prompt.md").write_text("p")
    assert ws.changed_files(path) == []
    ws.dispose(path)


def test_changed_files_lists_untracked_files_individually(git_repo, tmp_path):
    ws = Workspace(git_repo, tmp_path / "wt")
    path = ws.provision("T-010")
    (path / "docs" / "decisions").mkdir(parents=True)
    (path / "docs" / "decisions" / "T-010.md").write_text("d")
    assert ws.changed_files(path) == ["docs/decisions/T-010.md"]
    ws.dispose(path)
