import json
import subprocess
from datetime import timedelta

from swarm.board.memory import InMemoryBoard
from swarm.models import AgentRow, RunResult, Status, Task, Usage, utcnow
from swarm.policy import glob_match, in_scope, needs_review
from swarm.runner import Runner, SyncExecutor
from swarm.usage import Ledger
from swarm.workspace import CmdResult, Workspace


class FakeAdapter:
    """Writes files into cwd and returns a canned RunResult."""

    def __init__(self, files=None, structured=None, ok=True, timed_out=False, rate_limited=False,
                 report_file=None):
        self.files = files or {}
        self.structured = structured
        self.ok = ok
        self.timed_out = timed_out
        self.rate_limited = rate_limited
        self.report_file = report_file
        self.specs = []
        self.prompts = []

    def run(self, spec):
        self.specs.append(spec)
        self.prompts.append(spec.prompt_file.read_text())
        for rel, content in self.files.items():
            p = spec.cwd / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content)
        if self.report_file is not None:
            (spec.cwd / ".swarm-run" / "report.json").write_text(json.dumps(self.report_file))
        return RunResult(ok=self.ok, exit_code=0 if self.ok else 1, stdout="", stderr="",
                         structured_output=self.structured, usage=Usage(10, 5, 0.25),
                         timed_out=self.timed_out, rate_limited=self.rate_limited,
                         error="" if self.ok else "boom")


def fake_gh(args, cwd):
    if args[:2] == ["pr", "view"]:
        return CmdResult(1, "", "none")
    if args[:2] == ["pr", "create"]:
        return CmdResult(0, "https://gh/pr/1\n", "")
    return CmdResult(0, "", "")


def make_runner(cfg, git_repo, tmp_path, adapter, host="host-a"):
    board = InMemoryBoard()
    ws = Workspace(git_repo, tmp_path / "wt", gh=fake_gh)
    ledger = Ledger(tmp_path / "usage.jsonl")
    r = Runner(cfg, board, host, ws, ledger=ledger, adapter_factory=lambda a: adapter,
               sleep=lambda s: None, executor=SyncExecutor(), rules_text="RULES", log=lambda *a: None,
               log_dir=tmp_path / "logs")
    return r, board


def ready_task(board, **kw):
    base = dict(id="", title="Thing", type="backend", importance="normal", size="S", status=Status.READY,
                agent="codex-a", model="gpt-6-sol", effort="medium", scope=["src/**"])
    base.update(kw)
    return board.create_task(Task(**base))


def _break_verify(git_repo, body):
    (git_repo / "scripts" / "verify_fast.sh").write_text(body)
    subprocess.run(["git", "-C", str(git_repo), "commit", "-qam", "change verify"], check=True)
    subprocess.run(["git", "-C", str(git_repo), "push", "-q", "origin", "main"], check=True)


def test_policy_and_globs(cfg):
    assert glob_match("src/a/b.py", "src/**") and glob_match("src/x.py", "src/*.py")
    assert not glob_match("docs/a.md", "src/**") and not glob_match("src/a/b.py", "src/*.py")
    assert in_scope("src/a.py", ["src/**"]) and in_scope("anything", [])
    t = Task(id="T", title="", importance="normal")
    assert needs_review(t, cfg) is False
    assert needs_review(Task(id="T", title="", importance="high"), cfg) is True
    assert needs_review(Task(id="T", title="", importance="low", flags=["out_of_scope"]), cfg) is True
    cfg.review_policy = "all"
    assert needs_review(t, cfg) is True
    cfg.review_policy = "none"
    assert needs_review(Task(id="T", title="", importance="critical"), cfg) is False


def test_done_normal_goes_merge_ready(cfg, git_repo, tmp_path):
    adapter = FakeAdapter(files={"src/a.py": "print(1)\n"},
                          structured={"status": "done", "summary": "added a", "files_changed": ["src/a.py"],
                                      "decisions": [{"decision": "use print", "why": "simple", "impact": "low"}]})
    r, board = make_runner(cfg, git_repo, tmp_path, adapter)
    t = ready_task(board)
    out = r.run_task(t)
    stored = board.get_task(t.id)
    assert out.status is Status.MERGE_READY and stored.status is Status.MERGE_READY
    assert stored.pr_url == "https://gh/pr/1" and stored.attempts == 1 and stored.claim_nonce == ""
    assert board.reports[t.id][0][0] == "Report — attempt 1"
    assert adapter.specs[0].model == "gpt-6-sol" and adapter.specs[0].max_turns == 30
    assert "RULES" in adapter.prompts[0]
    assert not r.ws.worktree_path(t.id).exists()
    assert r.ledger.totals("codex-a").cost_usd == 0.25
    assert (tmp_path / "logs" / t.id / "attempt-1" / "prompt.md").exists()
    # decisions log landed on the branch
    wt = r.ws.provision(t.id, reuse_branch=True)
    assert (wt / "docs" / "decisions" / f"{t.id}.md").exists()
    r.ws.dispose(wt)


def test_high_importance_goes_review(cfg, git_repo, tmp_path):
    adapter = FakeAdapter(files={"src/a.py": "x"}, structured={"status": "done", "summary": "s"})
    r, board = make_runner(cfg, git_repo, tmp_path, adapter)
    t = ready_task(board, importance="high")
    assert r.run_task(t).status is Status.REVIEW


def test_out_of_scope_forces_review(cfg, git_repo, tmp_path):
    adapter = FakeAdapter(files={"other/a.py": "x"}, structured={"status": "done", "summary": "s"})
    r, board = make_runner(cfg, git_repo, tmp_path, adapter)
    t = ready_task(board)
    out = r.run_task(t)
    assert out.status is Status.REVIEW and "out_of_scope" in board.get_task(t.id).flags


def test_blocked_creates_question(cfg, git_repo, tmp_path):
    adapter = FakeAdapter(files={"src/a.py": "x"},
                          structured={"status": "blocked", "summary": "need db choice",
                                      "question": {"kind": "blocking", "text": "sqlite or pg?",
                                                   "options": ["sqlite", "pg"]}})
    r, board = make_runner(cfg, git_repo, tmp_path, adapter)
    t = ready_task(board)
    assert r.run_task(t).status is Status.BLOCKED
    q = board.list_questions(status="Open")[0]
    assert q.text == "sqlite or pg?" and q.task_id == t.id and q.asked_by == "codex-a" and q.kind == "blocking"


def test_fyi_question_keeps_going(cfg, git_repo, tmp_path):
    adapter = FakeAdapter(files={"src/a.py": "x"},
                          structured={"status": "done", "summary": "s",
                                      "question": {"kind": "fyi", "text": "chose 3s", "proceeding_with": "3s"}})
    r, board = make_runner(cfg, git_repo, tmp_path, adapter)
    t = ready_task(board)
    assert r.run_task(t).status is Status.MERGE_READY
    assert board.list_questions()[0].kind == "fyi"


def test_done_without_changes_is_failed(cfg, git_repo, tmp_path):
    adapter = FakeAdapter(files={}, structured={"status": "done", "summary": "claims done"})
    r, board = make_runner(cfg, git_repo, tmp_path, adapter)
    t = ready_task(board)
    assert r.run_task(t).status is Status.FAILED
    assert board.get_task(t.id).last_error == "no changes"


def test_verify_failure_goes_changes_requested(cfg, git_repo, tmp_path):
    _break_verify(git_repo, "#!/bin/sh\necho 'FAIL 1/3'; exit 1\n")
    adapter = FakeAdapter(files={"src/a.py": "x"}, structured={"status": "done", "summary": "s"})
    r, board = make_runner(cfg, git_repo, tmp_path, adapter)
    t = ready_task(board)
    out = r.run_task(t)
    stored = board.get_task(t.id)
    assert out.status is Status.CHANGES_REQUESTED and "FAIL 1/3" in stored.feedback
    assert stored.review_rounds == 1 and "resume" in stored.flags


def test_verify_failure_beyond_rounds_blocks(cfg, git_repo, tmp_path):
    _break_verify(git_repo, "#!/bin/sh\nexit 1\n")
    adapter = FakeAdapter(files={"src/a.py": "x"}, structured={"status": "done", "summary": "s"})
    r, board = make_runner(cfg, git_repo, tmp_path, adapter)
    t = ready_task(board, review_rounds=2, status=Status.CHANGES_REQUESTED)
    assert r.run_task(t).status is Status.BLOCKED
    assert board.list_questions(status="Open")[0].kind == "blocking"


def test_rate_limit_returns_to_ready_and_cools_down(cfg, git_repo, tmp_path):
    adapter = FakeAdapter(files={}, ok=False, rate_limited=True)
    r, board = make_runner(cfg, git_repo, tmp_path, adapter)
    t = ready_task(board)
    out = r.run_task(t)
    stored = board.get_task(t.id)
    assert out.status is Status.READY and stored.attempts == 0 and stored.claim_nonce == ""
    row = board.get_agent("codex-a")
    assert row.status == "cooldown" and row.cooldown_until > utcnow() + timedelta(minutes=10)


def test_timeout_without_changes_is_failed(cfg, git_repo, tmp_path):
    adapter = FakeAdapter(files={}, ok=False, timed_out=True)
    r, board = make_runner(cfg, git_repo, tmp_path, adapter)
    t = ready_task(board)
    assert r.run_task(t).status is Status.FAILED
    assert "timeout" in board.get_task(t.id).flags


def test_abnormal_end_with_changes_resumes(cfg, git_repo, tmp_path):
    adapter = FakeAdapter(files={"src/a.py": "half"}, ok=False, structured=None)
    r, board = make_runner(cfg, git_repo, tmp_path, adapter)
    t = ready_task(board)
    out = r.run_task(t)
    stored = board.get_task(t.id)
    assert out.status is Status.CHANGES_REQUESTED and "resume" in stored.flags
    assert "report_missing" in stored.flags and "ended with" in stored.feedback


def test_report_from_file_for_generic(cfg, git_repo, tmp_path):
    adapter = FakeAdapter(files={"src/a.py": "x"}, report_file={"status": "done", "summary": "via file"})
    r, board = make_runner(cfg, git_repo, tmp_path, adapter, host="host-b")
    t = ready_task(board, agent="fake-b", model="x", effort=None)
    assert r.run_task(t).status is Status.MERGE_READY
    assert adapter.specs[0].schema is None
    assert ".swarm-run/report.json" in adapter.prompts[0]


def test_tick_respects_slots_and_skips_unknown_agent(cfg, git_repo, tmp_path):
    adapter = FakeAdapter(files={"src/a.py": "x"}, structured={"status": "done", "summary": "s"})
    r, board = make_runner(cfg, git_repo, tmp_path, adapter)
    for i in range(4):
        ready_task(board, title=f"t{i}", agent="codex-a")
    ready_task(board, title="ghost", agent="not-configured")
    ready_task(board, title="other host", agent="fake-b")
    n = r.tick()
    assert n == 4  # SyncExecutor runs each immediately; the slot frees after each run
    assert board.get_task("T-005").status is Status.READY and board.get_task("T-006").status is Status.READY
    rows = {a.name for a in board.list_agents()}
    assert {"claude-a", "codex-a"} <= rows


def test_heartbeat_writes_rows(cfg, git_repo, tmp_path):
    adapter = FakeAdapter()
    r, board = make_runner(cfg, git_repo, tmp_path, adapter)
    r.heartbeat(force=True)
    row = board.get_agent("claude-a")
    assert row.host == "host-a" and row.status == "idle" and row.last_heartbeat is not None


def test_cooldown_gates_dispatch_for_every_importance(cfg, git_repo, tmp_path):
    adapter = FakeAdapter(files={"src/a.py": "x"}, structured={"status": "done", "summary": "s"})
    r, board = make_runner(cfg, git_repo, tmp_path, adapter)
    board.upsert_agent(AgentRow(name="codex-a", status="cooldown", cooldown_until=utcnow() + timedelta(minutes=5)))
    ready_task(board, title="n", importance="normal")
    ready_task(board, title="c", importance="critical")
    assert r.tick() == 0
    assert board.get_task("T-001").status is Status.READY and board.get_task("T-002").status is Status.READY


def test_recover_orphans_on_start(cfg, git_repo, tmp_path):
    r, board = make_runner(cfg, git_repo, tmp_path, FakeAdapter())
    t = board.create_task(Task(id="", title="orphan", status=Status.RUNNING, agent="codex-a", claim_nonce="old"))
    other = board.create_task(Task(id="", title="other host", status=Status.RUNNING, agent="fake-b"))
    assert r.recover_orphans() == 1
    s = board.get_task(t.id)
    assert s.status is Status.READY and "resume" in s.flags and s.claim_nonce == ""
    assert board.get_task(other.id).status is Status.RUNNING


def test_push_uses_force_with_lease(cfg, git_repo, tmp_path):
    # a stale remote branch from an earlier attempt must not block a fresh-from-main push
    adapter = FakeAdapter(files={"src/a.py": "x"}, structured={"status": "done", "summary": "s"})
    r, board = make_runner(cfg, git_repo, tmp_path, adapter)
    t = ready_task(board)
    wt = r.ws.provision(t.id)
    (wt / "old.txt").write_text("old\n")
    r.ws.commit_all(wt, "old attempt")
    r.ws.push(wt, t.branch)
    r.ws.dispose(wt)
    assert r.run_task(t).status is Status.MERGE_READY
    wt2 = r.ws.provision(t.id, reuse_branch=True)
    assert not (wt2 / "old.txt").exists() and (wt2 / "src" / "a.py").exists()
    r.ws.dispose(wt2)


def test_push_failure_is_failed(cfg, git_repo, tmp_path):
    adapter = FakeAdapter(files={"src/a.py": "x"}, structured={"status": "done", "summary": "s"})
    r, board = make_runner(cfg, git_repo, tmp_path, adapter)
    r.ws.push = lambda path, branch, force_with_lease=False: CmdResult(1, "", "rejected")
    t = ready_task(board)
    assert r.run_task(t).status is Status.FAILED
    assert "push failed" in board.get_task(t.id).last_error


def test_blocked_without_question_synthesizes_one(cfg, git_repo, tmp_path):
    adapter = FakeAdapter(files={"src/a.py": "x"},
                          structured={"status": "blocked", "summary": "cannot find the API key"})
    r, board = make_runner(cfg, git_repo, tmp_path, adapter)
    t = ready_task(board)
    assert r.run_task(t).status is Status.BLOCKED
    q = board.list_questions(status="Open")[0]
    assert q.kind == "blocking" and "API key" in q.text


def test_publish_abandons_when_claim_lost(cfg, git_repo, tmp_path):
    r, board = make_runner(cfg, git_repo, tmp_path, None)
    t = ready_task(board)

    class Hijack(FakeAdapter):
        def run(self, spec):
            stolen = board.get_task(t.id)
            stolen.claim_nonce, stolen.agent = "someone-else", "claude-a"
            board.update_task(stolen, ["claim_nonce", "agent"])
            return super().run(spec)

    adapter = Hijack(files={"src/a.py": "x"}, structured={"status": "done", "summary": "s"})
    r.adapter_factory = lambda a: adapter
    r.run_task(t)
    stored = board.get_task(t.id)
    assert stored.status is Status.RUNNING and stored.claim_nonce == "someone-else" and stored.agent == "claude-a"
    assert t.id not in board.reports


def test_stopping_requeues_instead_of_publishing(cfg, git_repo, tmp_path):
    r, board = make_runner(cfg, git_repo, tmp_path, None)

    class Stopper(FakeAdapter):
        def run(self, spec):
            r.stop()
            return super().run(spec)

    adapter = Stopper(files={"src/a.py": "half"}, ok=False)
    r.adapter_factory = lambda a: adapter
    t = ready_task(board)
    out = r.run_task(t)
    s = board.get_task(t.id)
    assert out.status is Status.READY and s.status is Status.READY and "resume" in s.flags and s.claim_nonce == ""
    wt = r.ws.provision(t.id, reuse_branch=True)
    assert (wt / "src" / "a.py").exists()  # partial work was pushed for the resume
    r.ws.dispose(wt)


def test_agent_row_reflects_spend_after_a_task(cfg, git_repo, tmp_path):
    adapter = FakeAdapter(files={"src/a.py": "x"}, structured={"status": "done", "summary": "s"})
    r, board = make_runner(cfg, git_repo, tmp_path, adapter)
    t = ready_task(board)
    r.run_task(t)
    row = board.get_agent("codex-a")
    assert row is not None and row.cost_5h_usd == 0.25 and row.runs == 1
