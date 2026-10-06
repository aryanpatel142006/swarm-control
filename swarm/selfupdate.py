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
    if run(["git", "merge-base", "--is-ancestor", "@{u}", "HEAD"], cwd=repo, timeout=15).ok:
        return None   # local commits not pushed yet: nothing to pull (a no-op ff "succeeded" and the process re-exec'd forever)
    if not run(["git", "merge", "--ff-only", "-q", "@{u}"], cwd=repo, timeout=60).ok:
        return None
    return run(["git", "rev-parse", "--short", "HEAD"], cwd=repo, timeout=15).out.strip() or "updated"


def current_head(repo: Path | None = None, *, run=run_cmd) -> str:
    """The harness checkout's HEAD on disk ("" when unknown). Compared with the HEAD a process started on, it tells
    a long-running process that another one on this laptop already pulled newer code."""
    repo = repo or harness_repo()
    if repo is None:
        return ""
    r = run(["git", "rev-parse", "HEAD"], cwd=repo, timeout=15)
    return r.out.strip() if r.ok else ""


def upstream_ahead(repo: Path | None = None, *, run=run_cmd) -> bool:
    """Fetch only (the checkout is left alone): True when the upstream branch has commits HEAD lacks."""
    repo = repo or harness_repo()
    if repo is None or not run(["git", "fetch", "-q"], cwd=repo, timeout=60).ok:
        return False
    r = run(["git", "rev-list", "--count", "HEAD..@{u}"], cwd=repo, timeout=15)
    try:
        return r.ok and int(r.out.strip() or 0) > 0
    except ValueError:
        return False


def restart_self() -> None:
    """Replace this process with a fresh one on the same argv (editable install: new code is already on disk)."""
    os.execv(sys.executable, [sys.executable] + sys.argv)
