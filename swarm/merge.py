"""Merger: rebase onto main, fast verify, force-with-lease push, squash merge, mark Done."""
from __future__ import annotations

import time
from typing import Callable

from .board.base import Board
from .config import Config
from .models import Question, Status, Task, utcnow
from .workspace import Workspace

MAX_MERGE_FAILURES = 3
MERGEABILITY_POLLS = 12      # × MERGEABILITY_WAIT_S ≈ 36 s for GitHub to recompute after a push
MERGEABILITY_WAIT_S = 3.0


def merge_failures(task: Task) -> int:
    return sum(1 for f in task.flags if f.startswith("merge_failed"))


class Merger:
    def __init__(self, cfg: Config, board: Board, ws: Workspace, *, log=print,
                 sleep: Callable[[float], None] = time.sleep):
        self.cfg, self.board, self.ws, self.log, self.sleep = cfg, board, ws, log, sleep

    def _wait_mergeable(self, branch: str) -> str:
        """After a push GitHub reports UNKNOWN for a few seconds; merging then fails with 'not mergeable'."""
        state = "UNKNOWN"
        for _ in range(MERGEABILITY_POLLS):
            mergeable, state = self.ws.pr_state(branch)
            if mergeable != "UNKNOWN" and state != "UNKNOWN":   # settled, or N/A (nothing to wait for)
                return state
            self.sleep(MERGEABILITY_WAIT_S)
        return state

    def _gh_merge(self, task: Task):
        self._wait_mergeable(task.branch)
        r = self.ws.pr_merge(task.branch)
        if not r.ok and "not mergeable" in (r.err + r.out):
            self.sleep(MERGEABILITY_WAIT_S * 2)   # one more chance for GitHub to settle
            self._wait_mergeable(task.branch)
            r = self.ws.pr_merge(task.branch)
        return r

    def _back(self, task: Task, feedback: str) -> bool:
        task.status = Status.CHANGES_REQUESTED
        task.feedback = feedback[:1900]
        task.flags = list(dict.fromkeys(task.flags + ["resume"]))
        self.board.update_task(task, ["status", "feedback", "flags"])
        self.log(f"[{task.id}] merge → changes requested")
        return False

    def _merge_failed(self, task: Task, error: str) -> bool:
        n = merge_failures(task) + 1
        task.last_error = error[:1900]
        task.flags = list(dict.fromkeys(task.flags + [f"merge_failed_{n}"]))
        if n >= MAX_MERGE_FAILURES:
            task.status = Status.BLOCKED
            self.board.update_task(task, ["last_error", "flags", "status"])
            self.board.create_question(Question(
                id="", text=f"{task.id} could not be merged {n} times: {error[:100]}"[:190], kind="blocking",
                context=error[:1900], options=["merge by hand then answer 'merged'", "cut", "human fix"],
                impact="high", task_id=task.id, asked_by="serve"))
            self.log(f"[{task.id}] merge failed {n} times → blocked")
        else:
            self.board.update_task(task, ["last_error", "flags"])
            self.log(f"[{task.id}] merge failed ({n}/{MAX_MERGE_FAILURES}): {error[:120]}")
        return False

    def merge(self, task: Task) -> bool:
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
                return self._merge_failed(task, "push failed: " + push.err.strip())
        finally:
            # release the branch before gh touches it: a checked-out branch cannot be deleted or fast-forwarded
            self.ws.dispose(wt)
        r = self._gh_merge(task)
        if not r.ok:
            return self._merge_failed(task, "gh pr merge failed: " + (r.err.strip() or r.out.strip()))
        task.status, task.claim_nonce = Status.DONE, ""
        task.flags = [f for f in task.flags if not f.startswith("merge_failed")]
        self.board.update_task(task, ["status", "claim_nonce", "flags"])
        self.board.append_task_report(task, "Merged", f"Squash-merged into {self.cfg.main_branch} at "
                                      f"{utcnow().isoformat(timespec='minutes')}. PR: {task.pr_url or '(none)'}")
        self.ws.delete_remote_branch(task.branch)
        self.log(f"[{task.id}] merged")
        return True
