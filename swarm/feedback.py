"""What the runner tells a worker about its previous round: verify output, stale sync instructions, placeholders.

Every function here is pure (text in, text out) so the rules are unit-tested without git or a CLI."""
from __future__ import annotations

import re

from .policy import glob_match

# ----- verify output (Q-168) -----
# verify_fast.sh prints the lint step first and the pytest tail last; one global tail cut kept only the pytest tail
# ("834 passed") and dropped the ruff errors that had failed the run (T-074, Oct 6 2026).
_PYTEST_PROGRESS = re.compile(r"^[.FEsxX]+\s*(\[\s*\d+%\])?\s*$")
_PYTEST_BANNER = re.compile(r"^(=+ .*(test session starts|FAILURES|ERRORS|warnings summary|short test summary"
                            r"|passed|failed|error|skipped|deselected|no tests ran).* =+|_{3,} .* _{3,})$", re.I)
_PYTEST_SUMMARY = re.compile(r"^=*\s*(\d+ (passed|failed|errors?|skipped|deselected|xfailed|xpassed|warnings?)"
                             r"(, )?)+.* in [\d.]+s.*$|^=+ no tests ran", re.I)
OTHER_CAP = 3000
PYTEST_FAIL_CAP = 3000
PYTEST_PASS_CAP = 500


def clip_middle(text: str, cap: int) -> str:
    """Keep the head and the tail of `text` (lint errors start at the head, totals sit at the tail)."""
    text = text.strip("\n")
    if len(text) <= cap:
        return text
    head = int(cap * 0.65)
    tail = cap - head
    return text[:head].rstrip() + f"\n[… {len(text) - cap} characters cut …]\n" + text[-tail:].lstrip()


def split_verify_output(text: str) -> tuple[str, str, bool | None]:
    """(output outside the pytest run, the pytest run, pytest_passed). pytest_passed is None without a pytest run."""
    lines = text.splitlines()
    start = next((i for i, ln in enumerate(lines) if _PYTEST_PROGRESS.match(ln) and ln.strip()
                  or _PYTEST_BANNER.match(ln.strip())), None)
    if start is None:
        return text, "", None
    end = max((i for i, ln in enumerate(lines) if _PYTEST_SUMMARY.match(ln.strip())), default=None)
    if end is None or end < start:
        end = len(lines) - 1
        summary = ""
    else:
        summary = lines[end]
    pytest_part = "\n".join(lines[start:end + 1])
    other = "\n".join(lines[:start] + lines[end + 1:])
    if summary:
        passed = not re.search(r"\b\d+ (failed|errors?)\b", summary, re.I) and "no tests ran" not in summary.lower()
    else:
        passed = not re.search(r"^(FAILED|ERROR) |Traceback", pytest_part, re.M)
        passed = passed and not re.search(r"[FE]", "".join(ln for ln in pytest_part.splitlines()
                                                         if _PYTEST_PROGRESS.match(ln)))
    return other, pytest_part, passed


def verify_feedback(output: str, *, script: str = "scripts/verify_fast.sh", code: int | None = None,
                    intro: str = "") -> str:
    """Feedback for a failed verify: the failing step first, each section capped on its own."""
    head = intro or f"{script} failed" + (f" (exit {code})" if code is not None else "") + ". Fix it."
    other, pytest_part, passed = split_verify_output(output or "")
    other = other.strip("\n")
    if passed is None:
        return head + "\n" + clip_middle(output or "(no output)", OTHER_CAP + PYTEST_PASS_CAP)
    parts = [head]
    if passed:
        if other.strip():
            parts += ["The tests passed; the failing step is in this output (lint, type check or another step):",
                      clip_middle(other, OTHER_CAP)]
        parts += ["pytest (passed), tail:", pytest_part[-PYTEST_PASS_CAP:].lstrip()]
    else:
        parts += ["pytest failed:", clip_middle(pytest_part, PYTEST_FAIL_CAP)]
        if other.strip():
            parts += ["Other output from the script (lint, type check, other steps):", clip_middle(other, OTHER_CAP)]
    return "\n".join(parts)


# ----- stale sync instructions (Q-164, Q-166) -----
# T-070's prompt held both the merger's "the harness merges origin/main and leaves conflict markers" and an older
# runner's "First run `git fetch origin && git rebase origin/main`"; the worktree was clean, so neither was true.
_LEGACY_REBASE = re.compile(r"rebase onto (origin/)?main conflicted|git rebase (?![-`'\"])|git rebase --continue"
                            r"|git fetch origin\s*&&|git pull --rebase|rebase --continue", re.I)
_NEGATED = re.compile(r"\b(do not|don't|never|no worker|not run)\b", re.I)
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z`(])")
MERGER_CONFLICT_PREFIX = "Main moved and now conflicts with this branch in:"
RUNNER_MARKERS_PREFIX = "Conflict markers are still in:"


def _strip_legacy(text: str) -> tuple[list[str], bool]:
    """(paragraphs, changed). A paragraph without a rebase/fetch instruction is kept verbatim (reviewer finding
    lists keep their line breaks); one with such a sentence loses that sentence."""
    out, changed = [], False
    for para in re.split(r"\n\s*\n", (text or "").strip()):
        if not para.strip():
            continue
        if not _LEGACY_REBASE.search(para):
            out.append(para)
            continue
        sentences = _SENTENCE_SPLIT.split(para.strip())
        kept = [s for s in sentences if not (_LEGACY_REBASE.search(s) and not _NEGATED.search(s))]
        if len(kept) == len(sentences):
            out.append(para)
            continue
        changed = True
        if " ".join(kept).strip():
            out.append(" ".join(kept).strip())
    return out, changed


def strip_legacy_rebase(text: str) -> str:
    """Drop every sentence that tells the worker to fetch or rebase (rule 17 forbids both since 012a0e9)."""
    paras, changed = _strip_legacy(text)
    return "\n\n".join(paras) if changed else (text or "")


def reconcile_sync_feedback(feedback: str, *, synced: bool, conflicts: list[str], markers: list[str],
                            main_ref: str = "origin/main") -> str:
    """Make the feedback agree with what the harness did to the worktree right before this run.

    synced: the harness merged main into the branch before this run (with or without conflicts). Then the merger's
    earlier "main conflicts, markers will be left" paragraph is superseded: the prompt's Merge conflicts section
    lists the real conflicts, or there are none and the feedback says so. The runner's own "markers are still in"
    paragraph survives only while markers remain. Legacy rebase/fetch instructions never survive."""
    paras, stale = _strip_legacy(feedback)
    kept = []
    for p in paras:
        if synced and p.startswith(MERGER_CONFLICT_PREFIX):
            stale = True
            continue
        if p.startswith(RUNNER_MARKERS_PREFIX) and not markers:
            stale = True
            continue
        kept.append(p)
    if not stale:
        return feedback or ""
    if synced and not conflicts and not markers:
        kept.append(f"Before this run the harness merged current `{main_ref}` into this branch without conflicts; "
                    "there are no conflict markers in the worktree. Earlier instructions about conflicts or "
                    "rebasing are obsolete: do not run `git rebase`, `git fetch` or `git merge`.")
    return "\n\n".join(kept)


# ----- placeholder tokens (Q-160, Q-162) -----
DEFAULT_PLACEHOLDER_PATTERNS = [r"\bTBD\b", r"TODO\(fill\)", r"\{\{", r"<fill", r"\bXXX\b", r"(?i)\blorem\b"]
DEFAULT_PLACEHOLDER_FILES = ["**/*.md", "**/*.markdown", "**/*.rst", "**/*.txt", "**/*.adoc", "docs/**"]
_FENCE = re.compile(r"^\s*(```|~~~)")
_INLINE_CODE = re.compile(r"`[^`\n]*`")


def placeholder_hits(added: dict[str, list[tuple[int, str]]], *, patterns: list[str], files: list[str],
                     fenced: dict[str, set[int]] | None = None) -> list[tuple[str, int, str]]:
    """(file, line, text) for every added line in a checked file that holds a placeholder token. Inline code spans
    and fenced code blocks are skipped: a doc may quote `{{ var }}` or `TODO` legitimately."""
    if not patterns:
        return []
    regs = [re.compile(p) for p in patterns]
    hits = []
    for path, lines in sorted(added.items()):
        if not any(glob_match(path, g) for g in files):
            continue
        skip = (fenced or {}).get(path, set())
        for no, line in lines:
            if no in skip:
                continue
            bare = _INLINE_CODE.sub("", line)
            if any(r.search(bare) for r in regs):
                hits.append((path, no, line.strip()[:200]))
    return hits


def fenced_lines(text: str) -> set[int]:
    """1-based line numbers inside ``` / ~~~ fences (fence lines included)."""
    inside, out = False, set()
    for i, line in enumerate(text.splitlines(), 1):
        if _FENCE.match(line):
            out.add(i)
            inside = not inside
        elif inside:
            out.add(i)
    return out


def placeholder_feedback(hits: list[tuple[str, int, str]], limit: int = 30) -> str:
    lines = ["Placeholder tokens are left in files this task changed. Replace each with the real value (run the "
             "measurement, or write what is known and mark the rest \"(not measured: <why>)\"), then verify again:"]
    lines += [f"- {f}:{n}: {t}" for f, n, t in hits[:limit]]
    if len(hits) > limit:
        lines.append(f"- … and {len(hits) - limit} more")
    return "\n".join(lines)
