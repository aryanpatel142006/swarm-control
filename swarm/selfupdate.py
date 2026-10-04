"""Keep every laptop's harness current without a human: when a runner is idle it fetches swarm-control,
fast-forwards if main moved, and restarts itself. Teammates stopped pulling and restarting by hand on Oct 4 2026,
so their runners sat on stale code for hours."""
from __future__ import annotations

import os
import sys
from pathlib import Path

from .workspace import run_cmd

HARNESS_ROOT = Path(__file__).resolve().parents[1]


def harness_repo(root: Path = HARNESS_ROOT) -> Path | None:
    return root if (root / ".git").exists() else None


def check_and_update(repo: Path | None = None, *, run=run_cmd) -> str | None:
    """Fetch; if the upstream branch is ahead and we are clean, fast-forward. Returns the new HEAD (short) when
    something was pulled, None otherwise (no repo, no upstream, dirty tree, already current, or network error)."""
    repo = repo or harness_repo()
    if repo is None:
        return None
    if not run(["git", "fetch", "-q"], cwd=repo, timeout=60).ok:
        return None
    if run(["git", "status", "--porcelain"], cwd=repo, timeout=15).out.strip():
        return None   # local edits: never clobber a developer's work in progress
    local = run(["git", "rev-parse", "HEAD"], cwd=repo, timeout=15).out.strip()
    upstream = run(["git", "rev-parse", "@{u}"], cwd=repo, timeout=15)
    if not upstream.ok or upstream.out.strip() in ("", local):
        return None
    if not run(["git", "merge", "--ff-only", "-q", "@{u}"], cwd=repo, timeout=60).ok:
        return None
    return run(["git", "rev-parse", "--short", "HEAD"], cwd=repo, timeout=15).out.strip() or "updated"


def restart_self() -> None:
    """Replace this process with a fresh one on the same argv (editable install: new code is already on disk)."""
    os.execv(sys.executable, [sys.executable] + sys.argv)
