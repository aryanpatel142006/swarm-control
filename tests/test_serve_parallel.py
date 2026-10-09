"""reviewer.parallel: serve runs several reviews at once, each in its own thread (Oct 9 2026: six tasks stacked up in
Review one at a time and the one the demo waited on sat 25 min)."""
import threading
import time
from collections import Counter
from datetime import timedelta

import pytest

from swarm.board.memory import InMemoryBoard
from swarm.config import ConfigError, _role
from swarm.models import Status, Task, utcnow
from swarm.reviewer import Reviewer
from swarm.serve import Server
from swarm.workspace import CmdResult, Workspace

WAIT = 5


class BlockingReviewer:
    """Each review blocks until the test releases its task; `fail` raises inside the review instead."""

    def __init__(self, board, fail=(), write_board=True):
        self.board, self.fail, self.write_board = board, set(fail), write_board
        self.seen: list[str] = []
        self.threads: list[str] = []
        self.entered = {}
        self.release = {}
        self._lock = threading.Lock()

    def gate(self, task_id):
        with self._lock:
            self.entered.setdefault(task_id, threading.Event())
            return self.release.setdefault(task_id, threading.Event())

    def process(self, task):
        gate = self.gate(task.id)
        with self._lock:
            self.seen.append(task.id)
            self.threads.append(threading.current_thread().name)
        self.entered[task.id].set()
        if task.id in self.fail:
            raise RuntimeError(f"boom in {task.id}")
        assert gate.wait(WAIT), f"{task.id} never released"
        task.status = Status.MERGE_READY
        if self.write_board:
            self.board.update_task(task, ["status"])
        return task


def make(cfg, git_repo, tmp_path, parallel, **kw):
    cfg.reviewer.parallel = parallel
    board = InMemoryBoard()
    ws = Workspace(git_repo, tmp_path / "wt", gh=lambda a, c: CmdResult(0, "", ""))
    clock = {"now": utcnow()}
    logs: list[str] = []
    rev = BlockingReviewer(board, **kw)
    srv = Server(cfg, board, ws, reviewer=rev, merger=None, now=lambda: clock["now"], sleep=lambda s: None,
                 log=lambda *a: logs.append(" ".join(str(x) for x in a)))
    return srv, board, rev, clock, logs


def review_tasks(board, n=3):
    imps = ["normal", "critical", "high", "low"]
    return [board.create_task(Task(id="", title=f"r{i}", status=Status.REVIEW, importance=imps[i % 4], priority=1))
            for i in range(n)]


def finish(srv, rev, task_id):
    """Release a review and wait until its thread has logged, recorded and left the in-flight map."""
    rev.gate(task_id).set()
    deadline = time.monotonic() + WAIT
    while True:
        with srv._review_lock:
            if task_id not in srv._review_threads:
                return
        assert time.monotonic() < deadline, f"{task_id} still in flight"
        time.sleep(0.01)


def test_two_reviews_in_flight_third_waits_none_twice(cfg, git_repo, tmp_path):
    srv, board, rev, clock, logs = make(cfg, git_repo, tmp_path, 2)
    normal, crit, high = review_tasks(board)
    assert srv.review_pending() == 2                      # critical first, then high; normal waits
    for tid in (crit.id, high.id):
        rev.gate(tid)
        assert rev.entered[tid].wait(WAIT)
    assert srv.reviews_in_flight() == sorted([crit.id, high.id])
    assert any(line.startswith("reviewing ") and "2/2 in flight" in line for line in logs)
    assert srv.review_pending() == 0                      # both slots busy: nothing new, nothing picked again
    assert sorted(rev.seen) == sorted([crit.id, high.id])
    finish(srv, rev, crit.id)
    assert board.get_task(crit.id).status is Status.MERGE_READY
    assert srv.review_pending() == 1                      # the freed slot takes the waiting task, not high again
    rev.gate(normal.id)
    assert rev.entered[normal.id].wait(WAIT)
    assert srv.reviews_in_flight() == sorted([high.id, normal.id])
    finish(srv, rev, high.id)
    finish(srv, rev, normal.id)
    srv.join_reviews(WAIT)
    assert srv.reviews_in_flight() == []
    assert srv.review_pending() == 0
    assert Counter(rev.seen) == Counter({crit.id: 1, high.id: 1, normal.id: 1})
    assert all(name.startswith("review-") for name in rev.threads)


def test_an_exception_in_one_review_does_not_stop_the_other(cfg, git_repo, tmp_path):
    srv, board, rev, clock, logs = make(cfg, git_repo, tmp_path, 2)
    normal, crit = review_tasks(board, 2)
    rev.fail = {crit.id}
    assert srv.review_pending() == 2
    rev.gate(crit.id)
    assert rev.entered[crit.id].wait(WAIT)
    finish(srv, rev, crit.id)                             # raised inside its thread
    assert any("serve step reviewed failed" in line and crit.id in line and "boom" in line for line in logs)
    assert srv.reviews_in_flight() == [normal.id]
    summary = srv.tick()                                  # serve keeps ticking; the failed task is tried again
    assert "reviewed" in summary
    finish(srv, rev, normal.id)
    srv.join_reviews(WAIT)
    assert board.get_task(normal.id).status is Status.MERGE_READY
    assert rev.seen.count(normal.id) == 1 and rev.seen.count(crit.id) == 2


def test_a_finished_review_is_not_restarted_while_the_board_lags(cfg, git_repo, tmp_path):
    # the review returns MERGE_READY but the board query still says Review (Notion is eventually consistent)
    srv, board, rev, clock, logs = make(cfg, git_repo, tmp_path, 2, write_board=False)
    (t,) = review_tasks(board, 1)
    rev.gate(t.id).set()
    assert srv.review_pending() == 1
    srv.join_reviews(WAIT)
    assert srv.review_pending() == 0 and rev.seen == [t.id]
    clock["now"] += timedelta(seconds=Server.REVIEW_SETTLE_S + 1)
    assert srv.review_pending() == 1                      # still Review long after: review it again
    srv.join_reviews(WAIT)
    assert rev.seen == [t.id, t.id]


def test_parallel_one_reviews_in_order_on_the_calling_thread(cfg, git_repo, tmp_path):
    srv, board, rev, clock, logs = make(cfg, git_repo, tmp_path, 1)
    normal, crit, high = review_tasks(board)
    for t in (normal, crit, high):
        rev.gate(t.id).set()
    srv.review_batch = 2
    assert srv.review_pending() == 2
    assert rev.seen == [crit.id, high.id]
    assert rev.threads == [threading.current_thread().name] * 2 and srv.reviews_in_flight() == []
    assert srv.review_pending() == 1 and rev.seen == [crit.id, high.id, normal.id]


def test_no_reviewer_role_means_one_at_a_time(cfg, git_repo, tmp_path):
    srv, *_ = make(cfg, git_repo, tmp_path, 3)
    assert srv.review_parallel() == 3
    srv.cfg.reviewer = None
    assert srv.review_parallel() == 1


def test_reviewer_parallel_config(cfg):
    agents = cfg.agents
    assert _role({"agent": "codex-a", "model": "m"}, agents, "reviewer").parallel == 1
    assert _role({"agent": "codex-a", "model": "m", "parallel": 2}, agents, "reviewer").parallel == 2
    with pytest.raises(ConfigError):
        _role({"agent": "codex-a", "model": "m", "parallel": -1}, agents, "reviewer")
    with pytest.raises(ConfigError):
        _role({"agent": "codex-a", "model": "m", "parallel": "two"}, agents, "reviewer")


def test_reviewer_last_agent_is_per_thread(cfg, git_repo, tmp_path):
    ws = Workspace(git_repo, tmp_path / "wt", gh=lambda a, c: CmdResult(0, "", ""))
    rev = Reviewer(cfg, InMemoryBoard(), ws, prompt_text="review", log=lambda *a: None)
    rev.last_agent = "codex-a"
    seen = []
    th = threading.Thread(target=lambda: (seen.append(rev.last_agent), setattr(rev, "last_agent", "claude-a")))
    th.start()
    th.join(WAIT)
    assert seen == [None] and rev.last_agent == "codex-a"
