from datetime import timedelta

from swarm.board.memory import InMemoryBoard
from swarm.models import AgentRow, Question, Status, Task, utcnow
from swarm.serve import Server
from swarm.workspace import CmdResult, Workspace


class FakeReviewer:
    def __init__(self, board):
        self.board = board
        self.seen = []

    def process(self, task):
        self.seen.append(task.id)
        task.status = Status.MERGE_READY
        self.board.update_task(task, ["status"])
        return task


class FakeMerger:
    def __init__(self, board):
        self.board = board
        self.seen = []

    def merge(self, task):
        self.seen.append(task.id)
        task.status = Status.DONE
        self.board.update_task(task, ["status"])
        return True


def make(cfg, git_repo, tmp_path, board=None, now=None):
    board = board or InMemoryBoard()
    ws = Workspace(git_repo, tmp_path / "wt", gh=lambda a, c: CmdResult(0, "", ""))
    clock = {"now": now or utcnow()}
    srv = Server(cfg, board, ws, reviewer=FakeReviewer(board), merger=FakeMerger(board),
                 now=lambda: clock["now"], sleep=lambda s: None, log=lambda *a: None)
    return srv, board, clock


def test_lock(cfg, git_repo, tmp_path):
    srv, board, clock = make(cfg, git_repo, tmp_path)
    assert srv.acquire_lock() is True
    assert srv.acquire_lock() is True  # re-acquiring my own lock is fine
    other, _, _ = make(cfg, git_repo, tmp_path, board=board, now=clock["now"])
    other.host = "laptop-b"
    assert other.acquire_lock() is False  # another laptop while the heartbeat is fresh
    restarted, _, _ = make(cfg, git_repo, tmp_path, board=board, now=clock["now"])
    assert restarted.acquire_lock() is True  # same laptop: a restart takes over immediately
    clock["now"] += timedelta(minutes=30)
    other.now = lambda: clock["now"]
    assert other.acquire_lock() is True  # stale heartbeat: anyone may take over


def test_reap_stale_running(cfg, git_repo, tmp_path):
    srv, board, clock = make(cfg, git_repo, tmp_path)
    now = clock["now"]
    board.upsert_agent(AgentRow(name="claude-a", last_heartbeat=now - timedelta(minutes=20), current_task="T-001"))
    board.upsert_agent(AgentRow(name="codex-a", last_heartbeat=now - timedelta(minutes=1)))
    t1 = board.create_task(Task(id="", title="stale", status=Status.RUNNING, agent="claude-a"))
    t2 = board.create_task(Task(id="", title="fresh", status=Status.RUNNING, agent="codex-a"))
    t3 = board.create_task(Task(id="", title="noagentrow", status=Status.RUNNING, agent="fake-b"))
    assert srv.reap() == 2
    s1 = board.get_task(t1.id)
    assert s1.status is Status.READY and s1.attempts == 1 and "resume" in s1.flags and s1.claim_nonce == ""
    assert board.get_task(t2.id).status is Status.RUNNING
    assert board.get_task(t3.id).status is Status.READY
    offline = board.get_agent("claude-a")
    assert offline.status == "offline" and offline.current_task == ""


def test_retry_ladder(cfg, git_repo, tmp_path):
    srv, board, clock = make(cfg, git_repo, tmp_path)
    t = board.create_task(Task(id="", title="f", type="backend", importance="normal", status=Status.FAILED,
                               attempts=1, agent="codex-a", model="gpt-6-sol", last_error="boom"))
    assert srv.retry_failed() == 1
    s = board.get_task(t.id)
    assert s.status is Status.READY and s.importance == "normal" and "boom" in s.feedback
    s.status, s.attempts = Status.FAILED, 2
    board.update_task(s, ["status", "attempts"])
    srv.retry_failed()
    s2 = board.get_task(t.id)
    assert s2.status is Status.READY and s2.importance == "high" and s2.model == "gpt-6-astra"
    s2.status, s2.attempts = Status.FAILED, 3
    board.update_task(s2, ["status", "attempts"])
    srv.retry_failed()
    assert board.get_task(t.id).status is Status.BLOCKED
    assert board.list_questions(status="Open")[0].task_id == t.id


def test_relay_blocking_and_fyi(cfg, git_repo, tmp_path):
    srv, board, clock = make(cfg, git_repo, tmp_path)
    t = board.create_task(Task(id="", title="b", status=Status.BLOCKED, agent="claude-a", milestone="M2",
                               scope=["src/**"]))
    q = board.create_question(Question(id="", text="which?", kind="blocking", task_id=t.id))
    q2 = board.create_question(Question(id="", text="chose 3s", kind="fyi", task_id=t.id, impact="high"))
    assert srv.relay() == 0  # nothing answered yet
    q.answer = "use tap"
    board.update_question(q, ["answer"])
    q2.answer = "make it 5s"
    q2.needs_follow_up = True
    board.update_question(q2, ["answer", "needs_follow_up"])
    assert srv.relay() == 2
    s = board.get_task(t.id)
    assert s.status is Status.READY and "use tap" in s.feedback and "resume" in s.flags
    assert all(x.status == "Applied" for x in board.list_questions())
    follow = [x for x in board.list_tasks() if x.id != t.id][0]
    assert follow.type == "bugfix" and follow.importance == "high" and "make it 5s" in follow.feedback
    assert follow.depends_on == [t.id] and follow.status is Status.BACKLOG and follow.agent is not None


def test_promote_dependents(cfg, git_repo, tmp_path):
    srv, board, clock = make(cfg, git_repo, tmp_path)
    a = board.create_task(Task(id="", title="a", status=Status.DONE))
    b = board.create_task(Task(id="", title="b", status=Status.BACKLOG, depends_on=[a.id], type="frontend",
                               importance="high"))
    c = board.create_task(Task(id="", title="c", status=Status.BACKLOG, depends_on=[a.id, b.id]))
    assert srv.promote() == 1
    sb = board.get_task(b.id)
    assert sb.status is Status.READY and sb.agent == "claude-a" and sb.model == "opus"
    assert board.get_task(c.id).status is Status.BACKLOG


def test_review_and_merge_pending(cfg, git_repo, tmp_path):
    srv, board, clock = make(cfg, git_repo, tmp_path)
    r = board.create_task(Task(id="", title="r", status=Status.REVIEW, priority=2))
    m = board.create_task(Task(id="", title="m", status=Status.MERGE_READY, priority=1))
    d = board.create_task(Task(id="", title="d", status=Status.BACKLOG, depends_on=[m.id]))
    assert srv.review_pending() == 1 and srv.reviewer.seen == [r.id]
    assert srv.merge_pending() == 2  # m, then r (now merge ready)
    assert board.get_task(d.id).status is Status.READY


def test_reroute_unknown_agent_and_cooldown(cfg, git_repo, tmp_path):
    srv, board, clock = make(cfg, git_repo, tmp_path)
    now = clock["now"]
    board.upsert_agent(AgentRow(name="codex-a", status="cooldown", cooldown_until=now + timedelta(minutes=10)))
    ghost = board.create_task(Task(id="", title="g", status=Status.READY, agent="not-configured", type="docs"))
    cold = board.create_task(Task(id="", title="c", status=Status.READY, agent="codex-a", type="backend",
                                  importance="normal"))
    crit = board.create_task(Task(id="", title="k", status=Status.READY, agent="codex-a", type="backend",
                                  importance="critical"))
    assert srv.reroute() == 2
    assert board.get_task(ghost.id).agent == "claude-a"
    assert board.get_task(cold.id).agent == "claude-a"
    assert board.get_task(crit.id).agent == "codex-a"


def test_tick_writes_status(cfg, git_repo, tmp_path):
    srv, board, clock = make(cfg, git_repo, tmp_path)
    board.create_task(Task(id="", title="x", status=Status.READY, agent="claude-a", milestone="M1"))
    summary = srv.tick()
    assert set(summary) >= {"reaped", "retried", "relayed", "promoted", "reviewed", "merged", "rerouted"}
    assert "SWARM STATUS" in board.status_page


def test_step_exception_does_not_abort_tick(cfg, git_repo, tmp_path):
    srv, board, clock = make(cfg, git_repo, tmp_path)

    def boom(task):
        raise RuntimeError("boom")

    srv.reviewer.process = boom
    board.create_task(Task(id="", title="r", status=Status.REVIEW))
    board.create_task(Task(id="", title="m", status=Status.MERGE_READY))
    summary = srv.tick()
    assert summary["merged"] == 1 and summary["errors"] == 1


def test_reroute_changes_requested_on_offline_agent(cfg, git_repo, tmp_path):
    srv, board, clock = make(cfg, git_repo, tmp_path)
    board.upsert_agent(AgentRow(name="codex-a", status="offline"))
    t = board.create_task(Task(id="", title="cr", status=Status.CHANGES_REQUESTED, agent="codex-a", type="backend"))
    assert srv.reroute() == 1 and board.get_task(t.id).agent == "claude-a"


def test_reap_orphan_with_fresh_heartbeat(cfg, git_repo, tmp_path):
    srv, board, clock = make(cfg, git_repo, tmp_path)
    now = clock["now"]
    board.upsert_agent(AgentRow(name="codex-a", last_heartbeat=now, current_task=""))
    t = board.create_task(Task(id="", title="orphan", status=Status.RUNNING, agent="codex-a",
                               started=now - timedelta(minutes=15)))
    fresh = board.create_task(Task(id="", title="just started", status=Status.RUNNING, agent="codex-a",
                                   started=now - timedelta(seconds=30)))
    assert srv.reap() == 1
    assert board.get_task(t.id).status is Status.READY and board.get_task(fresh.id).status is Status.RUNNING


def test_assign_ids_to_handmade_tasks(cfg, git_repo, tmp_path):
    srv, board, clock = make(cfg, git_repo, tmp_path)
    board.create_task(Task(id="", title="normal"))
    board.tasks[""] = Task(id="", title="hand made", status=Status.READY, page_id="pg-hand")
    assert srv.assign_ids() == 1
    assert board.get_task("T-002").title == "hand made" and "" not in board.tasks


def test_relay_on_merge_failed_task_retries_merge_or_marks_done(cfg, git_repo, tmp_path):
    srv, board, clock = make(cfg, git_repo, tmp_path)
    t = board.create_task(Task(id="", title="stuck", status=Status.BLOCKED, agent="claude-a",
                               flags=["merge_failed_1", "merge_failed_2", "merge_failed_3"]))
    q = board.create_question(Question(id="", text="could not be merged", kind="blocking", task_id=t.id, answer="retry"))
    assert srv.relay() == 1
    s = board.get_task(t.id)
    assert s.status is Status.MERGE_READY and not any(f.startswith("merge_failed") for f in s.flags)
    t2 = board.create_task(Task(id="", title="handmerged", status=Status.BLOCKED, agent="claude-a", flags=["merge_failed_3"]))
    board.create_question(Question(id="", text="could not be merged", kind="blocking", task_id=t2.id, answer="I merged it by hand"))
    srv.relay()
    assert board.get_task(t2.id).status is Status.DONE


def test_relay_cut_answer_cuts_the_task(cfg, git_repo, tmp_path):
    srv, board, clock = make(cfg, git_repo, tmp_path)
    t = board.create_task(Task(id="", title="b", status=Status.BLOCKED, agent="claude-a"))
    board.create_question(Question(id="", text="keeps failing", kind="blocking", task_id=t.id, answer="cut it"))
    assert srv.relay() == 1
    assert board.get_task(t.id).status is Status.CUT


def test_rebalance_moves_queued_work_to_idle_equal_agent(cfg, git_repo, tmp_path):
    srv, board, clock = make(cfg, git_repo, tmp_path)
    # claude-a and codex-a both score 4 on ml_audio? no: both default 3 → equal. codex-a has a queue, claude-a is idle.
    for i in range(3):
        board.create_task(Task(id="", title=f"q{i}", status=Status.READY, agent="codex-a", type="ml_audio"))
    board.create_task(Task(id="", title="busy", status=Status.RUNNING, agent="codex-a", type="ml_audio"))
    moved = srv.rebalance()
    assert moved == 2  # claude-a and fake-b are both idle and both score >= codex-a on ml_audio
    agents = [board.get_task(f"T-00{i}").agent for i in (1, 2, 3)]
    assert agents.count("claude-a") == 1 and agents.count("fake-b") == 1 and agents.count("codex-a") == 1
    # a critical task is never moved, and nothing moves to a weaker agent
    board.create_task(Task(id="", title="crit", status=Status.READY, agent="codex-a", type="backend", importance="critical"))
    assert srv.rebalance() == 0


def test_reap_orphan_rule_is_immune_to_clock_skew(cfg, git_repo, tmp_path):
    """Timestamps written by another laptop are compared against that laptop's own heartbeat, never serve's clock."""
    srv, board, clock = make(cfg, git_repo, tmp_path)
    now = clock["now"]
    their_now = now - timedelta(minutes=7)   # the other laptop's clock runs 7 minutes behind
    board.upsert_agent(AgentRow(name="codex-a", last_heartbeat=their_now, current_task=""))
    live = board.create_task(Task(id="", title="live", status=Status.RUNNING, agent="codex-a",
                                  started=their_now - timedelta(seconds=30)))
    stale = board.create_task(Task(id="", title="stale", status=Status.RUNNING, agent="codex-a",
                                   started=their_now - timedelta(minutes=15)))
    assert srv.reap() == 1
    assert board.get_task(live.id).status is Status.RUNNING
    assert board.get_task(stale.id).status is Status.READY
