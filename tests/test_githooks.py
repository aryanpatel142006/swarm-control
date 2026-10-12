"""Commit-msg hook that strips AI attribution trailers, and the opt-in pre-push gate for main (Oct 11-12 2026:
`Claude-Session:` lines reached selective-hearing main from sub-agents in spite of instructions; direct pushes to
main broke ruff and tests for every task)."""
import subprocess

from swarm.githooks import (HOOK_MARK, hooks_dir, hook_status, install_hooks, strip_ai_attribution,
                            trailer_commits)
from swarm.workspace import Workspace


def _git(cwd, *args, env=None, check=True):
    import os
    return subprocess.run(["git", "-C", str(cwd), *args], check=check, capture_output=True, text=True,
                          env={**os.environ, **(env or {})})


MSG = """T-1: fix the gain

Body line.

Co-Authored-By: Claude Opus <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01AwRa9DJt6f9p7JxT5u1U1u
Co-authored-by: Joseph Roche <joe@example.com>
"""


def test_strip_covers_session_lines_and_other_ai_coauthors_but_keeps_humans():
    out = strip_ai_attribution(MSG + "\n🤖 Generated with [Claude Code](https://claude.com/claude-code)\n"
                               "Co-Authored-By: Codex <codex@openai.com>\nAssisted-by: GitHub Copilot\n")
    assert "Claude-Session" not in out and "anthropic" not in out and "Generated with" not in out
    assert "Codex" not in out and "Copilot" not in out
    assert "Co-authored-by: Joseph Roche <joe@example.com>" in out and out.startswith("T-1: fix the gain")
    # a subject that merely talks about Claude stays
    assert strip_ai_attribution("docs: explain the Claude-Session trailer rule") == \
        "docs: explain the Claude-Session trailer rule"


def test_commit_msg_hook_strips_trailers_in_task_worktrees(git_repo, tmp_path):
    install_hooks(git_repo)
    ws = Workspace(git_repo, tmp_path / "wt")
    wt = ws.provision("T-001")
    (wt / "a.txt").write_text("x\n")
    _git(wt, "add", "-A")
    _git(wt, "-c", "user.email=a@b", "-c", "user.name=a", "commit", "-q", "-m", MSG)
    body = _git(wt, "log", "-1", "--format=%B").stdout
    assert "Claude-Session" not in body and "noreply@anthropic.com" not in body
    assert "Joseph Roche" in body
    ws.dispose(wt)


def test_main_checkout_is_left_alone_unless_asked(git_repo):
    install_hooks(git_repo)
    (git_repo / "b.txt").write_text("x\n")
    _git(git_repo, "add", "-A")
    _git(git_repo, "commit", "-q", "-m", MSG)
    assert "Claude-Session" in _git(git_repo, "log", "-1", "--format=%B").stdout
    install_hooks(git_repo, main=True)
    (git_repo / "c.txt").write_text("x\n")
    _git(git_repo, "add", "-A")
    _git(git_repo, "commit", "-q", "-m", MSG)
    assert "Claude-Session" not in _git(git_repo, "log", "-1", "--format=%B").stdout


def test_install_is_idempotent_and_chains_an_existing_hook(git_repo):
    d = hooks_dir(git_repo)
    d.mkdir(parents=True, exist_ok=True)
    own = d / "commit-msg"
    own.write_text("#!/bin/sh\necho 'Reviewed-by: team' >> \"$1\"\n")
    own.chmod(0o755)
    install_hooks(git_repo, main=True)
    install_hooks(git_repo, main=True)
    assert HOOK_MARK in own.read_text() and (d / "commit-msg.pre-swarm").exists()
    (git_repo / "d.txt").write_text("x\n")
    _git(git_repo, "add", "-A")
    _git(git_repo, "commit", "-q", "-m", MSG)
    body = _git(git_repo, "log", "-1", "--format=%B").stdout
    assert "Reviewed-by: team" in body and "Claude-Session" not in body
    st = hook_status(git_repo)
    assert st["commit-msg"] and st["main"] and not st["pre-push"]


def test_trailer_commits_lists_offending_commits_on_a_range(git_repo):
    _git(git_repo, "commit", "-q", "--allow-empty", "-m", MSG)
    _git(git_repo, "commit", "-q", "--allow-empty", "-m", "clean one")
    hits = trailer_commits(git_repo, "HEAD~2..HEAD")
    assert len(hits) == 1 and hits[0][1].startswith("T-1: fix the gain")


def _bare_push_setup(git_repo, verify_rc: int):
    (git_repo / "scripts" / "verify_fast.sh").write_text(f"#!/bin/sh\necho checking\nexit {verify_rc}\n")
    _git(git_repo, "commit", "-qam", "verify script")
    install_hooks(git_repo, pre_push_verify="scripts/verify_fast.sh", main_branch="main")


def test_pre_push_gate_blocks_a_red_main(git_repo):
    _bare_push_setup(git_repo, verify_rc=1)
    r = _git(git_repo, "push", "-q", "origin", "main", check=False)
    assert r.returncode != 0 and "verify" in (r.stderr + r.stdout).lower()
    # the escape hatch for an emergency fix
    r = _git(git_repo, "push", "-q", "origin", "main", env={"SWARM_SKIP_PREPUSH": "1"}, check=False)
    assert r.returncode == 0


def test_pre_push_gate_passes_a_green_main_and_ignores_task_branches(git_repo):
    _bare_push_setup(git_repo, verify_rc=0)
    assert _git(git_repo, "push", "-q", "origin", "main", check=False).returncode == 0
    (git_repo / "scripts" / "verify_fast.sh").write_text("#!/bin/sh\nexit 1\n")
    _git(git_repo, "commit", "-qam", "break verify")
    _git(git_repo, "branch", "task/T-9")
    assert _git(git_repo, "push", "-q", "origin", "task/T-9", check=False).returncode == 0


def test_pre_push_gate_refuses_ai_trailers_on_main(git_repo):
    install_hooks(git_repo, main_branch="main", pre_push=True)
    _git(git_repo, "commit", "-q", "--allow-empty", "-m", MSG)
    r = _git(git_repo, "push", "-q", "origin", "main", check=False)
    assert r.returncode != 0 and "trailer" in (r.stderr + r.stdout).lower()


def test_doctor_reports_the_hook_and_the_cli_installs_it(git_repo, project_dir):
    import yaml
    from typer.testing import CliRunner
    from swarm.cli import app
    from swarm.config import load_config
    from swarm.doctor import hook_checks
    # put the project config inside the git repo so repo_root is a git checkout
    (git_repo / ".swarm").mkdir()
    (git_repo / ".swarm" / "config.yaml").write_text((project_dir / ".swarm" / "config.yaml").read_text())
    cfg = load_config(git_repo / ".swarm" / "config.yaml")
    rows = {c.name: c for c in hook_checks(cfg)}
    assert not rows["hook:commit-msg"].ok and "swarm hooks --install" in rows["hook:commit-msg"].detail
    r = CliRunner().invoke(app, ["--config", str(git_repo / ".swarm" / "config.yaml"), "hooks", "--install",
                                 "--main"])
    assert r.exit_code == 0, r.output
    assert "commit-msg (strip AI trailers): installed" in r.output and "main checkout: yes" in r.output
    rows = {c.name: c for c in hook_checks(cfg)}
    assert rows["hook:commit-msg"].ok
    _git(git_repo, "commit", "-q", "--allow-empty", "-m", "x\n\nCo-Authored-By: Claude <noreply@anthropic.com>",
         env={"SWARM_KEEP_TRAILERS": "1"})
    rows = {c.name: c for c in hook_checks(cfg)}
    assert "1 of the last 50 commits" in rows["ai trailers on main"].detail
    del yaml


def test_workspace_installs_hooks_when_the_cli_asks(git_repo, tmp_path):
    ws = Workspace(git_repo, tmp_path / "wt")
    ws.hook_options = {"main": None}
    wt = ws.provision("T-002")
    assert hook_status(git_repo)["commit-msg"]
    ws.dispose(wt)
