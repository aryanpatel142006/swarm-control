"""What the runner tells a worker about its previous round: verify output, stale sync instructions, placeholders.

Every function here is pure (text in, text out) so the rules are unit-tested without git or a CLI."""
from __future__ import annotations

import re
from pathlib import Path

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
_TIMEOUT = re.compile(r"\[timeout after (\d+)s\]")
_FAIL_HINT = re.compile(r"FAIL|ERROR|[Ee]rror|Traceback|exit(ed)? (code |status )?[1-9]|:\d+:\d+: [A-Z]+\d+"
                        r"|would reformat|not found|[Dd]enied|[Ff]ound \d+ (error|issue)|over (the|its) .*budget"
                        # a project step's own words: "demo replay regressed or failed" ended T-099's verify_full and
                        # was "no failure recognised" (Q-244)
                        r"|\b[Ff]ailed\b|\b[Rr]egress(ed|ion)\b")
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


def failing_step_line(output: str) -> str:
    """The last line outside the pytest run that reads like a failure ("demo replay regressed or failed"), or ""."""
    other, _, _ = split_verify_output(output or "")
    for ln in reversed(other.splitlines()):
        ln = ln.strip()
        if ln and _FAIL_HINT.search(ln) and not re.search(r"\b0 failed\b|: PASS\b", ln):
            return ln[:160]
    return ""


def verify_feedback(output: str, *, script: str = "scripts/verify_fast.sh", code: int | None = None,
                    intro: str = "") -> str:
    """Feedback for a failed verify: the failing step first, each section capped on its own."""
    head = intro or f"{script} failed" + (f" (exit {code})" if code is not None else "") + ". Fix it."
    timeout = _TIMEOUT.search(output or "")
    if timeout:
        # run_cmd's own timeout (code -1): nothing in the output failed, the script ran out of time (Q-173)
        head += (f" It was stopped after {timeout.group(1)} s without finishing: a hang or a very slow step (other "
                 "agents may load this machine). Find the slow step with `pytest --durations=15` before resubmitting.")
    other, pytest_part, passed = split_verify_output(output or "")
    other = other.strip("\n")
    if passed is None:
        return head + "\n" + clip_middle(output or "(no output)", OTHER_CAP + PYTEST_PASS_CAP)
    parts = [head]
    if passed:
        if other.strip() and _FAIL_HINT.search(other):
            parts += ["The tests passed; the failing step is in this output (lint, type check or another step):",
                      clip_middle(other, OTHER_CAP)]
        elif not timeout:
            if other.strip():
                parts += ["Other output from the script (no failure recognised in it):", clip_middle(other, OTHER_CAP)]
            # T-080's feedback was a green pytest tail and nothing else (Q-172, Q-173): say so instead of leaving
            # the worker to guess
            parts.append("pytest passed and no other step printed a failure, yet the script exited non-zero: run "
                         f"`bash {script} > .swarm-run/verify.log 2>&1; echo rc=$?` and read the whole log (a step "
                         "after pytest, a time budget, or a step that fails silently).")
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


# ----- files a previous attempt's notes name (Q-193) -----
# T-085 attempt 1 started three evals and was cut off; their JSONs were complete when attempt 2 started, but nothing
# said so and attempt 2 spent its first minutes finding out whether the table could be rebuilt from them.
_PATH_TOKEN = re.compile(r"(?<![\w/.$~{-])((?:~|\$\{?[A-Za-z_]\w*\}?|\.{0,2}/)?[\w.@+-]*(?:/[\w.@+{}$-]+)*"
                         r"\.(?:json|jsonl|csv|tsv|txt|log|md|wav|flac|mp3|mp4|npz|npy|pt|onnx|parquet|png|html|yaml|yml))\b")
NOTES_FILES_CAP = 15


def note_paths(text: str) -> list[str]:
    """Path-like tokens with a data/result extension, in order of appearance, without duplicates."""
    out: list[str] = []
    for m in _PATH_TOKEN.finditer(text or ""):
        tok = m.group(1).strip("`'\"(),;:")
        if tok and tok not in out:
            out.append(tok)
    return out


def _expand(tok: str, env: dict) -> str | None:
    def sub(m):
        return env.get(m.group(1) or m.group(2), "\0")
    expanded = re.sub(r"\$\{(\w+)\}|\$(\w+)", sub, tok)
    if "\0" in expanded:
        return None
    if expanded.startswith("~"):
        expanded = str(Path.home()) + expanded[1:]
    return expanded


def notes_files_status(text: str, roots: list[Path], env: dict, *, ended_at: float | None = None,
                       cap: int = NOTES_FILES_CAP) -> str:
    """One line per file the notes name that exists now: size, modification time (and whether it changed after the
    attempt ended), JSON validity / JSONL line count. Paths are tried as given (absolute, ~ or $VAR) and relative to
    each root (the worktree, then the main checkout). "" when none exist."""
    import json
    import time as _time
    lines = []
    for tok in note_paths(text):
        expanded = _expand(tok, env)
        if expanded is None:
            continue
        cands = [Path(expanded)] if expanded.startswith("/") else [r / expanded for r in roots]
        f = next((c for c in cands if c.is_file()), None)
        if f is None:
            continue
        st = f.stat()
        bits = [f"{st.st_size} bytes", "modified " + _time.strftime("%H:%M UTC", _time.gmtime(st.st_mtime))]
        if ended_at and st.st_mtime > ended_at + 1:
            bits.append("written after that attempt ended (a background job finished it)")
        if f.suffix == ".json" and st.st_size <= 20_000_000:
            try:
                json.loads(f.read_text(errors="replace"))
                bits.append("valid JSON")
            except ValueError:
                bits.append("NOT valid JSON (partial or still being written?)")
        elif f.suffix == ".jsonl" and st.st_size <= 50_000_000:
            with f.open(errors="replace") as fh:
                bits.append(f"{sum(1 for ln in fh if ln.strip())} lines")
        lines.append(f"- `{tok}` → {f}: " + ", ".join(bits))
        if len(lines) >= cap:
            break
    return "\n".join(lines)


# ----- stray editor/merge backups (Q-198) -----
# A BSD-sed backup `docs/BENCHMARKS.md-e` was committed in T-073 and was still on main three tasks later.
DEFAULT_STRAY_PATTERNS = [r"\.[A-Za-z0-9]+-e$", r"\.orig$", r"\.rej$", r"\.bak$", r"~$", r"\.sw[op]$",
                          r"(^|/)\.DS_Store$"]


def stray_files(paths: list[str], patterns: list[str]) -> list[str]:
    regs = [re.compile(p) for p in patterns]
    return [f for f in paths if any(r.search(f) for r in regs)]


def stray_feedback(files: list[str]) -> str:
    return ("This branch adds backup or merge leftovers that must not be committed: " + ", ".join(files)
            + ". Remove them with `git rm --cached <file>` and delete the file (on macOS `sed -i -e` writes a "
            "`<file>-e` backup: use `sed -i '' …` or Python), then verify again.")


# ----- scratch files and out-of-scope edits (Q-439) -----
# A worker committed scratch.py, patch_judge_*.js and an unrelated rewrite of eval/pipeline_eval.py, and nothing
# in the post-run checks said so. Neither list blocks the publish; both go to the report, the reviewer and the board.
# Patterns are fnmatch globs on the file's basename; a leading "/" limits one to the repo top level.
DEFAULT_SCRATCH_PATTERNS = ["scratch*", "*patch*.js", "*.orig", "*.rej", "tmp*", "test_tmp*", "debug*", "/*.py"]
HARNESS_NOTE_MARK = "Harness notes (not blocking):"


def scratch_files(added: list[str], patterns: list[str], scope: list[str] = ()) -> list[str]:
    """New files whose name looks like scratch work. A file the task's scope names is intended, so it is skipped
    (an empty scope names nothing)."""
    from fnmatch import fnmatchcase
    from .policy import in_scope
    out = []
    for f in added:
        base = f.rsplit("/", 1)[-1]
        hit = any((("/" not in f and fnmatchcase(f, p[1:])) if p.startswith("/") else fnmatchcase(base, p))
                  for p in patterns)
        if hit and not (scope and in_scope(f, list(scope))):
            out.append(f)
    return out


def out_of_scope_edits(changed: list[str], scope: list[str], *, named_in: str = "", exempt: tuple = (),
                       skip: list[str] = ()) -> list[str]:
    """Changed files outside the task's scope that no text names (description, acceptance, the worker's notes)."""
    from .policy import in_scope
    if not scope:
        return []
    text = named_in or ""
    return [f for f in changed if not in_scope(f, list(scope)) and not f.startswith(exempt) and f not in skip
            and f not in text and f.rsplit("/", 1)[-1] not in text]


def scope_lint_lines(scratch: list[str], outside: list[str], limit: int = 20) -> list[str]:
    def cap(files: list[str]) -> str:
        return ", ".join(files[:limit]) + (f" … and {len(files) - limit} more" if len(files) > limit else "")
    lines = []
    if scratch:
        lines.append(f"scratch files committed: {cap(scratch)}")
    if outside:
        lines.append(f"out-of-scope edits: {cap(outside)} (outside the task's scope and not named in its description, "
                     "acceptance or the worker's notes_for_reviewer as `outside scope: <file> because <criterion>`)")
    return lines


def scope_lint_feedback(lines: list[str]) -> str:
    return (HARNESS_NOTE_MARK + "\n" + "\n".join(f"- {l}" for l in lines)) if lines else ""
