"""Merger: merge current main into the branch, fast verify, force-with-lease push, squash merge, mark Done."""
from __future__ import annotations

import time
from typing import Callable

from .board.base import Board
from .config import Config
from .feedback import verify_feedback
from .models import Question, Status, Task, utcnow
from .workspace import CmdResult, Workspace

MAX_MERGE_FAILURES = 3
MERGEABILITY_POLLS = 12      # × MERGEABILITY_WAIT_S ≈ 36 s for GitHub to recompute after a push
MERGEABILITY_WAIT_S = 3.0


def merge_failures(task: Task) -> int:
    return sum(1 for f in task.flags if f.startswith("merge_failed"))


def conflict_feedback(conflicts: list[str], causes: list[str], main_branch: str) -> str:
    """Changes-requested text for a branch that no longer merges cleanly. It tells the worker what will be in its
    worktree, not to rebase: the Codex sandbox cannot write rebase or fetch metadata (Q-140, Q-144, Q-146) and
    three Claude attempts at T-063 went to a hand rebase instead of the task (Q-137)."""
    return ("Main moved and now conflicts with this branch in: " + ", ".join(conflicts)
            + (" (main changed them in: " + "; ".join(causes) + ")" if causes else "")
            + f". Before your next run the harness merges origin/{main_branch} into your branch and leaves conflict "
            "markers in those files. Resolve them keeping main's intent and this task's change, `git add` them and "
            "`git commit`. Do not run `git rebase`, `git fetch` or `git merge` yourself.")


class Merger:
    def __init__(self, cfg: Config, board: Board, ws: Workspace, *, log=print,
                 sleep: Callable[[float], None] = time.sleep):
        self.cfg, self.board, self.ws, self.log, self.sleep = cfg, board, ws, log, sleep

    @staticmethod
    def _ref(task: Task) -> str:
        return task.pr_url or task.branch

    def _wait_mergeable(self, task: Task) -> str:
        """After a push GitHub reports UNKNOWN for a few seconds (and briefly an old head); merging then fails."""
        state, tip = "UNKNOWN", self.ws.remote_tip(task.branch)
        for _ in range(MERGEABILITY_POLLS):
            info = self.ws.pr_info(self._ref(task))
            mergeable = str(info.get("mergeable") or ("N/A" if not info else "UNKNOWN"))
            state = str(info.get("mergeStateStatus") or ("N/A" if not info else "UNKNOWN"))
            head_ok = not tip or not info.get("headRefOid") or info["headRefOid"] == tip
            if mergeable != "UNKNOWN" and state != "UNKNOWN" and head_ok:   # settled, or N/A (nothing to wait for)
                return state
            self.sleep(MERGEABILITY_WAIT_S)
        return state

    def _gh_merge(self, task: Task):
        ref = self._ref(task)
        before = self.ws.pr_info(ref)
        if before and str(before.get("state", "OPEN")).upper() != "OPEN":
            # a stale link (branch names repeat across projects; an old runner may still link the old PR):
            # open a fresh PR for this branch and merge that one
            try:
                fresh = self.ws.pr_create_or_update(task.branch, task.title_with_id(),
                                                    f"Reopened by the merger: the linked PR {ref} was {before.get('state')}.")
            except RuntimeError as e:
                return CmdResult(1, "", f"PR {ref} is {before.get('state')} and a new PR could not be opened: {e}")
            if not fresh:
                return CmdResult(1, "", f"PR {ref} is {before.get('state')} and no new PR could be opened")
            self.log(f"[{task.id}] stale PR {ref} → new PR {fresh}")
            task.pr_url = ref = fresh
            self.board.update_task(task, ["pr_url"])
        self._wait_mergeable(task)
        r = self.ws.pr_merge(ref)
        if not r.ok and "not mergeable" in (r.err + r.out):
            self.sleep(MERGEABILITY_WAIT_S * 2)   # one more chance for GitHub to settle
            self._wait_mergeable(task)
            r = self.ws.pr_merge(ref)
        if r.ok:
            after = self.ws.pr_info(ref)
            if after and str(after.get("state", "MERGED")).upper() != "MERGED":
                return CmdResult(1, "", f"gh reported success but PR {ref} is {after.get('state')}, not merged")
        return r

    def _back(self, task: Task, feedback: str) -> bool:
        task.status = Status.CHANGES_REQUESTED
        task.feedback = feedback[:6000]      # Notion rich text is chunked; a 1900 cut lost the failing step
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
                id="", text=f"{task.id} could not be merged {n} times: {error[:600]}", kind="blocking",
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
            # merge, never rebase: a branch may hold a conflict-resolution merge commit (the worker resolved the
            # markers the runner left), and a rebase would replay those conflicts. The PR is squash-merged.
            ok, conflicts, causes = self.ws.merge_main(wt, keep_conflicts=False)
            if not ok:
                return self._back(task, conflict_feedback(conflicts, causes, self.cfg.main_branch))
            self.ws.run_script(wt, self.cfg.verify.setup_worktree, 600)
            verify = self.ws.run_script(wt, self.cfg.verify.fast, 900)
            if verify is not None and not verify.ok:
                return self._back(task, verify_feedback(
                    verify.out + ("\n" + verify.err if verify.err else ""), code=verify.code,
                    intro=f"{self.cfg.verify.fast} failed after merging current main into the branch. Fix it."))
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
