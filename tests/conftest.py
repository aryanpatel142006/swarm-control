from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
import yaml

SAMPLE_CONFIG = {
    "project": "demo",
    "repo": "example/demo",
    "main_branch": "main",
    "worktree_root": "../demo-wt",
    "event": {"start": "2026-10-10T12:00:00-04:00", "end": "2026-10-11T12:00:00-04:00"},
    "poll_seconds": 15,
    "idle_poll_seconds": 30,
    "heartbeat_seconds": 60,
    "serve_seconds": 30,
    "review_policy": "high_and_above",
    "max_review_rounds": 2,
    "max_attempts": 3,
    "heartbeat_stale_minutes": 10,
    "max_queue_depth": 4,
    "verify": {
        "setup_worktree": "scripts/setup_worktree.sh",
        "fast": "scripts/verify_fast.sh",
        "full": "scripts/verify_full.sh",
    },
    "task_limits": {
        "S": {"turns": 30, "minutes": 20, "budget_usd": 3},
        "M": {"turns": 60, "minutes": 40, "budget_usd": 8},
        "L": {"turns": 120, "minutes": 75, "budget_usd": 20},
    },
    "hosts": {
        "host-a": {"max_parallel": {"claude": 3, "codex": 3, "generic": 2}},
        "host-b": {"max_parallel": {"codex": 3, "antigravity": 2, "generic": 2}},
    },
    "routing": {
        "importance_to_tier": {"critical": "best", "high": "high", "normal": "mid", "low": "low"},
        "best_tier_only_when": {"importance": "critical"},
        "type_model_overrides": {"claude": {"frontend": {"best": "opus"}}},
    },
    "docs_by_type": {
        "_all": ["PLAN.md#summary", "AGENTS.md"],
        "frontend": ["docs/DESIGN.md", "docs/CONTRACTS.md"],
        "backend": ["docs/CONTRACTS.md", "docs/ARCHITECTURE.md"],
    },
    "agents": {
        "claude-a": {
            "provider": "claude", "host": "host-a", "parallel": 2,
            "models": {"best": "fable", "high": "opus", "mid": "sonnet", "low": "haiku"},
            "effort": {"best": "high", "high": "high", "mid": "medium", "low": "low"},
            "strengths": {"frontend": 5, "backend": 4, "docs": 5, "tests": 4},
            "soft_cap_5h_usd": 40,
        },
        "codex-a": {
            "provider": "codex", "host": "host-a",
            "models": {"best": "gpt-6-astra", "high": "gpt-6-astra", "mid": "gpt-6-sol", "low": "gpt-6-luna"},
            "effort": {"best": "xhigh", "high": "high", "mid": "medium", "low": "low"},
            "strengths": {"frontend": 3, "backend": 5, "tests": 5, "infra": 5},
            "sandbox": "workspace-write",
        },
        "fake-b": {
            "provider": "generic", "host": "host-b",
            "models": {"best": "x", "high": "x", "mid": "x", "low": "x"},
            "strengths": {"backend": 3, "docs": 4},
            "command_template": "bash tests/fake_agent.sh {prompt_file} {model} {cwd}",
        },
    },
    "reviewer": {"agent": "codex-a", "model": "gpt-6-sol", "effort": "medium"},
    "planner": {"agent": "claude-a", "model": "opus", "effort": "high"},
}


@pytest.fixture
def sample_config_dict():
    import copy
    return copy.deepcopy(SAMPLE_CONFIG)


@pytest.fixture
def project_dir(tmp_path, sample_config_dict):
    """A fake project repo dir with .swarm/config.yaml and docs."""
    root = tmp_path / "demo"
    (root / ".swarm").mkdir(parents=True)
    (root / "docs").mkdir()
    (root / ".swarm" / "config.yaml").write_text(yaml.safe_dump(sample_config_dict))
    (root / "PLAN.md").write_text("# Demo\n\n## Summary\nA demo product.\n\n## Milestones\n- M1\n")
    (root / "AGENTS.md").write_text("Rules: stay in scope.\n")
    (root / "docs" / "DESIGN.md").write_text("# Design\nBlue buttons.\n")
    (root / "docs" / "CONTRACTS.md").write_text("# Contracts\n\n## events\n{\"a\":1}\n\n## http\nGET /x\n")
    (root / "docs" / "ARCHITECTURE.md").write_text("# Arch\n\n## ml\nml section\n")
    return root


@pytest.fixture
def cfg(project_dir):
    from swarm.config import load_config
    return load_config(project_dir / ".swarm" / "config.yaml")


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True).stdout


@pytest.fixture
def git_repo(tmp_path):
    """A local git repo with an 'origin' bare remote and one commit on main."""
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True)
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "tester")
    (repo / "README.md").write_text("# demo\n")
    (repo / "scripts").mkdir()
    (repo / "scripts" / "verify_fast.sh").write_text("#!/bin/sh\nexit 0\n")
    os.chmod(repo / "scripts" / "verify_fast.sh", 0o755)
    (repo / ".gitignore").write_text(".swarm-run/\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "push", "-q", "-u", "origin", "main")
    return repo


@pytest.fixture(autouse=True)
def _isolated_verify_locks(tmp_path, monkeypatch):
    """Runner tests export the per-host verify lock to the worker env; keep its files out of the real ~/.swarm."""
    import swarm.hostlock as hostlock
    root = tmp_path / "swarm-home"
    monkeypatch.setattr(hostlock, "lock_dir", lambda project: root / project / "locks")
    monkeypatch.delenv(hostlock.HELD_ENV, raising=False)
