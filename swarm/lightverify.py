"""Light verify: docs/eval-only branches skip the project's full verify in review and merge.

Oct 10 2026 (selective-hearing, ~03:50 UTC): about 30 tasks sat in Merge Ready because every review ran
`verify_full.sh` on laptop-a, which has 2 verify slots; tasks waited 20+ minutes for a slot, and almost all of them
changed only docs and offline eval scripts/tests. A branch whose every changed file (git diff --name-only against the
merge base with main, renames counted on both sides) matches `verify.light_paths` and none matches a protected glob
runs `verify.light_command` (default: `verify.fast`) instead. Never light for a critical task, an empty diff, or
when `verify.light_paths` is absent or empty (the backward-compatible default).

Web verify (Oct 10 2026, ~22:00 UTC): verify_fast took 9-29 min on a saturated laptop-a (load 10-17) with 2 verify
slots and ~12 web-only tasks queued in Review/Merge Ready. A branch whose every changed file matches
`verify.web_paths` runs `verify.web_command` (the project's web-only check: node tests, JS syntax, the app-route
Python tests) in pre-publish verify, review and merge, takes no verify slot and never waits behind exclusive
measurements. Checked before the light rules, and for critical tasks too: the web command is what can break.

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


# Changes that cannot perturb a measurement: their verify does not queue behind exclusive measurements for long.
QUICK_DEFAULT = ("docs/**", "*.md", "web/**", "frontend/**")


def quick_slot_wait(verify_cfg, files: list[str]) -> float | None:
    """Seconds a harness verify of a branch changing `files` waits at most behind `swarm-lock --exclusive`
    measurements (hostlock quick_cap_s), or None for the usual wait. Quick only when every changed file matches
    `verify.quick_paths` (None = QUICK_DEFAULT, [] = off). Oct 10 2026: merges of web-only tasks waited 1800 s each
    behind audio measurements, one at a time, and six frontend tasks queued for hours."""
    paths = getattr(verify_cfg, "quick_paths", None)
    paths = [p for p in (QUICK_DEFAULT if paths is None else paths) if p.strip()]
    files = [f for f in files if f.strip()]
    if not paths or not files:
        return None
    if all(any(glob_match(f, p) for p in paths) for f in files):
        return float(getattr(verify_cfg, "quick_wait_seconds", 60.0))
    return None


@dataclass
class VerifyChoice:
    light: bool
    command: str | None
    reason: str                         # "N files, docs/eval only" or the first path that forces the full verify
    files: list[str] = field(default_factory=list)
    web: bool = False                   # verify.web_command: no verify slot, no exclusive wait (light is True too)

    @property
    def slot(self) -> bool:
        """Whether the run takes a per-host verify slot (and waits behind exclusive measurements)."""
        return not self.web

    def log_line(self, task_id: str) -> str:
        if self.web:
            return f"[{task_id}] web verify: {self.command} ({self.reason}; no verify slot)"
        return f"[{task_id}] light verify ({self.reason})" if self.light else f"[{task_id}] full verify: {self.reason}"

    def commit_line(self) -> str:
        if self.web:
            return f"Verify: web, {self.command} ({self.reason})"
        return (f"Verify: light, {self.command} ({self.reason})" if self.light
                else f"Verify: full, {self.command or '(none)'} ({self.reason})")


def web_choice(verify_cfg, files: list[str]) -> VerifyChoice | None:
    """The web verify for a branch changing `files`, or None: every file matches `verify.web_paths` and
    `verify.web_command` is set. Importance does not matter (see the module doc)."""
    paths = [p for p in (getattr(verify_cfg, "web_paths", None) or []) if p.strip()]
    command = getattr(verify_cfg, "web_command", None)
    files = sorted({f for f in files if f.strip()})
    if not paths or not command or not files:
        return None
    if all(any(glob_match(f, p) for p in paths) for f in files):
        return VerifyChoice(True, command, f"{len(files)} file{'s' if len(files) != 1 else ''}, web only", files,
                            web=True)
    return None


def logs_choice(verify_cfg) -> bool:
    """Whether review and merge log their verify choice (light or web verify configured)."""
    return bool(getattr(verify_cfg, "light_paths", None) or getattr(verify_cfg, "web_paths", None))


def choose_verify(verify_cfg, files: list[str], importance: str, full_command: str | None,
                  web: bool = True) -> VerifyChoice:
    """Which verify a review or merge runs for a branch that changes `files`. `full_command` is what that step runs
    without the light path (the reviewer's verify.full, the merger's verify.fast). A web-only branch gets the web
    verify first (`web_choice`)."""
    w = web_choice(verify_cfg, files) if web else None
    if w is not None:
        return w
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
