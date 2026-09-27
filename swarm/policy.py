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
