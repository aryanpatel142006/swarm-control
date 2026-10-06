"""Review policy and scope matching."""
from __future__ import annotations

import re

from .config import Config
from .models import Task

FORCE_REVIEW_FLAGS = ("report_missing", "out_of_scope", "docs_touched")


def glob_match(path: str, glob: str) -> bool:
    pattern = re.escape(glob).replace(r"\*\*/", "(?:.*/)?").replace(r"\*\*", ".*").replace(r"\*", "[^/]*")
    return re.fullmatch(pattern, path) is not None


def in_scope(path: str, scope: list[str]) -> bool:
    if not scope:
        return True
    return any(glob_match(path, g) for g in scope)


def needs_review(task: Task, cfg: Config) -> bool:
    if any(f in task.flags for f in FORCE_REVIEW_FLAGS):
        return True
    policy = cfg.review_policy
    if policy == "all":
        return True
    if policy == "none":
        return False
    if policy == "critical_only":
        return task.importance == "critical"
    return task.importance in ("critical", "high")


# ----- task text lint (planner and `swarm add`) -----
SHARED_DOCS = ("docs/CONTRACTS.md", "docs/DESIGN.md", "docs/ARCHITECTURE.md", "README.md", "PLAN.md")
_PATH_RE = re.compile(r"(?<![\w./~:-])((?:[\w.-]+/)+[\w-][\w.-]*\.[A-Za-z][A-Za-z0-9]{0,5})(?![\w/*])")


def mentioned_paths(text: str) -> list[str]:
    """Repo-relative file paths named in task text (`dir/file.ext`); absolute, home and URL paths are skipped."""
    out = []
    for m in _PATH_RE.finditer(text or ""):
        p = m.group(1).rstrip(".")
        if p.startswith("./"):
            p = p[2:]
        if p and not p.startswith(("../", ".swarm-run/")) and p not in out:
            out.append(p)
    return out


def complete_scope(scope: list[str], description: str, acceptance: str,
                   exists=None) -> tuple[list[str], list[str]]:
    """(scope, notes). Every file the acceptance names joins the scope: workers were told to stay in Scope while
    the acceptance needed protocol.py, demo.sh or a test pinning the old default (Q-096, Q-118, Q-122, Q-123).
    Shared docs keep their single owner and only get a note. With `exists`, paths named in the scope or the text
    that are not on main are reported, so the worker knows to create them (Q-096, Q-127)."""
    scope = list(scope)
    notes: list[str] = []
    if scope:
        for p in mentioned_paths(acceptance):
            if in_scope(p, scope):
                continue
            if p in SHARED_DOCS:
                notes.append(f"Acceptance names `{p}`, a shared doc outside this task's scope: say in the report what "
                             "it needs and leave the edit to its owner.")
            else:
                scope.append(p)
    if exists is not None:
        literal = [g for g in scope if "*" not in g and "?" not in g]
        missing = [p for p in dict.fromkeys(literal + mentioned_paths(description) + mentioned_paths(acceptance))
                   if not exists(p)]
        if missing:
            notes.append("Not on main when this task was written: " + ", ".join(f"`{p}`" for p in missing)
                         + ". Create them if this task produces them; if another task does, check it merged first.")
    return scope, notes


def apply_task_lint(task: Task, exists=None) -> list[str]:
    """Complete a new task's scope and append the notes to its description (Planner.apply and `swarm add`)."""
    task.scope, notes = complete_scope(task.scope, task.description, task.acceptance, exists)
    if notes:
        task.description = (task.description.rstrip() + "\n\nHarness notes:\n" + "\n".join(f"- {n}" for n in notes)).strip()
    return notes


def main_exists(ws):
    """`exists` callback against a fresh worktree of main, or None when main cannot be checked out."""
    try:
        wt = ws.main_worktree()
    except Exception:  # noqa: BLE001 - the lint is advice; never block task creation on it
        return None
    return lambda p: (wt / p).exists()
