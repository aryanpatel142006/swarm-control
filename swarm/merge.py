"""Merger: rebase onto main, fast verify, force-with-lease push, squash merge, mark Done."""
from __future__ import annotations

from .board.base import Board
from .config import Config
from .models import Status, Task, utcnow
from .workspace import Workspace


class Merger:
    def __init__(self, cfg: Config, board: Board, ws: Workspace, *, log=print):
        self.cfg, self.board, self.ws, self.log = cfg, board, ws, log

    def _back(self, task: Task, feedback: str) -> bool:
        task.status = Status.CHANGES_REQUESTED
        task.feedback = feedback[:1900]
        task.flags = list(dict.fromkeys(task.flags + ["resume"]))
        self.board.update_task(task, ["status", "feedback", "flags"])
        self.log(f"[{task.id}] merge → changes requested")
        return False

    def merge(self, task: Task) -> bool:
        if "merge_failed" in task.flags:
            return False
        wt = self.ws.provision(task.id, reuse_branch=True)
        try:
            ok, conflicts = self.ws.rebase_onto_main(wt)
            if not ok:
                return self._back(task, "Rebase onto main conflicted. Resolve conflicts in: " + ", ".join(conflicts)
                                  + f". Run `git fetch origin && git rebase origin/{self.cfg.main_branch}`, "
                                  "resolve, then `git rebase --continue`.")
            self.ws.run_script(wt, self.cfg.verify.setup_worktree, 600)
            verify = self.ws.run_script(wt, self.cfg.verify.fast, 900)
            if verify is not None and not verify.ok:
                return self._back(task, "verify_fast.sh failed after rebase onto main:\n" + verify.tail(1500))
            push = self.ws.push(wt, task.branch, force_with_lease=True)
            if not push.ok:
                task.last_error = ("push failed: " + push.err.strip())[:1900]
                self.board.update_task(task, ["last_error"])
                return False
            r = self.ws.pr_merge(task.branch)
            if not r.ok:
                task.last_error = ("gh pr merge failed: " + (r.err.strip() or r.out.strip()))[:1900]
                task.flags = list(dict.fromkeys(task.flags + ["merge_failed"]))
                self.board.update_task(task, ["last_error", "flags"])
                self.log(f"[{task.id}] gh merge failed: {task.last_error}")
                return False
            task.status, task.claim_nonce = Status.DONE, ""
            self.board.update_task(task, ["status", "claim_nonce"])
            self.board.append_task_report(task, "Merged", f"Squash-merged into {self.cfg.main_branch} at "
                                          f"{utcnow().isoformat(timespec='minutes')}. PR: {task.pr_url or '(none)'}")
            self.log(f"[{task.id}] merged")
            return True
        finally:
            self.ws.dispose(wt)
