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


def test_review_and_merge_take_critical_first(cfg, git_repo, tmp_path):
    srv, board, clock = make(cfg, git_repo, tmp_path)
    srv.review_batch = 1
    hi = board.create_task(Task(id="", title="h", status=Status.REVIEW, importance="high", priority=1))
    crit = board.create_task(Task(id="", title="c", status=Status.REVIEW, importance="critical", priority=5))
    assert srv.review_pending() == 1 and srv.reviewer.seen == [crit.id]
    assert srv.review_pending() == 1 and srv.reviewer.seen == [crit.id, hi.id]



    srv, board, clock = make(cfg, git_repo, tmp_path)
    board.upsert_agent(AgentRow(name="claude-a", status="idle", last_heartbeat=utcnow()))   # checked in
    now = clock["now"]
    board.upsert_agent(AgentRow(name="codex-a", status="cooldown", cooldown_until=now + timedelta(minutes=10)))
    ghost = board.create_task(Task(id="", title="g", status=Status.READY, agent="not-configured", type="docs"))
    cold = board.create_task(Task(id="", title="c", status=Status.READY, agent="codex-a", type="backend",
                                  importance="normal"))
    crit = board.create_task(Task(id="", title="k", status=Status.READY, agent="codex-a", type="backend",
                                  importance="critical"))
    assert srv.reroute() == 3
    assert board.get_task(ghost.id).agent == "claude-a"
    assert board.get_task(cold.id).agent == "claude-a"
    # critical work also leaves a cooling agent when a capable agent is idle (Oct 5 2026: four hours lost otherwise)
    assert board.get_task(crit.id).agent == "claude-a"


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
    board.upsert_agent(AgentRow(name="claude-a", status="idle", last_heartbeat=utcnow()))   # checked in
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


def test_foreign_blocking_question_does_not_cut_or_unblock_the_task(cfg, git_repo, tmp_path):
    srv, board, clock = make(cfg, git_repo, tmp_path)
    t = board.create_task(Task(id="", title="b", status=Status.BLOCKED, agent="muse-c"))
    q = board.create_question(Question(id="", text="[muse-b -> muse-c] hi", kind="blocking", task_id=t.id,
                                       asked_by="muse-b", answer="cut it"))
    assert srv.relay() == 1
    assert board.get_task(t.id).status is Status.BLOCKED
    assert board.list_questions()[0].status == "Applied"


def test_blocking_question_from_own_agent_host_orchestrator_or_serve_still_blocks(cfg, git_repo, tmp_path):
    for who in ("claude-a", "claude-a@laptop-a", "orchestrator@laptop-a", "orchestrator", "serve", "reviewer"):
        srv, board, clock = make(cfg, git_repo, tmp_path)
        t = board.create_task(Task(id="", title="b", status=Status.BLOCKED, agent="claude-a"))
        board.create_question(Question(id="", text="which?", kind="blocking", task_id=t.id, asked_by=who,
                                       answer="cut it"))
        assert srv.relay() == 1
        assert board.get_task(t.id).status is Status.CUT, who


def test_blocks_task_rules():
    from swarm.relay import blocks_task
    t = Task(id="T-1", title="x", agent="claude-a3")
    mk = lambda who, kind="blocking": Question(id="Q", text="x", kind=kind, asked_by=who, task_id="T-1")
    assert blocks_task(mk("claude-a3"), t) and blocks_task(mk("orchestrator@laptop-a"), t)
    assert blocks_task(mk(""), t) and blocks_task(mk("serve"), t)
    assert blocks_task(mk("codex-x"), t, ("codex-x",))
    assert not blocks_task(mk("muse-b"), t) and not blocks_task(mk("muse-b@laptop-c"), t)
    assert not blocks_task(mk("claude-a3", "fyi"), t)


def test_rebalance_moves_queued_work_to_idle_equal_agent(cfg, git_repo, tmp_path):
    srv, board, clock = make(cfg, git_repo, tmp_path)
    board.upsert_agent(AgentRow(name="claude-a", status="idle", last_heartbeat=utcnow()))   # checked in
    board.upsert_agent(AgentRow(name="fake-b", status="idle", last_heartbeat=utcnow()))     # checked in
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


def test_status_page_failure_does_not_stop_the_loop(cfg, git_repo, tmp_path):
    """A deleted or archived Status page must not take serve down; it logs once and keeps working."""
    board = InMemoryBoard()

    def boom(text):
        raise RuntimeError("Notion 400 validation_error: Can't edit block that is archived")

    board.write_status_page = boom
    srv, board, clock = make(cfg, git_repo, tmp_path, board=board)
    logs = []
    srv.log = logs.append
    assert srv.write_status(force=True) is None
    assert srv.write_status(force=True) is None
    assert sum("status page" in line for line in logs) == 1   # logged once, not every tick
    srv.tick()   # still alive


def test_serve_runs_a_retro_once_when_a_milestone_completes(cfg, git_repo, tmp_path):
    """Self-improvement runs on its own: when every task of a milestone is Done or Cut, serve calls the retro once."""
    srv, board, clock = make(cfg, git_repo, tmp_path)
    srv.retro_state = tmp_path / "retro.json"
    calls = []
    srv.retro = lambda ms=None: calls.append(clock["now"]) or []
    t1 = board.create_task(Task(id="", title="a", status=Status.RUNNING, agent="claude-a", milestone="M1"))
    t2 = board.create_task(Task(id="", title="b", status=Status.DONE, agent="claude-a", milestone="M1"))
    board.create_task(Task(id="", title="c", status=Status.READY, agent="claude-a", milestone="M2"))
    srv.tick()
    assert calls == []                      # M1 still has a running task
    t1 = board.get_task(t1.id); t1.status = Status.CUT; board.update_task(t1, ["status"])
    srv.tick()
    srv.tick()
    assert len(calls) == 1                  # once for M1, not every tick
    assert "M1" in srv.retro_state.read_text()


def test_promote_treats_a_cut_dependency_as_resolved_and_blocks_on_an_unknown_one(cfg, git_repo, tmp_path):
    """A dependent of a cut task used to wait in Backlog forever, so its milestone (and the retro) never finished."""
    srv, board, clock = make(cfg, git_repo, tmp_path)
    board.upsert_agent(AgentRow(name="claude-a", status="idle", last_heartbeat=utcnow()))
    cut = board.create_task(Task(id="", title="dropped", status=Status.CUT, agent="claude-a", type="backend"))
    dep = board.create_task(Task(id="", title="needs it", status=Status.BACKLOG, type="backend", depends_on=[cut.id]))
    ghost = board.create_task(Task(id="", title="typo dep", status=Status.BACKLOG, type="backend", depends_on=["T-999"]))
    srv.promote()
    d = board.get_task(dep.id)
    assert d.status is Status.READY and cut.id in d.feedback and "cut" in d.feedback.lower()
    g = board.get_task(ghost.id)
    assert g.status is Status.BLOCKED
    qs = [q for q in board.list_questions(status="Open") if q.task_id == ghost.id]
    assert len(qs) == 1 and "T-999" in qs[0].text
    srv.promote()
    assert len([q for q in board.list_questions(status="Open") if q.task_id == ghost.id]) == 1   # asked once


def test_promote_blocks_a_dependency_cycle_once(cfg, git_repo, tmp_path):
    srv, board, clock = make(cfg, git_repo, tmp_path)
    a = board.create_task(Task(id="", title="a", status=Status.BACKLOG, type="backend"))
    b = board.create_task(Task(id="", title="b", status=Status.BACKLOG, type="backend", depends_on=[a.id]))
    a = board.get_task(a.id); a.depends_on = [b.id]; board.update_task(a, ["depends_on"])
    srv.promote(); srv.promote()
    assert board.get_task(a.id).status is Status.BLOCKED and board.get_task(b.id).status is Status.BLOCKED
    qs = board.list_questions(status="Open")
    assert len(qs) == 1 and a.id in qs[0].text and b.id in qs[0].text and "cycle" in qs[0].text


def test_dependency_cycle_finder_is_linear_on_wide_graphs():
    import time as _t
    from swarm.serve import _dependency_cycles
    tasks = [Task(id=f"T-{i:03d}", title="x", depends_on=[f"T-{j:03d}" for j in range(max(0, i - 6), i)]) for i in range(300)]
    t0 = _t.monotonic()
    assert _dependency_cycles(tasks) == []
    assert _t.monotonic() - t0 < 1.0
    tasks[0].depends_on = ["T-299"]
    assert len(_dependency_cycles(tasks)) == 1


def test_serve_startup_retries_when_the_board_is_unreachable(cfg, git_repo, tmp_path):
    srv, board, clock = make(cfg, git_repo, tmp_path)
    calls, sleeps = {"n": 0}, []
    real = srv.acquire_lock

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise ConnectionError("Notion unreachable")
        return real()
    srv.acquire_lock = flaky
    srv.sleep = sleeps.append
    srv.loop(stop=lambda: True)
    assert calls["n"] == 3 and sleeps[:2] == [15, 30]


def test_reroute_moves_critical_work_off_an_offline_agent(cfg, git_repo, tmp_path):
    srv, board, clock = make(cfg, git_repo, tmp_path)
    board.upsert_agent(AgentRow(name="codex-a", status="offline", last_heartbeat=utcnow() - timedelta(minutes=40)))
    board.upsert_agent(AgentRow(name="claude-a", status="idle", last_heartbeat=utcnow()))
    t = board.create_task(Task(id="", title="api", status=Status.READY, agent="codex-a", type="backend", importance="critical"))
    assert srv.reroute() == 1 and board.get_task(t.id).agent == "claude-a"


def test_status_write_also_updates_the_headline(cfg, git_repo, tmp_path):
    srv, board, clock = make(cfg, git_repo, tmp_path)
    board.create_task(Task(id="", title="x", status=Status.READY, agent="claude-a", milestone="M1"))
    srv.write_status(force=True)
    text, color = board.headline
    assert color == "green_background" and "0/1 done" in text


def test_rebalance_lets_a_competent_idle_agent_take_work_a_saturated_stronger_agent_cannot_start(cfg, git_repo, tmp_path):
    """Oct 4 2026, selective-hearing M0: codex-b (infra 5, parallel 1) was busy, T-002 (infra) waited in its queue,
    claude-a (infra 4) sat idle. Idle beats waiting: a weaker-but-competent agent takes the task once the donor
    has no free slot; critical tasks still wait for the strongest agent."""
    srv, board, clock = make(cfg, git_repo, tmp_path)
    board.upsert_agent(AgentRow(name="claude-a", status="idle", last_heartbeat=utcnow()))
    board.upsert_agent(AgentRow(name="codex-a", status="running", last_heartbeat=utcnow()))
    codex, claude = cfg.agents["codex-a"], cfg.agents["claude-a"]
    assert codex.strengths["infra"] > claude.strengths["infra"] >= 3
    for i in range(codex.parallel):   # every slot busy
        board.create_task(Task(id="", title=f"busy{i}", status=Status.RUNNING, agent="codex-a", type="infra"))
    waiting = board.create_task(Task(id="", title="setup script", status=Status.READY, agent="codex-a", type="infra"))
    crit = board.create_task(Task(id="", title="core types", status=Status.READY, agent="codex-a", type="backend",
                                  importance="critical"))
    assert srv.rebalance() == 1
    assert board.get_task(waiting.id).agent == "claude-a"
    assert board.get_task(crit.id).agent == "codex-a"


def test_rebalance_leaves_work_with_a_stronger_agent_that_has_a_free_slot(cfg, git_repo, tmp_path):
    srv, board, clock = make(cfg, git_repo, tmp_path)
    board.upsert_agent(AgentRow(name="claude-a", status="idle", last_heartbeat=utcnow()))
    board.upsert_agent(AgentRow(name="codex-a", status="running", last_heartbeat=utcnow()))
    cfg.agents["codex-a"].parallel = 2          # one slot busy, one free
    board.create_task(Task(id="", title="busy", status=Status.RUNNING, agent="codex-a", type="infra"))
    waiting = board.create_task(Task(id="", title="setup script", status=Status.READY, agent="codex-a", type="infra"))
    assert srv.rebalance() == 0          # codex-a will pick it up on its next poll; no need to downgrade
    assert board.get_task(waiting.id).agent == "codex-a"


def test_reconcile_marks_tasks_done_when_their_pr_was_merged_by_a_human(cfg, git_repo, tmp_path):
    """Oct 4 2026: the user merged PR #3 on GitHub while its task was Running on a silent agent; the task was
    later retried on another agent and redid merged work."""
    srv, board, clock = make(cfg, git_repo, tmp_path)
    t = board.create_task(Task(id="", title="core", status=Status.RUNNING, agent="codex-a",
                               pr_url="https://github.com/x/y/pull/3"))
    u = board.create_task(Task(id="", title="open", status=Status.REVIEW, agent="codex-a",
                               pr_url="https://github.com/x/y/pull/4"))
    srv.ws.pr_info = lambda ref: {"state": "MERGED"} if ref.endswith("/3") else {"state": "OPEN"}
    assert srv.reconcile_merged() == 1
    assert board.get_task(t.id).status is Status.DONE and board.get_task(u.id).status is Status.REVIEW


def test_redistribute_when_an_agent_comes_back_online(cfg, git_repo, tmp_path):
    srv, board, clock = make(cfg, git_repo, tmp_path)
    board.upsert_agent(AgentRow(name="claude-a", status="idle", last_heartbeat=utcnow()))
    board.upsert_agent(AgentRow(name="codex-a", status="offline", last_heartbeat=utcnow()))
    assert srv.redistribute_on_return() == 0           # first sighting: nothing changed yet
    for i in range(3):   # backend work piled on claude-a while codex-a was away
        board.create_task(Task(id="", title=f"b{i}", status=Status.READY, agent="claude-a", type="backend"))
    board.upsert_agent(AgentRow(name="codex-a", status="idle", last_heartbeat=utcnow()))
    moved = srv.redistribute_on_return()
    assert moved >= 1
    agents = {board.get_task(f"T-00{i}").agent for i in (1, 2, 3)}
    assert "codex-a" in agents                       # codex-a is the stronger backend agent and is back
    assert srv.redistribute_on_return() == 0          # steady state: no churn


def test_critical_work_leaves_a_cooling_agent_for_an_idle_capable_one(cfg, git_repo, tmp_path):
    """Oct 5 2026 04:03-08:00 UTC: seven critical Ready tasks waited four hours on a rate-limited agent while
    another agent sat idle, because stealing excluded critical work."""
    from datetime import timedelta
    srv, board, clock = make(cfg, git_repo, tmp_path)
    now = utcnow()
    board.upsert_agent(AgentRow(name="claude-a", status="idle", last_heartbeat=now))
    board.upsert_agent(AgentRow(name="codex-a", status="cooldown", last_heartbeat=now, cooldown_until=now + timedelta(hours=1)))
    t = board.create_task(Task(id="", title="gate", status=Status.READY, agent="codex-a", type="backend", importance="critical"))
    assert srv.reroute() + srv.rebalance() >= 1
    assert board.get_task(t.id).agent == "claude-a"


def test_accept_as_is_answer_to_a_reviewer_escalation_merges(cfg, git_repo, tmp_path):
    """Oct 5 2026: 'accept as is' on T-010's escalation sent the task back to the worker for a 4th attempt."""
    srv, board, clock = make(cfg, git_repo, tmp_path)
    t = board.create_task(Task(id="", title="dfn", status=Status.BLOCKED, agent="claude-a", pr_url="https://x/pull/1"))
    board.create_question(Question(id="", text=f"Reviewer escalated {t.id}: criterion not met", kind="blocking",
                                   task_id=t.id, options=["cut", "split", "accept as is", "human fix"],
                                   answer="accept as is"))
    assert srv.relay() == 1
    assert board.get_task(t.id).status is Status.MERGE_READY


def test_rebalance_never_moves_a_host_pinned_task_off_its_host(cfg, git_repo, tmp_path):
    srv, board, clock = make(cfg, git_repo, tmp_path)
    board.upsert_agent(AgentRow(name="fake-b", status="idle", last_heartbeat=utcnow()))
    for i in range(3):
        board.create_task(Task(id="", title=f"q{i}", status=Status.READY, agent="codex-a", type="ml_audio",
                               flags=["host:host-a"]))
    board.create_task(Task(id="", title="busy", status=Status.RUNNING, agent="codex-a", type="ml_audio"))
    srv.rebalance()
    assert all(board.get_task(f"T-00{i}").agent != "fake-b" for i in (1, 2, 3))


def test_reap_trusts_no_heartbeat_during_a_board_outage(cfg, git_repo, tmp_path):
    """A stale heartbeat while serve itself cannot reach Notion is the network, not a dead worker (T-061, Oct 6)."""
    from swarm.board.notion import NotionError
    srv, board, clock = make(cfg, git_repo, tmp_path)
    now = clock["now"]
    board.upsert_agent(AgentRow(name="claude-a", last_heartbeat=now - timedelta(minutes=20), current_task="T-001"))
    t1 = board.create_task(Task(id="", title="stale", status=Status.RUNNING, agent="claude-a"))

    def boom() -> int:
        raise NotionError(0, "transport", "nodename nor servname provided")
    srv._step({}, "lock", boom)
    assert srv.reap() == 0 and board.get_task(t1.id).status is Status.RUNNING
    clock["now"] = now + timedelta(minutes=cfg.heartbeat_stale_minutes + 1)
    board.upsert_agent(AgentRow(name="claude-a", last_heartbeat=now - timedelta(minutes=20), current_task="T-001"))
    assert srv.reap() == 1 and board.get_task(t1.id).status is Status.READY


def test_an_agent_back_with_an_exhausted_plan_gets_no_tasks(cfg, git_repo, tmp_path):
    """Oct 6 2026: codex-b's heartbeat returned while its plan was used up; redistribute_on_return and reroute sent
    two Ready tasks back to it and both failed the same way."""
    srv, board, clock = make(cfg, git_repo, tmp_path)
    now = clock["now"]
    board.upsert_agent(AgentRow(name="claude-a", status="idle", last_heartbeat=now))
    board.upsert_agent(AgentRow(name="codex-a", status="offline", last_heartbeat=now - timedelta(minutes=30)))
    srv.redistribute_on_return()
    for i in range(3):
        board.create_task(Task(id="", title=f"b{i}", status=Status.READY, agent="claude-a", type="backend"))
    crit = board.create_task(Task(id="", title="c", status=Status.READY, agent="codex-a", type="backend",
                                  importance="critical"))
    board.upsert_agent(AgentRow(name="codex-a", status="cooldown", last_heartbeat=now,
                                cooldown_until=now + timedelta(hours=3), note="usage limit until 10:03 UTC"))
    srv.redistribute_on_return()
    assert all(board.get_task(f"T-00{i}").agent == "claude-a" for i in (1, 2, 3))
    assert board.get_task(crit.id).agent == "claude-a"     # critical work does not wait on an exhausted plan
    # reroute: critical work normally waits for a briefly rate-limited strongest agent when nobody is idle,
    # but never for a used-up plan
    c = board.get_task(crit.id); c.agent = "codex-a"; board.update_task(c, ["agent"])
    board.upsert_agent(AgentRow(name="claude-a", status="running", last_heartbeat=now))
    board.create_task(Task(id="", title="busy", status=Status.RUNNING, agent="claude-a", type="backend"))
    assert srv.reroute() >= 1 and board.get_task(crit.id).agent == "claude-a"


def test_reap_warns_once_about_a_heartbeat_from_the_future(cfg, git_repo, tmp_path):
    logs = []
    srv, board, clock = make(cfg, git_repo, tmp_path)
    srv.log = logs.append
    now = clock["now"]
    board.upsert_agent(AgentRow(name="codex-b", last_heartbeat=now + timedelta(hours=4), current_task=""))
    srv.reap(); srv.reap()
    assert sum("ahead of this clock" in m for m in logs) == 1


# ----- field note 85: routing only moves queued work (Oct 6 2026, T-095/T-102) -----

def _both_cooling(board, now, minutes=40):
    for name in ("claude-a", "codex-a"):
        board.upsert_agent(AgentRow(name=name, status="cooldown", last_heartbeat=now,
                                    cooldown_until=now + timedelta(minutes=minutes)))
    board.upsert_agent(AgentRow(name="fake-b", status="offline", last_heartbeat=now - timedelta(hours=2)))


def test_routing_never_reassigns_or_restatuses_reviewed_work(cfg, git_repo, tmp_path):
    """T-095 (approved, PR #88) and T-102 (Review, PR #93) were moved between agents and ended Ready."""
    srv, board, clock = make(cfg, git_repo, tmp_path)
    now = clock["now"]
    board.upsert_agent(AgentRow(name="claude-a", status="idle", last_heartbeat=now))
    board.upsert_agent(AgentRow(name="codex-a", status="offline", last_heartbeat=now - timedelta(hours=1)))
    board.upsert_agent(AgentRow(name="fake-b", status="idle", last_heartbeat=now))
    srv.redistribute_on_return()    # first sighting
    frozen = {}
    for st in (Status.REVIEW, Status.MERGE_READY, Status.BLOCKED, Status.DONE, Status.RUNNING):
        t = board.create_task(Task(id="", title=st.value, status=st, agent="codex-a", type="backend",
                                   pr_url="https://x/pull/88", claim_nonce="n" if st is Status.RUNNING else ""))
        frozen[t.id] = (st, "codex-a")
    # a Changes Requested row a run has claimed (nonce still set) is not routing's either
    held = board.create_task(Task(id="", title="held", status=Status.CHANGES_REQUESTED, agent="codex-a",
                                  type="backend", pr_url="https://x/pull/93", claim_nonce="live"))
    frozen[held.id] = (Status.CHANGES_REQUESTED, "codex-a")
    for _ in range(3):
        srv.reroute()
        srv.rebalance()
    board.upsert_agent(AgentRow(name="codex-a", status="idle", last_heartbeat=now))
    srv.redistribute_on_return()
    for tid, (st, agent) in frozen.items():
        got = board.get_task(tid)
        assert (got.status, got.agent) == (st, agent), tid


def test_reroute_moves_changes_requested_off_a_dead_agent_but_keeps_its_status(cfg, git_repo, tmp_path):
    srv, board, clock = make(cfg, git_repo, tmp_path)
    board.upsert_agent(AgentRow(name="claude-a", status="idle", last_heartbeat=utcnow()))
    board.upsert_agent(AgentRow(name="codex-a", status="offline"))
    t = board.create_task(Task(id="", title="cr", status=Status.CHANGES_REQUESTED, agent="codex-a", type="backend",
                               pr_url="https://x/pull/88", feedback="merge conflict in scripts/demo_regress.py"))
    assert srv.reroute() == 1
    got = board.get_task(t.id)
    assert got.agent == "claude-a" and got.status is Status.CHANGES_REQUESTED and got.feedback.startswith("merge")


def test_routing_rereads_the_row_before_writing(cfg, git_repo, tmp_path):
    """A listing can be minutes old: the reviewer or a runner may have moved the task since."""
    srv, board, clock = make(cfg, git_repo, tmp_path)
    now = clock["now"]
    board.upsert_agent(AgentRow(name="claude-a", status="idle", last_heartbeat=now))
    board.upsert_agent(AgentRow(name="codex-a", status="offline", last_heartbeat=now - timedelta(hours=1)))
    t = board.create_task(Task(id="", title="r", status=Status.READY, agent="codex-a", type="backend"))
    stale = board.list_tasks()
    real = board.list_tasks
    board.list_tasks = lambda **kw: [x for x in stale if kw.get("status") is None or x.status in kw["status"]]
    row = board.tasks[t.id]
    row.status = Status.REVIEW            # moved to Review after the listing was taken
    assert srv.reroute() == 0
    board.list_tasks = real
    assert board.get_task(t.id).status is Status.REVIEW and board.get_task(t.id).agent == "codex-a"


def test_reroute_does_not_ping_pong_between_cooling_agents(cfg, git_repo, tmp_path):
    """Oct 6 2026: with both Claude accounts limited, T-095/T-102 swapped claude-a <-> claude-a2 every tick."""
    logs = []
    srv, board, clock = make(cfg, git_repo, tmp_path)
    srv.log = logs.append
    now = clock["now"]
    _both_cooling(board, now)
    t = board.create_task(Task(id="", title="q", status=Status.READY, agent="claude-a", type="backend"))
    cr = board.create_task(Task(id="", title="c", status=Status.CHANGES_REQUESTED, agent="codex-a", type="backend"))
    for _ in range(4):
        assert srv.reroute() == 0
        assert srv.rebalance() == 0
    assert board.get_task(t.id).agent == "claude-a" and board.get_task(cr.id).agent == "codex-a"
    assert board.get_task(cr.id).status is Status.CHANGES_REQUESTED
    waits = [m for m in logs if "all agents cooling" in m]
    until = (now + timedelta(minutes=40)).strftime("%H:%M")
    assert len(waits) == 2 and f"{t.id} waits on claude-a until {until} UTC" in waits[0]
    # once an agent is free again the task moves at once
    board.upsert_agent(AgentRow(name="codex-a", status="idle", last_heartbeat=now))
    assert srv.reroute() == 1 and board.get_task(t.id).agent == "codex-a"


def test_redistribute_on_return_never_moves_work_onto_a_cooling_agent(cfg, git_repo, tmp_path):
    """Oct 6 2026: codex-b came back and redistribute sent T-095/T-102 to claude-a, which was usage-limited."""
    srv, board, clock = make(cfg, git_repo, tmp_path)
    now = clock["now"]
    _both_cooling(board, now)
    srv.redistribute_on_return()
    t = board.create_task(Task(id="", title="q", status=Status.READY, agent="claude-a", type="backend"))
    board.upsert_agent(AgentRow(name="fake-b", status="cooldown", last_heartbeat=now,
                                cooldown_until=now + timedelta(minutes=5)))    # back online, but limited
    assert srv.redistribute_on_return() == 0
    assert board.get_task(t.id).agent == "claude-a"


def test_serve_says_once_that_an_edited_config_needs_a_serve_restart(cfg):
    from swarm.board.memory import InMemoryBoard
    from swarm.serve import Server
    logs = []
    srv = Server(cfg, InMemoryBoard(), ws=None, reviewer=None, merger=None, log=logs.append)
    srv.warn_config_changed()
    assert not logs
    (cfg.path.parent / "tuning.yaml").write_text("skills_by_type: {backend: [x]}\n")
    srv.warn_config_changed()
    srv.warn_config_changed()
    assert len(logs) == 1 and "restart serve" in logs[0]


def test_reap_gives_heartbeats_a_fresh_window_after_this_machine_slept(cfg, git_repo, tmp_path):
    """Oct 6 23:05-23:46: the lid was closed on battery and the laptop slept. On waking, serve's first tick saw
    claude-a's 41-minute-old heartbeat and reaped T-115 attempt 4 mid-measurement, though its runner (same laptop)
    was alive and beat seconds later."""
    logs = []
    srv, board, clock = make(cfg, git_repo, tmp_path)
    srv.log = logs.append
    mono = {"t": 1000.0}
    srv.monotonic = lambda: mono["t"]
    start = clock["now"]
    board.upsert_agent(AgentRow(name="claude-a", status="running", last_heartbeat=start, current_task="T-001"))
    t1 = board.create_task(Task(id="", title="measuring", status=Status.RUNNING, agent="claude-a", started=start))
    assert srv.reap() == 0
    clock["now"] = start + timedelta(minutes=41)      # asleep: the wall clock moved, the monotonic one did not
    mono["t"] += 2
    assert srv.reap() == 0 and board.get_task(t1.id).status is Status.RUNNING
    assert any("asleep" in m for m in logs)
    assert board.get_agent("claude-a").status == "running"   # not marked offline either
    # the runner beats after waking: nothing happens
    clock["now"] += timedelta(seconds=30); mono["t"] += 30
    board.upsert_agent(AgentRow(name="claude-a", status="running", last_heartbeat=clock["now"], current_task="T-001"))
    assert srv.reap() == 0
    # a runner that really died during the sleep is reaped one stale window after the wake
    board.upsert_agent(AgentRow(name="claude-a", status="running", last_heartbeat=start, current_task="T-001"))
    step = timedelta(minutes=cfg.heartbeat_stale_minutes + 1)
    clock["now"] += step; mono["t"] += step.total_seconds()
    assert srv.reap() == 1 and board.get_task(t1.id).status is Status.READY


def test_pinned_task_never_leaves_its_host_when_its_agents_are_offline_or_unknown(cfg, git_repo, tmp_path):
    """T-216/T-217 (Oct 10): laptop-b tasks went to laptop-a agents while laptop-b was offline."""
    srv, board, clock = make(cfg, git_repo, tmp_path)
    now = clock["now"]
    for name in ("claude-a", "codex-a"):
        board.upsert_agent(AgentRow(name=name, status="idle", last_heartbeat=now))
    board.upsert_agent(AgentRow(name="fake-b", status="offline", last_heartbeat=now - timedelta(hours=2)))
    unknown = board.create_task(Task(id="", title="u", status=Status.READY, agent="codex-sol-b", type="frontend",
                                     flags=["host:host-b"]))
    offline = board.create_task(Task(id="", title="o", status=Status.READY, agent="fake-b", type="frontend",
                                     flags=["host:host-b"]))
    gone = board.create_task(Task(id="", title="g", status=Status.READY, agent="codex-sol-b", type="frontend",
                                  flags=["host:host-c"]))     # a host this config has no agent for
    backlog = board.create_task(Task(id="", title="b", status=Status.BACKLOG, agent="fake-b", type="frontend",
                                     flags=["host:host-b"]))
    for _ in range(2):
        srv.reroute()
        srv.rebalance()
        srv.redistribute_on_return()
    srv.promote()
    hosts = {n: a.host for n, a in cfg.agents.items()}
    for t in (unknown, offline, backlog):
        assert hosts.get(board.get_task(t.id).agent, "host-b") == "host-b", board.get_task(t.id).agent
    assert board.get_task(gone.id).agent == "codex-sol-b"
    assert board.get_task(backlog.id).status is Status.READY


def test_a_failure_on_the_account_refunds_the_attempt_and_cools_the_agent(cfg, git_repo, tmp_path):
    """Oct 10 2026: laptop-b's old runner counted codex-b's lost login (401) as failed attempts on T-297..T-311."""
    from swarm.models import auth_lost, usage_limited
    srv, board, clock = make(cfg, git_repo, tmp_path)
    now = clock["now"]
    board.upsert_agent(AgentRow(name="codex-a", status="idle", last_heartbeat=now))
    board.upsert_agent(AgentRow(name="claude-a", status="idle", last_heartbeat=now))
    t = board.create_task(Task(id="", title="f", type="backend", status=Status.FAILED, attempts=2, agent="codex-a",
                               model="gpt-6-sol", last_error="Report missing and no files changed. CLI: unexpected "
                               "status 401 Unauthorized: Missing bearer or basic authentication in header"))
    assert srv.retry_failed() == 1
    s = board.get_task(t.id)
    assert s.status is Status.READY and s.attempts == 1 and "resume" in s.flags and s.agent == "claude-a"
    assert s.last_error.startswith("auth lost:") and s.importance == "normal"
    assert auth_lost(board.get_agent("codex-a"), now)
    # a used-up plan the same way, on an agent with no limit yet
    board.upsert_agent(AgentRow(name="claude-a", status="idle", last_heartbeat=now))
    t2 = board.create_task(Task(id="", title="g", type="backend", status=Status.FAILED, attempts=1, agent="claude-a",
                                last_error="Report missing and no files changed. CLI: You’ve hit your usage limit."))
    srv.retry_failed()
    assert board.get_task(t2.id).attempts == 0 and usage_limited(board.get_agent("claude-a"), now)
    # verify output that mentions an invalid API key is the task's failure, not the account's
    t3 = board.create_task(Task(id="", title="h", type="backend", status=Status.FAILED, attempts=1, agent="codex-a",
                                last_error="verify failed: elevenlabs invalid_api_key in test_tts"))
    srv.retry_failed()
    assert board.get_task(t3.id).attempts == 1


def test_an_old_runners_short_rate_limit_on_a_used_up_plan_becomes_a_usage_limit(cfg, git_repo, tmp_path):
    from swarm.models import usage_limited
    srv, board, clock = make(cfg, git_repo, tmp_path)
    now = clock["now"]
    board.upsert_agent(AgentRow(name="codex-a", status="cooldown", last_heartbeat=now,
                                cooldown_until=now + timedelta(minutes=14), note="rate limited at 2026-10-10T04:35+00:00"))
    board.create_task(Task(id="", title="f", status=Status.READY, agent="codex-a", last_error=(
        "rate limited: You’ve hit your usage limit. Upgrade to Pro (https://chatgpt.com/explore/pro), visit "
        "https://chatgpt.com/settings/usage to purchase more credits or try again in 2 hours 10 minutes.")))
    assert srv.upgrade_limits() == 1
    row = board.get_agent("codex-a")
    assert usage_limited(row, now) and row.cooldown_until == now + timedelta(hours=2, minutes=10)
    assert srv.upgrade_limits() == 0          # once


def test_reap_honours_a_per_agent_heartbeat_window(cfg, git_repo, tmp_path):
    # muse-b (Oct 10) writes its own row about every 17 min: the project's 10-min window kept flipping it offline
    cfg.agents["codex-a"].heartbeat_stale_minutes = 30
    srv, board, clock = make(cfg, git_repo, tmp_path)
    now = clock["now"]
    board.upsert_agent(AgentRow(name="codex-a", status="running", last_heartbeat=now - timedelta(minutes=17),
                                current_task="T-001"))
    board.upsert_agent(AgentRow(name="claude-a", status="idle", last_heartbeat=now - timedelta(minutes=17)))
    t = board.create_task(Task(id="", title="slow beat", status=Status.RUNNING, agent="codex-a"))
    srv.reap()
    assert board.get_agent("codex-a").status == "running"            # inside its own 30-min window
    assert board.get_task(t.id).status is Status.RUNNING
    assert board.get_agent("claude-a").status == "offline"           # project default still applies to others
    clock["now"] += timedelta(minutes=15)
    srv.reap()
    assert board.get_agent("codex-a").status == "offline"


# ----- host inference for tasks made by agents on other hosts (T-398, T-400, T-406, Oct 10 2026) -----
def _cfg_with_muse(project_dir, sample_config_dict):
    import yaml
    from swarm.config import load_config
    sample_config_dict["hosts"]["muse-cloud"] = {"max_parallel": {"generic": 1}}
    sample_config_dict["agents"]["muse-b"] = {
        "provider": "generic", "host": "muse-cloud", "task_types": ["research", "docs", "eval"],
        "models": {"best": "muse", "high": "muse", "mid": "muse", "low": "muse"},
        "command_template": "echo no >&2; exit 1"}
    (project_dir / ".swarm" / "config.yaml").write_text(yaml.safe_dump(sample_config_dict))
    return load_config(project_dir / ".swarm" / "config.yaml")


def test_infer_host_from_agent_title_and_never_running(project_dir, sample_config_dict, git_repo, tmp_path):
    cfg = _cfg_with_muse(project_dir, sample_config_dict)
    srv, board, _ = make(cfg, git_repo, tmp_path)
    board.create_task(Task(id="T-001", title="plain", status=Status.READY, agent="fake-b"))
    board.create_task(Task(id="T-002", title="[muse-b] survey", type="research", status=Status.READY, agent="claude-a"))
    board.create_task(Task(id="T-003", title="Muse B: summarise", type="docs", status=Status.BACKLOG))
    board.create_task(Task(id="T-004", title="[muse-b/x] run", type="eval", status=Status.RUNNING, agent="claude-a"))
    board.create_task(Task(id="T-005", title="[muse-b] pinned", type="research", status=Status.READY, agent="claude-a",
                           flags=["host:laptop-a"]))
    board.create_task(Task(id="T-006", title="nobody", status=Status.READY))
    board.create_task(Task(id="T-007", title="[muse-b] code", type="backend", status=Status.READY))
    assert srv.infer_hosts() == 4
    assert board.get_task("T-001").pinned_host == "host-b"
    t2 = board.get_task("T-002")
    assert (t2.pinned_host, t2.agent, t2.model) == ("muse-cloud", "muse-b", "muse")
    t3 = board.get_task("T-003")
    assert (t3.pinned_host, t3.agent) == ("muse-cloud", "muse-b")
    assert board.get_task("T-004").pinned_host == ""      # Running: untouched
    assert board.get_task("T-005").pinned_host == "laptop-a"
    assert board.get_task("T-006").pinned_host == ""
    t7 = board.get_task("T-007")                          # muse-b may not take backend: host pinned, agent not set
    assert (t7.pinned_host, t7.agent) == ("muse-cloud", None)
    assert srv.infer_hosts() == 0                         # idempotent


# ----- laptop-c's orchestrator: Ahmad tasks without a host flag, and probe tasks (Oct 10 2026) -----
def _cfg_with_c(project_dir, sample_config_dict):
    import yaml
    from swarm.config import load_config
    sample_config_dict["hosts"]["laptop-c"] = {"max_parallel": {"generic": 1, "codex": 1}}
    base = {"host": "laptop-c", "models": {"best": "m", "high": "m", "mid": "m", "low": "m"},
            "command_template": "echo no >&2; exit 1"}
    sample_config_dict["agents"]["muse-c"] = dict(base, provider="generic", task_types=["research", "docs", "eval"])
    sample_config_dict["agents"]["codex-c"] = dict(sample_config_dict["agents"]["codex-a"], host="laptop-c")
    (project_dir / ".swarm" / "config.yaml").write_text(yaml.safe_dump(sample_config_dict))
    return load_config(project_dir / ".swarm" / "config.yaml")


def test_infer_host_for_ahmad_titles_and_cut_probes(project_dir, sample_config_dict, git_repo, tmp_path):
    cfg = _cfg_with_c(project_dir, sample_config_dict)
    srv, board, _ = make(cfg, git_repo, tmp_path)
    board.create_task(Task(id="T-001", title="URGENT from Ahmad: survey", type="research", status=Status.READY, agent="claude-a"))
    board.create_task(Task(id="T-002", title="Ahmad: fix the bridge", type="backend", status=Status.READY, agent="claude-a"))
    board.create_task(Task(id="T-003", title="Run it via muse-c", type="docs", status=Status.BACKLOG))
    board.create_task(Task(id="T-004", title="probe-ping", status=Status.READY, agent="claude-a"))
    board.create_task(Task(id="T-005", title="Ahmad: running", type="docs", status=Status.RUNNING, agent="claude-a"))
    board.create_task(Task(id="T-006", title="Ahmad: pinned", type="docs", status=Status.READY, agent="claude-a",
                           flags=["host:laptop-a"]))
    assert srv.infer_hosts() == 4
    t1 = board.get_task("T-001")
    assert (t1.pinned_host, t1.agent) == ("laptop-c", "muse-c")
    t2 = board.get_task("T-002")
    assert (t2.pinned_host, t2.agent) == ("laptop-c", "codex-c")     # muse-c may not take backend
    t3 = board.get_task("T-003")
    assert (t3.pinned_host, t3.agent) == ("laptop-c", "muse-c")
    assert board.get_task("T-004").status is Status.CUT
    assert board.get_task("T-005").pinned_host == ""
    assert board.get_task("T-006").pinned_host == "laptop-a"
    assert srv.infer_hosts() == 0
