"""Git worktrees, commits, pushes, and GitHub PR helpers. The runner pushes; models never do."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

_MARKER_RE = re.compile(r"^(<{7}|>{7})( |$)", re.M)
_SEPARATOR_RE = re.compile(r"^={7}$", re.M)
_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def merge_conflict_instructions(conflicts: list[str], causes: list[str], main_ref: str) -> str:
    """What a worker must do with the conflict markers the runner left in its worktree. Plain edits, `git add` and
    `git commit` only: those work inside the Codex sandbox, `git rebase` and `git fetch` do not (Q-140)."""
    lines = [f"Before this run the harness merged current `{main_ref}` into your branch. Git could not combine "
             "these files on its own; they contain conflict markers (`<<<<<<<`, `=======`, `>>>>>>>`):", ""]
    lines += [f"- {f}" for f in conflicts]
    if causes:
        lines += ["", "Main changed them in: " + "; ".join(causes)]
    lines += ["", "Resolve them first: edit each file so it keeps main's intent and this task's change, remove every "
              "marker, then `git add " + " ".join(conflicts) + "` and `git commit --no-edit` (that concludes the "
              "merge). Do not run `git rebase`, `git fetch`, `git pull`, `git merge`, `git merge --abort` or "
              "`git reset`: the merge is already in progress and the harness handles main. If your sandbox refuses "
              "the commit, leave the resolved files in place; the harness commits them for you. Then run the "
              "project's fast verify before anything else: main may have changed defaults or contracts that this "
              "branch's tests relied on (not only the files above), and those failures are yours to fix too (Q-152)."]
    return "\n".join(lines)


@dataclass
class CmdResult:
    code: int
    out: str
    err: str

    @property
    def ok(self) -> bool:
        return self.code == 0

    def tail(self, n: int = 2000) -> str:
        text = self.out + ("\n" + self.err if self.err else "")
        return text[-n:]


def run_cmd(args: list[str], cwd: Path, timeout: int = 600, input: bytes | None = None,
            env: dict | None = None) -> CmdResult:
    try:
        p = subprocess.run(args, cwd=str(cwd), input=input, capture_output=True, timeout=timeout,
                           env={**os.environ, **(env or {})})
    except subprocess.TimeoutExpired as e:
        return CmdResult(-1, (e.stdout or b"").decode(errors="replace"),
                         (e.stderr or b"").decode(errors="replace") + f"\n[timeout after {timeout}s]")
    except FileNotFoundError as e:
        return CmdResult(-2, "", f"command not found: {args[0]} ({e})")
    return CmdResult(p.returncode, p.stdout.decode(errors="replace"), p.stderr.decode(errors="replace"))


def _default_gh(args: list[str], cwd: Path) -> CmdResult:
    return run_cmd(["gh", *args], cwd=cwd, timeout=120)


class Workspace:
    def __init__(self, repo_root: Path, worktree_root: Path, main_branch: str = "main",
                 remote: str = "origin", gh: Callable[[list[str], Path], CmdResult] | None = None):
        self.repo_root = Path(repo_root).resolve()
        self.worktree_root = Path(worktree_root).resolve()
        self.main_branch = main_branch
        self.remote = remote
        self.gh = gh or _default_gh
        # set by the CLI: a context-manager factory holding a per-host verify slot (swarm/hostlock.py); None = no lock
        self.verify_slot: Callable[[str], object] | None = None
        self.script_env: dict[str, str] = {}      # Config.project_env(): shared data paths for verify scripts

    # ----- git plumbing -----
    def git(self, cwd: Path, *args: str, check: bool = True, timeout: int = 300) -> CmdResult:
        r = run_cmd(["git", "-C", str(cwd), *args], cwd=cwd, timeout=timeout)
        if check and not r.ok:
            raise RuntimeError(f"git {' '.join(args)} failed: {r.err.strip() or r.out.strip()}")
        return r

    _fetch_lock = threading.Lock()   # one fetch at a time per process; parallel workers raced on ref locks

    def fetch(self) -> None:
        """Fetch, serialized in-process and retried when another process holds a ref lock ("cannot lock ref")."""
        sleep = getattr(self, "sleep", time.sleep)
        with Workspace._fetch_lock:
            for attempt in range(4):
                r = self.git(self.repo_root, "fetch", "-q", self.remote, check=False)
                if r.ok:
                    return
                if "lock" not in (r.err + r.out).lower() or attempt == 3:
                    raise RuntimeError(f"git fetch -q {self.remote} failed: {r.err.strip() or r.out.strip()}")
                sleep(2 * (attempt + 1))

    def worktree_path(self, task_id: str) -> Path:
        return self.worktree_root / task_id

    def _main_ref(self) -> str:
        return f"{self.remote}/{self.main_branch}"

    def provision(self, task_id: str, *, reuse_branch: bool = False) -> Path:
        self.fetch()
        path = self.worktree_path(task_id)
        branch = f"task/{task_id}"
        if path.exists():
            self.dispose(path)
        self.git(self.repo_root, "worktree", "prune")
        self.worktree_root.mkdir(parents=True, exist_ok=True)
        remote_branch = f"{self.remote}/{branch}"
        has_remote = self.git(self.repo_root, "rev-parse", "--verify", "--quiet", remote_branch, check=False).ok
        start = remote_branch if (reuse_branch and has_remote) else self._main_ref()
        self._worktree_add(branch, path, start)
        (path / ".swarm-run").mkdir(exist_ok=True)
        for pattern in (".swarm-run/", ".venv", "node_modules", "frontend/node_modules", "web/node_modules"):
            self._ensure_excluded(pattern)   # links made by setup_worktree.sh must never reach a commit (Oct 4 2026)
        return path

    def _worktree_add(self, branch: str, path: Path, start: str) -> None:
        """`git worktree add`, retried when a parallel git process holds .git/config.lock or a ref lock: a crash
        there left T-010's retry to start from main without its earlier commits (Q-082, Oct 5 2026)."""
        sleep = getattr(self, "sleep", time.sleep)
        for attempt in range(4):
            r = self.git(self.repo_root, "worktree", "add", "-q", "-B", branch, str(path), start, check=False)
            if r.ok:
                return
            if "lock" not in (r.err + r.out).lower() or attempt == 3:
                raise RuntimeError(f"git worktree add {path.name} failed: {r.err.strip() or r.out.strip()}")
            if path.exists():
                shutil.rmtree(path, ignore_errors=True)
            self.git(self.repo_root, "worktree", "prune", check=False)
            sleep(2 * (attempt + 1))

    def unmerged_commits(self, task_id: str) -> int:
        """Commits on origin/task/<id> that main does not have (uses the refs of the last fetch)."""
        ref = f"{self.remote}/task/{task_id}"
        if not self.git(self.repo_root, "rev-parse", "--verify", "--quiet", ref, check=False).ok:
            return 0
        r = self.git(self.repo_root, "rev-list", "--count", f"{self._main_ref()}..{ref}", check=False)
        try:
            return int(r.out.strip() or 0) if r.ok else 0
        except ValueError:
            return 0

    def branch_mentions(self, task_id: str) -> bool:
        """True when a commit on origin/task/<id> that main lacks names the task (the runner's own commits do)."""
        ref = f"{self.remote}/task/{task_id}"
        r = self.git(self.repo_root, "log", "--format=%s%n%b", f"{self._main_ref()}..{ref}", check=False)
        return r.ok and re.search(rf"(?<![\w-]){re.escape(task_id)}(?![\w-])", r.out) is not None

    def _ensure_excluded(self, pattern: str) -> None:
        """Ignore runtime files even when the project's .gitignore does not (info/exclude is shared by worktrees)."""
        common = self.git(self.repo_root, "rev-parse", "--git-common-dir", check=False).out.strip()
        if not common:
            return
        exclude = (self.repo_root / common / "info" / "exclude").resolve()
        try:
            exclude.parent.mkdir(parents=True, exist_ok=True)
            existing = exclude.read_text() if exclude.exists() else ""
            if pattern not in existing.splitlines():
                exclude.write_text(existing + ("" if existing.endswith("\n") or not existing else "\n") + pattern + "\n")
        except OSError:
            pass

    def provision_detached(self, name: str, ref: str | None = None) -> Path:
        """A throwaway worktree at `ref` (default: current origin/main) with no branch, e.g. to run verify on main."""
        self.fetch()
        path = self.worktree_root / name
        if path.exists():
            self.dispose(path)
        self.git(self.repo_root, "worktree", "prune")
        self.worktree_root.mkdir(parents=True, exist_ok=True)
        self.git(self.repo_root, "worktree", "add", "-q", "--detach", str(path), ref or self._main_ref())
        (path / ".swarm-run").mkdir(exist_ok=True)
        return path

    def dispose(self, path: Path) -> None:
        r = self.git(self.repo_root, "worktree", "remove", "--force", str(path), check=False)
        if not r.ok and path.exists():
            shutil.rmtree(path, ignore_errors=True)
            self.git(self.repo_root, "worktree", "prune", check=False)

    def commit_all(self, path: Path, message: str) -> bool:
        self.git(path, "add", "-A")
        if self.git(path, "diff", "--cached", "--quiet", check=False).ok:
            return False
        self.git(path, "-c", "user.email=swarm@local", "-c", "user.name=swarm", "commit", "-q", "-m", message)
        return True

    def push(self, path: Path, branch: str, *, force_with_lease: bool = False) -> CmdResult:
        args = ["push", "-q", "-u", self.remote, f"HEAD:refs/heads/{branch}"]
        if force_with_lease:
            args.insert(1, "--force-with-lease")
        return self.git(path, *args, check=False, timeout=180)

    def changed_files(self, path: Path) -> list[str]:
        committed = self.git(path, "diff", "--name-only", f"{self._main_ref()}...HEAD", check=False).out.split()
        status = self.git(path, "status", "--porcelain", "--untracked-files=all", check=False).out.splitlines()
        uncommitted = [line[3:].strip() for line in status if line.strip()]
        return sorted(set(committed) | set(uncommitted))

    def added_lines(self, path: Path) -> dict[str, list[tuple[int, str]]]:
        """Lines this branch adds relative to main, by file: {path: [(line number, text)]} (committed work only)."""
        r = self.git(path, "diff", "-U0", "--no-color", "--no-ext-diff", f"{self._main_ref()}...HEAD", check=False)
        out: dict[str, list[tuple[int, str]]] = {}
        current, line_no, header = None, 0, False
        for line in r.out.splitlines():
            if line.startswith("diff --git "):
                current, header = None, True
                continue
            if header and line.startswith("+++ "):
                name = line[4:].strip()
                current = None if name == "/dev/null" else (name[2:] if name.startswith("b/") else name)
                continue
            m = _HUNK_RE.match(line)
            if m:
                line_no, header = int(m.group(1)), False
                continue
            if current and not header and line.startswith("+"):
                out.setdefault(current, []).append((line_no, line[1:]))
                line_no += 1
        return out

    def diff_stat(self, path: Path) -> str:
        return self.git(path, "diff", "--stat", self._main_ref(), check=False).out

    def rebase_onto_main(self, path: Path) -> tuple[bool, list[str], list[str]]:
        """(ok, conflicting files, main commits that touched them): the worker needs to know *what* changed on
        main, not just that it conflicted (T-002 piled up fallbacks for two rounds without that, Oct 4 2026)."""
        self.fetch()
        r = self.git(path, "-c", "user.email=swarm@local", "-c", "user.name=swarm",
                     "rebase", self._main_ref(), check=False)
        if r.ok:
            return True, [], []
        conflicts = sorted(self.git(path, "diff", "--name-only", "--diff-filter=U", check=False).out.split())
        self.git(path, "rebase", "--abort", check=False)
        causes = self.git(path, "log", "--format=%h %s", "-5", self._main_ref(), "--not", "HEAD", "--",
                          *conflicts, check=False).out.splitlines() if conflicts else []
        return False, conflicts, [c.strip() for c in causes if c.strip()]

    def merge_main(self, path: Path, *, keep_conflicts: bool) -> tuple[bool, list[str], list[str]]:
        """Merge current main into the branch: (ok, conflicting files, main commits that touched them).

        The harness syncs branches with main by merging, never by rebasing: a sandboxed worker CLI (Codex) cannot
        write rebase or fetch metadata, and rebasing a branch that already holds a conflict-resolution merge would
        replay the same conflicts (Q-140, Q-144, Q-146, Oct 6 2026). PRs are squash-merged, so merge commits on a
        task branch never reach main. With keep_conflicts the markers and MERGE_HEAD stay in the worktree for the
        worker to resolve with plain edits, `git add` and `git commit`; otherwise the merge is aborted."""
        self.fetch()
        r = self.git(path, "-c", "user.email=swarm@local", "-c", "user.name=swarm",
                     "merge", "--no-edit", "-q", self._main_ref(), check=False)
        if r.ok:
            return True, [], []
        conflicts = self.unmerged_files(path)
        if not conflicts:   # not a content conflict (dirty tree, unrelated histories): never leave it half done
            self.git(path, "merge", "--abort", check=False)
            raise RuntimeError(f"git merge {self._main_ref()} failed: {r.err.strip() or r.out.strip()}")
        causes = self.git(path, "log", "--format=%h %s", "-5", self._main_ref(), "--not", "HEAD", "--",
                          *conflicts, check=False).out.splitlines()
        if not keep_conflicts:
            self.git(path, "merge", "--abort", check=False)
        return False, conflicts, [c.strip() for c in causes if c.strip()]

    def unmerged_files(self, path: Path) -> list[str]:
        return sorted(self.git(path, "diff", "--name-only", "--diff-filter=U", check=False).out.split())

    def merge_in_progress(self, path: Path) -> bool:
        return self.git(path, "rev-parse", "-q", "--verify", "MERGE_HEAD", check=False).ok

    def conflict_marker_files(self, path: Path) -> list[str]:
        """Changed files that still hold conflict markers (a `<<<<<<<`/`>>>>>>>` line and a `=======` line). Catches
        a worker that `git add`ed a file without resolving it, so markers never reach review or main."""
        hits = []
        for rel in self.changed_files(path):
            p = Path(path) / rel
            try:
                if not p.is_file() or p.is_symlink() or p.stat().st_size > 2_000_000:
                    continue
                text = p.read_text(errors="ignore")
            except OSError:
                continue
            if _MARKER_RE.search(text) and _SEPARATOR_RE.search(text):
                hits.append(rel)
        return hits

    def rebase_in_progress(self, path: Path) -> bool:
        for name in ("rebase-merge", "rebase-apply"):
            rel = self.git(path, "rev-parse", "--git-path", name, check=False).out.strip()
            if rel and (Path(rel) if Path(rel).is_absolute() else Path(path) / rel).exists():
                return True
        return False

    def run_script(self, path: Path, script_rel: str | None, timeout: int, *, slot: bool = True) -> CmdResult | None:
        """Run a project script in a worktree. With slot=True (verify scripts) it first takes a per-host verify
        slot when the CLI configured one, so N verifies at most run at once on this machine; setup scripts pass
        slot=False. The wait for a slot does not count against `timeout`."""
        if not script_rel:
            return None
        script = path / script_rel
        if not script.exists():
            return None
        # the target repo's scripts must not resolve `python3` to swarm-control's own venv
        own_bin = str(Path(sys.executable).parent)
        path_env = os.pathsep.join(p for p in os.environ.get("PATH", "").split(os.pathsep) if p and p != own_bin)
        env = {**self.script_env, "PATH": path_env}
        if not slot or self.verify_slot is None:
            return run_cmd(["bash", str(script)], cwd=path, timeout=timeout, env=env)
        from .hostlock import HELD_ENV
        with self.verify_slot(f"{path.name}: {script_rel}") as held:
            return run_cmd(["bash", str(script)], cwd=path, timeout=timeout,
                           env={**env, HELD_ENV: "1"} if held else env)

    # ----- GitHub -----
    def pr_create_or_update(self, branch: str, title: str, body: str) -> str:
        """The PR for this branch is the OPEN one; branch names repeat across projects (task/T-001), so an old
        merged PR under the same name is never reused."""
        listed = self.gh(["pr", "list", "--head", branch, "--state", "open", "--json", "url,number", "--limit", "5"],
                         self.repo_root)
        url = ""
        if listed.ok:
            try:
                rows = json.loads(listed.out or "[]")
                url = str(rows[0]["url"]) if rows else ""
            except (ValueError, KeyError, IndexError, TypeError):
                url = ""
        if url:
            self.gh(["pr", "edit", url, "--body", body], self.repo_root)
            return url
        created = self.gh(["pr", "create", "--head", branch, "--base", self.main_branch,
                           "--title", title, "--body", body], self.repo_root)
        if not created.ok:
            raise RuntimeError(f"gh pr create failed: {created.err.strip() or created.out.strip()}")
        return created.out.strip().splitlines()[-1] if created.out.strip() else ""

    def pr_info(self, ref: str) -> dict:
        """state / headRefOid / url / mergeable / mergeStateStatus of a PR (by url, number or branch); {} if none."""
        r = self.gh(["pr", "view", ref, "--json", "state,headRefOid,url,mergeable,mergeStateStatus"], self.repo_root)
        if not r.ok:
            return {}
        try:
            data = json.loads(r.out)
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    def pr_state(self, ref: str) -> tuple[str, str]:
        """GitHub's (mergeable, mergeStateStatus) for a PR; UNKNOWN while GitHub recomputes, N/A without a PR."""
        data = self.pr_info(ref)
        if not data:
            return "N/A", "N/A"          # no PR or gh unavailable: nothing to wait for
        return str(data.get("mergeable") or "UNKNOWN"), str(data.get("mergeStateStatus") or "UNKNOWN")

    def pr_merge(self, ref: str) -> CmdResult:
        # no --delete-branch: gh would try to delete the local branch too, which fails while a worktree holds it
        return self.gh(["pr", "merge", ref, "--squash"], self.repo_root)

    def remote_tip(self, branch: str) -> str:
        r = self.git(self.repo_root, "ls-remote", "--heads", self.remote, branch, check=False, timeout=60)
        return r.out.split()[0] if r.ok and r.out.strip() else ""

    def delete_remote_branch(self, branch: str) -> CmdResult:
        return self.git(self.repo_root, "push", "-q", self.remote, "--delete", branch, check=False, timeout=120)

    # ----- a long-lived worktree of main, used by serve and the planner -----
    def main_worktree(self) -> Path:
        path = self.worktree_root / "_main"
        if path.exists() and (path / ".git").exists():
            self.git(path, "fetch", "-q", self.remote, check=False)
            self.git(path, "reset", "-q", "--hard", self._main_ref(), check=False)
            return path
        self.fetch()
        self.git(self.repo_root, "worktree", "prune")
        self.worktree_root.mkdir(parents=True, exist_ok=True)
        self.git(self.repo_root, "worktree", "add", "-q", "-B", "swarm-main", str(path), self._main_ref())
        return path
