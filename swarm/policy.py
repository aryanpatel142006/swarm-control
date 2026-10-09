"""Review policy and scope matching."""
from __future__ import annotations

import re
from pathlib import Path

from .config import Config
from .models import Status, Task

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
        missing, local = [], []
        for p in dict.fromkeys(literal + mentioned_paths(description) + mentioned_paths(acceptance)):
            found = exists(p)
            if isinstance(found, str):     # not in git, but in the main checkout: gitignored shared data (Q-227)
                local.append((p, found))
            elif not found:
                missing.append(p)
        if local:
            notes.append("Not tracked in git (gitignored shared data in the main checkout, absent from worktrees): "
                         + ", ".join(f"`{p}` at `{a}`" for p, a in local)
                         + ". Read them at those absolute paths (or through the project's env dirs).")
        if missing:
            same_name = getattr(exists, "same_name", None)
            named, moved = [], []
            for p in missing:
                alts = [a for a in (same_name(p) if same_name else []) if a != p]
                named.append(f"`{p}`" + (f" (main has {', '.join(f'`{a}`' for a in alts[:3])})" if alts else ""))
                moved += alts[:1]
            notes.append("Not on main when this task was written: " + ", ".join(named)
                         + ". Create them if this task produces them; if another task does, check it merged first."
                         + (" A file of the same name elsewhere on main is usually the one the text means: check it "
                            "before creating a new one (Q-304)." if moved else ""))
    return scope, notes


_BARE_FILE = re.compile(r"(?<![\w./~:$-])([\w-][\w.-]*\.(?:sh|py|js|mjs|ts|tsx|jsx|md|yaml|yml|toml|css|html|ini|cfg))"
                        r"(?![\w/*])")
# Paths under these directories are inputs and outputs (results, fixtures, recordings), not files a task edits.
_DATA_DIRS = {"results", "fixtures", "data", "sessions", "runs", "models", "weights", "decisions", "debt"}


def named_files(task: Task) -> tuple[set[str], set[str]]:
    """(repo paths, bare file names) a task's literal scope entries and text name. Bare names count because
    orchestrator-written text says `gpu_up.sh` as often as `scripts/gpu_up.sh` (T-110)."""
    text = f"{task.title}\n{task.description}\n{task.acceptance}"
    paths = {g for g in task.scope if "*" not in g and "?" not in g} | set(mentioned_paths(text))
    paths = {p for p in paths if not (set(p.split("/")[:-1]) & _DATA_DIRS)}
    bare = {m.group(1) for m in _BARE_FILE.finditer(text)} | {p.rsplit("/", 1)[-1] for p in paths}
    return paths, bare


def open_task_collisions(task: Task, others: list[Task]) -> list[tuple[Task, list[str]]]:
    """Open tasks (not Done/Cut) that name a file this task names, with no dependency either way. Two open tasks on
    one file collide at merge time: T-110 and T-111 both edited scripts/gpu_up.sh's srun lines in M9 (Q-288), and
    the retro's shared-file finding lists the same docs every round."""
    paths, bare = named_files(task)
    out = []
    for o in others:
        if o.id == task.id or o.status in (Status.DONE, Status.CUT):
            continue
        if o.id in task.depends_on or (task.id and task.id in o.depends_on):
            continue
        opaths, obare = named_files(o)
        globs = [g for g in o.scope if "*" in g or "?" in g]
        hits = {p for p in paths if p in opaths or p.rsplit("/", 1)[-1] in obare or (globs and in_scope(p, globs))}
        hits |= {p for p in opaths if p.rsplit("/", 1)[-1] in bare}
        named = {h.rsplit("/", 1)[-1] for h in hits}
        hits |= {b for b in bare & obare if b not in named}
        if hits:
            out.append((o, sorted(hits)))
    return out


# ----- named options: env vars and long flags the task text names (Q-478) -----
_ENV_ASSIGN = re.compile(r"(?<![\w$])([A-Z][A-Z0-9_]{3,})=")
_ENV_REF = re.compile(r"\$\{?([A-Z][A-Z0-9_]{3,})\}?")
_LONG_FLAG = re.compile(r"(?<![\w-])(--[a-z][a-z0-9]*(?:-[a-z0-9]+)*)(?![\w-])")
# Variables every shell has, and flags of the harness and of git: never "named options" of the project.
_SYSTEM_ENV = {"HOME", "PATH", "USER", "PWD", "SHELL", "TMPDIR", "TERM", "LANG", "EDITOR", "OLDPWD", "LOGNAME"}
_COMMON_FLAGS = {"--help", "--version", "--force", "--no-verify", "--ff-only", "--rebase", "--exclusive", "--depends",
                 "--agent", "--type", "--milestone", "--size", "--importance", "--priority", "--description",
                 "--acceptance", "--minutes", "--ignore-hold", "--exclusive-max-minutes", "--scope", "--host"}
OPTION_LINT_MAX_TOKENS = 12
OPTION_LINT_MAX_BRANCHES = 20


def named_options(text: str) -> list[str]:
    """Env var names (`NAME=` or `$NAME`) and long flags (`--word`) in task text, in order, without duplicates."""
    found: list[tuple[int, str]] = []
    for rx in (_ENV_ASSIGN, _ENV_REF):
        found += [(m.start(), m.group(1)) for m in rx.finditer(text or "") if m.group(1) not in _SYSTEM_ENV]
    found += [(m.start(), m.group(1)) for m in _LONG_FLAG.finditer(text or "") if m.group(1) not in _COMMON_FLAGS]
    return list(dict.fromkeys(tok for _, tok in sorted(found)))[:OPTION_LINT_MAX_TOKENS]


def _option_regex(token: str) -> tuple[str, str]:
    """(ERE for `git grep -E -o`, Python regex that finds the token in what it printed): whole names only, so
    `--foo` is not found inside `--foo-bar` and `FOO_BAR` is not found inside `MY_FOO_BAR`."""
    t = re.escape(token)
    if token.startswith("--"):
        cls = "A-Za-z0-9_-"
        return f"(^|[^{cls}]){t}($|[^{cls}])", rf"(?<![\w-]){t}(?![\w-])"
    return f"(^|[^A-Za-z0-9_]){t}($|[^A-Za-z0-9_])", rf"(?<!\w){t}(?!\w)"


def _present_tokens(root: Path, tokens: list[str], ref: str = "") -> set[str] | None:
    """Which of `tokens` appear in the tree at `root` (the working tree, or `ref`), docs/decisions excluded.
    None when git cannot answer (no repo, no such ref)."""
    import subprocess
    cmd = ["git", "-C", str(root), "grep", "-h", "-o", "-I", "-E"]
    for t in tokens:
        cmd += ["-e", _option_regex(t)[0]]
    cmd += ([ref] if ref else []) + ["--", ".", ":(exclude)docs/decisions"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode not in (0, 1):
        return None
    return {t for t in tokens if re.search(_option_regex(t)[1], r.stdout)}


def option_lint_lines(root: Path, wt: Path, text: str, open_ids: set[str] | None = None,
                      remote: str = "origin") -> list[str]:
    """`<token> is not on main` for each env var or long flag the text names that no file on main mentions, plus
    `(provided by T-x, not merged)` when an open task branch has it. `wt` is a checkout of main, `root` the
    repository (for the branch refs). Advice: any git failure gives no lines."""
    import subprocess
    tokens = named_options(text)
    if not tokens or not (Path(wt) / ".git").exists() and not (Path(root) / ".git").exists():
        return []
    present = _present_tokens(Path(wt), tokens)
    if present is None:
        return []
    missing = [t for t in tokens if t not in present]
    providers: dict[str, list[str]] = {t: [] for t in missing}
    if missing:
        try:
            r = subprocess.run(["git", "-C", str(root), "for-each-ref", "--sort=-committerdate",
                                "--format=%(refname)", f"refs/remotes/{remote}/task/T-*"],
                               capture_output=True, text=True, timeout=30)
            refs = r.stdout.split() if r.returncode == 0 else []
        except (OSError, subprocess.SubprocessError):
            refs = []
        if open_ids is not None:
            refs = [f for f in refs if f.rsplit("/", 1)[-1] in open_ids]
        for ref in refs[:OPTION_LINT_MAX_BRANCHES]:
            tid = ref.rsplit("/", 1)[-1]
            have = _present_tokens(Path(root), missing, ref) or set()
            for t in have:
                providers[t].append(tid)
    out = []
    for t in missing:
        out.append(f"{t} is not on main" + (f" (provided by {', '.join(providers[t])}, not merged)"
                                           if providers[t] else ""))
    return out


def apply_task_lint(task: Task, exists=None, open_tasks: list[Task] | None = None) -> list[str]:
    """Complete a new task's scope and append the notes to its description (Planner.apply and `swarm add`). With
    `open_tasks`, a file another open task also names is reported, for the worker and for whoever adds the task."""
    task.scope, notes = complete_scope(task.scope, task.description, task.acceptance, exists)
    options = getattr(exists, "option_lint", None)
    if options is not None:
        ids = {o.id for o in open_tasks} if open_tasks is not None else None
        notes += options(f"{task.description}\n{task.acceptance}", ids)
    hits = open_task_collisions(task, open_tasks or [])
    if hits:
        listed = "; ".join(f"{o.id} ({', '.join(f'`{f}`' for f in files[:4])})" for o, files in hits[:4])
        more = f" and {len(hits) - 4} more" if len(hits) > 4 else ""
        notes.append(f"Open tasks that name the same files, with no dependency either way: {listed}{more}. If you edit "
                     "them, whichever task merges second meets the other's edits: re-read them after the harness "
                     "merges main.")
    if notes:
        task.description = (task.description.rstrip() + "\n\nHarness notes:\n" + "\n".join(f"- {n}" for n in notes)).strip()
    return notes


def main_exists(ws):
    """`exists` callback against a fresh worktree of main, or None when main cannot be checked out. A path that is
    not on main but exists in the main checkout (a gitignored results file or fixture) returns its absolute path:
    the old "Not on main" note sent a worker hunting for a file that sat in HEARING_RESULTS_DIR (Q-227)."""
    try:
        wt = ws.main_worktree()
    except Exception:  # noqa: BLE001 - the lint is advice; never block task creation on it
        return None
    root = Path(getattr(ws, "repo_root", "") or "")

    def exists(p: str):
        if (wt / p).exists():
            return True
        if str(root) and (root / p).exists():
            return str(root / p)
        return False
    files: list[str] = []

    def same_name(p: str) -> list[str]:
        """Tracked files on main with the same file name as `p`: T-118's text said eval/demo_regress.py for
        scripts/demo_regress.py (Q-304)."""
        if not files:
            files.extend(_tracked_files(wt))
        name = p.rsplit("/", 1)[-1]
        return sorted(f for f in files if f.rsplit("/", 1)[-1] == name)
    exists.same_name = same_name
    exists.option_lint = lambda text, open_ids=None: option_lint_lines(
        root, wt, text, open_ids, getattr(ws, "remote", "origin"))
    return exists


def _tracked_files(root: Path) -> list[str]:
    import subprocess
    try:
        out = subprocess.run(["git", "-C", str(root), "ls-files"], capture_output=True, text=True, timeout=30)
        if out.returncode == 0:
            return out.stdout.splitlines()
    except (OSError, subprocess.SubprocessError):
        pass
    skip = {".git", "node_modules", ".venv", "__pycache__"}
    return [str(f.relative_to(root)) for f in root.rglob("*")
            if f.is_file() and not skip & set(f.relative_to(root).parts)]


_ROOT_PATH = re.compile(r"(?:^|(?<=[\s'\"(`=]))(/[\w.<>{}@+-]+)((?:/[\w.<>{}@+-]+)*\.[A-Za-z0-9]{1,6})?(?=$|[\s'\"),`;:]|\.(?:\s|$))",
                        re.M)


def shell_expansion_hints(text: str, root: Path | None = None) -> list[str]:
    """Paths in orchestrator-written task text that start at a filesystem-root entry that does not exist and carry
    a file extension: almost always a `$VAR/…` that the shell expanded to nothing because the text was in double
    quotes (Q-186: `/demo_regress_<stamp>.json` had lost `$HEARING_RESULTS_DIR`). Advice for `swarm add` only."""
    root = Path(root or "/")
    hints = []
    for m in _ROOT_PATH.finditer(text or ""):
        first, rest = m.group(1), m.group(2) or ""
        token = first + rest
        if not re.search(r"\.[A-Za-z0-9]{1,6}$", token):
            continue                                   # /simple, /api/x: routes, not files
        if (root / first.lstrip("/")).exists():
            continue
        hints.append(f"`{token}` starts at the filesystem root, where `{first}` does not exist: probably a `$VAR/…` "
                     "your shell expanded to nothing. Pass task text in single quotes or a quoted heredoc.")
    return list(dict.fromkeys(hints))
