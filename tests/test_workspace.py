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
    ok, conflicts, _causes = ws.rebase_onto_main(path)
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
    ok, conflicts, _causes = ws.rebase_onto_main(path)
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
            created = any(c[:2] == ["pr", "create"] for c in calls)
            return CmdResult(0, '[{"url":"https://gh/pr/9","number":9}]' if created else "[]", "")
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


def test_pr_lookup_ignores_closed_and_merged_prs(git_repo, tmp_path):
    """Branch names repeat across projects (task/T-001); an old merged PR on that branch is not 'the PR'."""
    calls = []

    def fake_gh(args, cwd):
        calls.append(args)
        if args[:2] == ["pr", "list"]:
            assert "--head" in args and "--state" in args and args[args.index("--state") + 1] == "open"
            return CmdResult(0, "[]" if len([c for c in calls if c[:2] == ["pr", "create"]]) == 0 else
                             '[{"url":"https://gh/pr/21","number":21}]', "")
        if args[:2] == ["pr", "view"]:
            return CmdResult(0, '{"url":"https://gh/pr/1"}', "")   # the stale, merged PR gh would resolve by branch
        if args[:2] == ["pr", "create"]:
            return CmdResult(0, "https://gh/pr/21\n", "")
        if args[:2] == ["pr", "edit"]:
            return CmdResult(0, "", "")
        return CmdResult(1, "", "unexpected")

    ws = Workspace(git_repo, tmp_path / "wt", gh=fake_gh)
    assert ws.pr_create_or_update("task/T-001", "T-001 · title", "body") == "https://gh/pr/21"
    assert any(a[:2] == ["pr", "create"] for a in calls)
    assert ws.pr_create_or_update("task/T-001", "T-001 · title", "body2") == "https://gh/pr/21"
    assert [a for a in calls if a[:2] == ["pr", "edit"]][0][2] == "https://gh/pr/21"   # edit by url, not branch


def test_pr_info_and_merge_by_url(git_repo, tmp_path):
    calls = []

    def fake_gh(args, cwd):
        calls.append(args)
        if args[:2] == ["pr", "view"]:
            return CmdResult(0, '{"state":"OPEN","headRefOid":"abc123","url":"https://gh/pr/21","mergeable":"MERGEABLE","mergeStateStatus":"CLEAN"}', "")
        if args[:2] == ["pr", "merge"]:
            return CmdResult(0, "merged", "")
        return CmdResult(1, "", "unexpected")

    ws = Workspace(git_repo, tmp_path / "wt", gh=fake_gh)
    info = ws.pr_info("https://gh/pr/21")
    assert info["state"] == "OPEN" and info["headRefOid"] == "abc123"
    assert ws.pr_state("https://gh/pr/21") == ("MERGEABLE", "CLEAN")
    assert ws.pr_merge("https://gh/pr/21").ok and calls[-1][:3] == ["pr", "merge", "https://gh/pr/21"]


def test_fetch_retries_on_a_ref_lock_and_serializes(git_repo, tmp_path):
    """Two workers and serve fetching one repo at once: 'cannot lock ref' crashed a runner in night cycle 1."""
    from swarm.workspace import Workspace
    ws = Workspace(git_repo, tmp_path / "wt", gh=lambda a, c: CmdResult(0, "", ""))
    calls = []
    real = ws.git

    def flaky(path, *args, **kw):
        if args[:1] == ("fetch",):
            calls.append(args)
            if len(calls) == 1:
                return CmdResult(1, "", "error: cannot lock ref 'refs/remotes/origin/task/T-025': is at abc but expected def")
        return real(path, *args, **kw)
    ws.git = flaky
    ws.sleep = lambda s: None
    ws.fetch()
    assert len(calls) == 2


def test_provision_excludes_harness_links_and_scripts_never_see_swarms_venv(git_repo, tmp_path):
    """Oct 4 2026: a worktree's .venv symlink was committed by `git add -A` (the project ignored `.venv/`, which
    does not match a symlink) and later replaced the main checkout's venv with a link to itself. Also, scripts
    run by the harness resolved `python3` to swarm-control's own venv."""
    import sys
    from swarm.workspace import Workspace
    ws = Workspace(git_repo, tmp_path / "wt", push=lambda *a, **k: None) if "push" in Workspace.__init__.__code__.co_varnames else Workspace(git_repo, tmp_path / "wt")
    wt = ws.provision("T-009")
    exclude = (git_repo / ".git" / "info" / "exclude").read_text().splitlines()
    for pat in (".venv", "node_modules", ".swarm-run/"):
        assert pat in exclude
    (wt / ".venv").symlink_to(git_repo)          # a harness-style link must never be staged
    (wt / "real.txt").write_text("x")
    assert ws.commit_all(wt, "T-009: test")
    staged = ws.git(wt, "show", "--name-only", "--format=", "HEAD").out.split()
    assert "real.txt" in staged and ".venv" not in staged
    (wt / "p.sh").write_text("echo \"$PATH\"\n")
    r = ws.run_script(wt, "p.sh", 30)
    assert r.ok and str(Path(sys.executable).parent) not in r.out


def test_rebase_feedback_names_the_main_commits_that_conflicted(git_repo, tmp_path):
    from swarm.workspace import Workspace
    ws = Workspace(git_repo, tmp_path / "wt")
    wt = ws.provision("T-010")
    (wt / "shared.txt").write_text("branch\n")
    ws.commit_all(wt, "T-010: branch side")
    (wt / "shared.txt").write_text("branch\n")
    ws.push(wt, "task/T-010")
    (git_repo / "shared.txt").write_text("main\n")
    _git(git_repo, "add", "-A")
    _git(git_repo, "commit", "-qm", "infra: rewrite shared.txt")
    _git(git_repo, "push", "-q", "origin", "main")
    ok, conflicts, causes = ws.rebase_onto_main(wt)
    assert not ok and conflicts == ["shared.txt"]
    assert any("rewrite shared.txt" in c for c in causes)
