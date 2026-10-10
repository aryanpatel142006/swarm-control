"""Light verify: docs/eval-only branches skip the project's full verify in review and merge.

Oct 10 2026 (selective-hearing, ~03:50 UTC): about 30 tasks sat in Merge Ready because every review ran
`verify_full.sh` on laptop-a, which has 2 verify slots; tasks waited 20+ minutes for a slot, and almost all of them
changed only docs and offline eval scripts/tests. A branch whose every changed file (git diff --name-only against the
merge base with main, renames counted on both sides) matches `verify.light_paths` and none matches a protected glob
runs `verify.light_command` (default: `verify.fast`) instead. Never light for a critical task, an empty diff, or
when `verify.light_paths` is absent or empty (the backward-compatible default).

Globs: a pattern with a "/" matches the whole repo-relative path, `**` across directories, `*` and `?` inside one
path segment; a pattern without "/" matches the file's basename anywhere (gitignore style), so `*.md` is every
Markdown file and `requirements*` protects `eval/requirements.txt` too. Protected wins over light.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache

# Never light, whatever light_paths says; `verify.protected_paths` extends this list.
PROTECTED_DEFAULT = ("hearing/**", "config/**", "scripts/**", "web/**", ".swarm/**", "pyproject.toml",
                     "requirements*", ".github/**")


@lru_cache(maxsize=512)
def _regex(pattern: str) -> re.Pattern:
    out, i = [], 0
    while i < len(pattern):
        c = pattern[i]
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
            continue
        if pattern.startswith("**", i):
            out.append(".*")
            i += 2
            continue
        out.append("[^/]*" if c == "*" else "[^/]" if c == "?" else re.escape(c))
        i += 1
    return re.compile("".join(out) + r"\Z")


def glob_match(path: str, pattern: str) -> bool:
    path, pattern = path.strip(), pattern.strip()
    path = path[2:] if path.startswith("./") else path
    if not pattern:
        return False
    if "/" not in pattern:
        return bool(_regex(pattern).match(path.rsplit("/", 1)[-1]))
    return bool(_regex(pattern.lstrip("/")).match(path))


@dataclass
class VerifyChoice:
    light: bool
    command: str | None
    reason: str                         # "N files, docs/eval only" or the first path that forces the full verify
    files: list[str] = field(default_factory=list)

    def log_line(self, task_id: str) -> str:
        return f"[{task_id}] light verify ({self.reason})" if self.light else f"[{task_id}] full verify: {self.reason}"

    def commit_line(self) -> str:
        return (f"Verify: light, {self.command} ({self.reason})" if self.light
                else f"Verify: full, {self.command or '(none)'} ({self.reason})")


def choose_verify(verify_cfg, files: list[str], importance: str, full_command: str | None) -> VerifyChoice:
    """Which verify a review or merge runs for a branch that changes `files`. `full_command` is what that step runs
    without the light path (the reviewer's verify.full, the merger's verify.fast)."""
    light_paths = [p for p in (getattr(verify_cfg, "light_paths", None) or []) if p.strip()]
    files = sorted({f for f in files if f.strip()})

    def full(reason: str) -> VerifyChoice:
        return VerifyChoice(False, full_command, reason, files)

    if not light_paths:
        return full("verify.light_paths not set")
    if importance == "critical":
        return full("critical task")
    if not files:
        return full("no changed files against main")
    protected = list(PROTECTED_DEFAULT) + list(getattr(verify_cfg, "protected_paths", None) or [])
    for f in files:
        hit = next((p for p in protected if glob_match(f, p)), None)
        if hit:
            return full(f"{f} (protected: {hit})")
        if not any(glob_match(f, p) for p in light_paths):
            return full(f)
    command = getattr(verify_cfg, "light_command", None) or getattr(verify_cfg, "fast", None)
    if not command:
        return full("no verify.light_command or verify.fast configured")
    return VerifyChoice(True, command, f"{len(files)} file{'s' if len(files) != 1 else ''}, docs/eval only", files)
