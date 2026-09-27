"""Git worktrees, commits, pushes, and GitHub PR helpers. The runner pushes; models never do."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


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

    # ----- git plumbing -----
    def git(self, cwd: Path, *args: str, check: bool = True, timeout: int = 300) -> CmdResult:
        r = run_cmd(["git", "-C", str(cwd), *args], cwd=cwd, timeout=timeout)
        if check and not r.ok:
            raise RuntimeError(f"git {' '.join(args)} failed: {r.err.strip() or r.out.strip()}")
        return r

    def fetch(self) -> None:
        self.git(self.repo_root, "fetch", "-q", self.remote)

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
        self.git(self.repo_root, "worktree", "add", "-q", "-B", branch, str(path), start)
        (path / ".swarm-run").mkdir(exist_ok=True)
        self._ensure_excluded(".swarm-run/")
        return path

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

    def diff_stat(self, path: Path) -> str:
        return self.git(path, "diff", "--stat", self._main_ref(), check=False).out

    def rebase_onto_main(self, path: Path) -> tuple[bool, list[str]]:
        self.fetch()
        r = self.git(path, "-c", "user.email=swarm@local", "-c", "user.name=swarm",
                     "rebase", self._main_ref(), check=False)
        if r.ok:
            return True, []
        conflicts = self.git(path, "diff", "--name-only", "--diff-filter=U", check=False).out.split()
        self.git(path, "rebase", "--abort", check=False)
        return False, sorted(conflicts)

    def run_script(self, path: Path, script_rel: str | None, timeout: int) -> CmdResult | None:
        if not script_rel:
            return None
        script = path / script_rel
        if not script.exists():
            return None
        return run_cmd(["bash", str(script)], cwd=path, timeout=timeout)

    # ----- GitHub -----
    def pr_create_or_update(self, branch: str, title: str, body: str) -> str:
        view = self.gh(["pr", "view", branch, "--json", "url"], self.repo_root)
        if view.ok:
            try:
                url = json.loads(view.out)["url"]
            except (ValueError, KeyError):
                url = ""
            self.gh(["pr", "edit", branch, "--body", body], self.repo_root)
            return url
        created = self.gh(["pr", "create", "--head", branch, "--base", self.main_branch,
                           "--title", title, "--body", body], self.repo_root)
        if not created.ok:
            raise RuntimeError(f"gh pr create failed: {created.err.strip() or created.out.strip()}")
        return created.out.strip().splitlines()[-1] if created.out.strip() else ""

    def pr_state(self, branch: str) -> tuple[str, str]:
        """GitHub's (mergeable, mergeStateStatus) for the PR on this branch; UNKNOWN while GitHub recomputes."""
        r = self.gh(["pr", "view", branch, "--json", "mergeable,mergeStateStatus"], self.repo_root)
        if not r.ok:
            return "N/A", "N/A"          # no PR or gh unavailable: nothing to wait for
        try:
            data = json.loads(r.out)
        except ValueError:
            return "N/A", "N/A"
        return str(data.get("mergeable") or "UNKNOWN"), str(data.get("mergeStateStatus") or "UNKNOWN")

    def pr_merge(self, branch: str) -> CmdResult:
        # no --delete-branch: gh would try to delete the local branch too, which fails while a worktree holds it
        return self.gh(["pr", "merge", branch, "--squash"], self.repo_root)

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
