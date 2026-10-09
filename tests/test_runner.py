import json
import subprocess
from datetime import timedelta

from swarm.board.memory import InMemoryBoard
from swarm.models import AgentRow, Report, RunResult, Status, Task, Usage, utcnow
from swarm.policy import glob_match, in_scope, needs_review
from swarm.runner import Runner, SyncExecutor
from swarm.tools import PluginInfo
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
    r.auto_update = False     # never fetch/re-exec the real harness checkout from a test
    return r, board


def ready_task(board, **kw):
    base = dict(id="", title="Thing", type="backend", importance="normal", size="S", status=Status.READY,
                agent="codex-a", model="gpt-6-sol", effort="medium", scope=["src/**"])
    base.update(kw)
    return board.create_task(Task(**base))


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout


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
    assert adapter.specs[0].model == "gpt-6-sol" and adapter.specs[0].max_turns == 60   # S=30, medium floor 60
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


def test_loop_installs_signal_handlers_that_park(cfg, git_repo, tmp_path):
    import signal
    r, board = make_runner(cfg, git_repo, tmp_path, FakeAdapter())
    captured = {}

    def fake_signal(sig, handler):
        captured[sig] = handler
        return signal.SIG_DFL

    r.install_signal_handlers(signal_fn=fake_signal)
    assert {signal.SIGINT, signal.SIGTERM} <= set(captured)      # plus SIGUSR1 = graceful restart
    captured[signal.SIGTERM](signal.SIGTERM, None)
    assert r._stopping is True


def test_harness_written_logs_are_never_out_of_scope(cfg, git_repo, tmp_path):
    """Resumed attempts see docs/decisions/<id>.md from the previous attempt; that must not force a review."""
    adapter = FakeAdapter(files={"src/a.py": "x", "docs/decisions/T-001.md": "old", "docs/debt/T-001.md": "old"},
                          structured={"status": "done", "summary": "s"})
    r, board = make_runner(cfg, git_repo, tmp_path, adapter)
    t = ready_task(board)
    out = r.run_task(t)
    assert out.status is Status.MERGE_READY and "out_of_scope" not in board.get_task(t.id).flags


def test_runner_passes_mcp_servers_by_task_type(cfg, git_repo, tmp_path):
    cfg.mcp_by_type = {"frontend": ["magic"]}
    adapter = FakeAdapter(files={"src/a.py": "x"}, structured={"status": "done", "summary": "s"})
    r, board = make_runner(cfg, git_repo, tmp_path, adapter)
    r.run_task(ready_task(board, type="frontend", agent="claude-a", model="sonnet"))
    r.run_task(ready_task(board, type="backend", agent="claude-a", model="sonnet"))
    assert adapter.specs[0].mcp == ["magic"] and adapter.specs[1].mcp == []


def test_runner_ensures_plugins_and_passes_tools_to_the_prompt(cfg, git_repo, tmp_path):
    cfg.plugins_required = ["superpowers"]
    cfg.plugins_by_type = {"frontend": ["frontend-design"]}
    cfg.skills_by_type = {"frontend": ["frontend-design"]}
    cfg.mcp_by_type = {"frontend": ["magic"]}
    cfg.mcp_by_importance = {"critical": ["context7"]}
    adapter = FakeAdapter(files={"src/a.py": "x"}, structured={"status": "done", "summary": "ok"})
    ensured = []
    r, board = make_runner(cfg, git_repo, tmp_path, adapter)
    r.ensure_plugins = lambda names, enable=True: ensured.append((list(names), enable)) or []
    r.installed_plugins = lambda: {"frontend-design": PluginInfo("frontend-design", "frontend-design@m", False,
                                                                 "/cache/fd/1"),
                                   "humanizer": PluginInfo("humanizer", "humanizer@h", True, "/cache/h/3")}
    cfg.mcp_servers = {"magic": {"type": "http", "url": "https://magic/mcp"}}
    ready_task(board, type="frontend", importance="critical", scope=["src/**"], agent="claude-a", model="sonnet")
    r.tick()
    assert ensured == [(["superpowers"], True), (["frontend-design"], False)]
    assert adapter.specs[0].plugin_dirs == ["/cache/fd/1"]
    assert adapter.specs[0].settings == {"enabledPlugins": {"humanizer@h": False}}
    assert adapter.specs[0].mcp == ["magic", "context7"]
    assert adapter.specs[0].mcp_servers == {"magic": {"type": "http", "url": "https://magic/mcp"}}
    assert "## Tools for this task" in adapter.prompts[0] and "frontend-design" in adapter.prompts[0]
    assert "claude plugin install" in adapter.prompts[0]


def test_runner_skips_plugin_setup_for_non_claude_agents(cfg, git_repo, tmp_path):
    cfg.plugins_required = ["superpowers"]
    adapter = FakeAdapter(files={"src/a.py": "x"}, structured={"status": "done", "summary": "ok"})
    ensured = []
    r, board = make_runner(cfg, git_repo, tmp_path, adapter)
    r.ensure_plugins = lambda names, enable=True: ensured.append(list(names)) or []
    ready_task(board, type="backend", agent="codex-a", scope=["src/**"])
    r.tick()
    assert ensured == [] and adapter.specs


def test_decision_log_accumulates_across_attempts(cfg, git_repo, tmp_path):
    """A resumed attempt appends its decisions; the first attempt's decisions stay on the branch."""
    adapter = FakeAdapter(files={"src/a.py": "x"}, structured={
        "status": "done", "summary": "first", "decisions": [{"decision": "use tap", "why": "simple", "impact": "low"}]})
    r, board = make_runner(cfg, git_repo, tmp_path, adapter)
    t = ready_task(board, importance="high", scope=["src/**"])
    r.run_task(t)
    stored = board.get_task(t.id)
    assert stored.status is Status.REVIEW
    stored.status, stored.feedback = Status.CHANGES_REQUESTED, "add a note"
    stored.flags = list(dict.fromkeys(stored.flags + ["resume"]))
    board.update_task(stored, ["status", "feedback", "flags"])
    adapter.structured = {"status": "done", "summary": "second",
                          "decisions": [{"decision": "keep favicon", "why": "no warnings", "impact": "low"}]}
    r.run_task(board.get_task(t.id))
    wt = r.ws.provision(t.id, reuse_branch=True)
    text = (wt / "docs" / "decisions" / f"{t.id}.md").read_text()
    r.ws.dispose(wt)
    assert "use tap" in text and "keep favicon" in text
    assert text.index("use tap") < text.index("keep favicon")


def test_abnormal_end_resumes_one_model_tier_up(cfg, git_repo, tmp_path):
    """Max turns / timeout with work left behind: the resume runs on the next tier (haiku → sonnet), not the same
    model again. Three Haiku attempts at 30 turns each is how T-013 burned $0.60 for nothing on Sep 28."""
    adapter = FakeAdapter(files={"src/a.py": "x"}, structured=None, ok=False, report_file=None)
    adapter_error = "error_max_turns"
    orig = adapter.run

    def run(spec):
        r = orig(spec)
        r.error = adapter_error
        return r
    adapter.run = run
    r, board = make_runner(cfg, git_repo, tmp_path, adapter)
    t = ready_task(board, agent="claude-a", model="haiku", effort="low", importance="low", scope=["src/**"])
    r.run_task(t)
    stored = board.get_task(t.id)
    assert stored.status is Status.CHANGES_REQUESTED and "resume" in stored.flags
    assert stored.model == "sonnet"          # one tier up (low → mid) for the same agent
    stored.status = Status.READY
    board.update_task(stored, ["status"])
    r.run_task(board.get_task(t.id))
    assert board.get_task(t.id).model == "opus"   # mid → high; never above the agent's best tier


def test_resumed_branch_gets_current_main_before_the_run(cfg, git_repo, tmp_path):
    """T-011 resumed on a branch cut before the backend merges and could not even run verify. A resumed run
    starts from current main; a rebase conflict is reported in the prompt instead of silently working on stale code."""
    adapter = FakeAdapter(files={"src/a.py": "half"}, ok=False, structured=None)
    r, board = make_runner(cfg, git_repo, tmp_path, adapter)
    t = ready_task(board)
    r.run_task(t)                                  # attempt 1 parks work on the branch (resume)
    _git(git_repo, "checkout", "-q", "main")
    (git_repo / "backend_new.py").write_text("merged meanwhile\n")
    _git(git_repo, "add", "backend_new.py"); _git(git_repo, "commit", "-qm", "main moved on"); _git(git_repo, "push", "-q", "origin", "main")
    stored = board.get_task(t.id); stored.status = Status.READY; board.update_task(stored, ["status"])
    seen = {}
    orig = adapter.run

    def run(spec):
        seen["has_new_file"] = (spec.cwd / "backend_new.py").exists()
        seen["prompt"] = spec.prompt_file.read_text()
        return orig(spec)
    adapter.run = run
    r.run_task(board.get_task(t.id))
    assert seen["has_new_file"] is True and "rebase" not in seen["prompt"].lower()
    # conflict: main now changes the same file the branch touched
    _git(git_repo, "checkout", "-q", "main")
    (git_repo / "src").mkdir(exist_ok=True); (git_repo / "src" / "a.py").write_text("main version\n")
    _git(git_repo, "add", "src/a.py"); _git(git_repo, "commit", "-qm", "conflicting"); _git(git_repo, "push", "-q", "origin", "main")
    stored = board.get_task(t.id); stored.status = Status.READY; board.update_task(stored, ["status"])
    r.run_task(board.get_task(t.id))
    assert "## Merge conflicts: resolve these first" in seen["prompt"] and "- src/a.py" in seen["prompt"]


def test_conflicting_resume_leaves_markers_and_merge_head_for_the_worker(cfg, git_repo, tmp_path):
    """Q-140/Q-144/Q-146: the Codex sandbox cannot rebase or fetch. The runner merges main before the CLI starts and
    leaves the conflict for plain edits + `git add` + `git commit`, which the sandbox allows."""
    adapter = FakeAdapter(files={"src/a.py": "branch version\n"}, ok=False, structured=None)
    r, board = make_runner(cfg, git_repo, tmp_path, adapter)
    t = ready_task(board, agent="codex-a")
    r.run_task(t)                                       # attempt 1 parks work on the branch (resume)
    _git(git_repo, "checkout", "-q", "main")
    (git_repo / "src").mkdir(exist_ok=True); (git_repo / "src" / "a.py").write_text("main version\n")
    _git(git_repo, "add", "src/a.py"); _git(git_repo, "commit", "-qm", "T-050 rewrote a.py"); _git(git_repo, "push", "-q", "origin", "main")
    stored = board.get_task(t.id); stored.status = Status.READY; board.update_task(stored, ["status"])
    seen = {}

    def run(spec):                                       # the worker resolves the markers, adds and commits
        a = spec.cwd / "src" / "a.py"
        seen["markers"] = "<<<<<<<" in a.read_text() and ">>>>>>>" in a.read_text()
        seen["merge_head"] = subprocess.run(["git", "-C", str(spec.cwd), "rev-parse", "-q", "--verify", "MERGE_HEAD"],
                                            capture_output=True).returncode == 0
        seen["prompt"] = spec.prompt_file.read_text()
        a.write_text("resolved\n")
        _git(spec.cwd, "add", "src/a.py")
        _git(spec.cwd, "-c", "user.email=w@x", "-c", "user.name=w", "commit", "-q", "--no-edit")
        return RunResult(ok=True, exit_code=0, stdout="", stderr="", structured_output={"status": "done", "summary": "ok"})
    adapter.run = run
    assert r.run_task(board.get_task(t.id)).status is Status.MERGE_READY
    assert seen["markers"] and seen["merge_head"]
    p = seen["prompt"]
    assert "- src/a.py" in p and "T-050 rewrote a.py" in p and "`git add src/a.py`" in p
    assert "Do not run `git rebase`, `git fetch`" in p
    assert _git(git_repo, "show", f"origin/task/{t.id}:src/a.py") == "resolved\n"


def test_unresolved_markers_go_back_to_the_worker_not_to_review(cfg, git_repo, tmp_path):
    """A worker that `git add`s a file without resolving it (or a sandboxed one whose commit the harness makes) must
    never send conflict markers to review; the work is kept on the branch and the task comes back."""
    body = "<<<<<<< HEAD\nmine\n=======\ntheirs\n>>>>>>> origin/main\n"
    adapter = FakeAdapter(files={"src/a.py": body}, structured={"status": "done", "summary": "ok"})
    r, board = make_runner(cfg, git_repo, tmp_path, adapter)
    t = ready_task(board)
    assert r.run_task(t).status is Status.CHANGES_REQUESTED
    stored = board.get_task(t.id)
    assert "Conflict markers are still in: src/a.py" in stored.feedback and "resume" in stored.flags
    assert "<<<<<<<" in _git(git_repo, "show", f"origin/task/{t.id}:src/a.py")   # pushed, not lost


def test_runner_merges_main_that_moved_during_the_run_instead_of_rebasing(cfg, git_repo, tmp_path):
    adapter = FakeAdapter(files={"src/a.py": "x\n"}, structured={"status": "done", "summary": "ok"})
    orig = adapter.run

    def run(spec):
        out = orig(spec)
        _git(git_repo, "checkout", "-q", "main")
        (git_repo / "src").mkdir(exist_ok=True); (git_repo / "src" / "a.py").write_text("main\n")
        _git(git_repo, "add", "src/a.py"); _git(git_repo, "commit", "-qm", "conflicting main"); _git(git_repo, "push", "-q", "origin", "main")
        return out
    adapter.run = run
    r, board = make_runner(cfg, git_repo, tmp_path, adapter)
    t = ready_task(board)
    assert r.run_task(t).status is Status.MERGE_READY      # the pre-verify conflict is aborted and left to the merger
    assert _git(git_repo, "show", f"origin/task/{t.id}:src/a.py") == "x\n"


def test_runner_arms_a_claim_watchdog_on_every_run(cfg, git_repo, tmp_path):
    """The run spec carries a should_stop that turns True once the board no longer shows this run's claim
    (the task was reaped and handed elsewhere)."""
    adapter = FakeAdapter(files={"src/a.py": "x"}, structured={"status": "done", "summary": "ok"})
    r, board = make_runner(cfg, git_repo, tmp_path, adapter)
    r.claim_check_s = 0
    t = ready_task(board)
    r.run_task(t)
    watch = adapter.specs[0].should_stop
    assert callable(watch)
    nonce = adapter.specs[0].claim_nonce
    stored = board.get_task(t.id)
    stored.status, stored.claim_nonce = Status.RUNNING, nonce
    board.update_task(stored, ["status", "claim_nonce"])
    assert watch() is False
    stored.claim_nonce = "someone-else"
    board.update_task(stored, ["claim_nonce"])
    assert watch() is True


def test_runner_lists_plugins_once_per_process(cfg, git_repo, tmp_path):
    """`claude plugin list --json` costs a second or more; three calls per task added up."""
    cfg.plugins_required = ["frontend-design"]
    adapter = FakeAdapter(files={"src/a.py": "x"}, structured={"status": "done", "summary": "ok"})
    r, board = make_runner(cfg, git_repo, tmp_path, adapter)
    calls = {"list": 0}

    def listing():
        calls["list"] += 1
        return {"frontend-design": PluginInfo("frontend-design", "frontend-design@m", True, "/cache/fd")}
    r.installed_plugins = listing
    r.ensure_plugins = lambda names, enable=True: []
    for _ in range(2):
        ready_task(board, agent="claude-a", model="sonnet", scope=["src/**"])
    r.tick(); r.tick()
    assert len(adapter.specs) == 2 and calls["list"] == 1


def test_loop_survives_board_outages_with_backoff(cfg, git_repo, tmp_path):
    """A Notion or network outage used to kill `swarm run`; overnight that means a dead agent."""
    adapter = FakeAdapter()
    r, board = make_runner(cfg, git_repo, tmp_path, adapter)
    sleeps, calls = [], {"n": 0}
    r.sleep = sleeps.append
    r.recover_orphans = lambda: None
    r.install_signal_handlers = lambda: None

    def tick():
        calls["n"] += 1
        if calls["n"] <= 3:
            raise ConnectionError("Notion unreachable")
        return 0
    r.tick = tick
    r.loop(stop=lambda: calls["n"] >= 5)
    assert calls["n"] == 5
    assert sleeps[:3] == [15, 30, 60]            # backoff while failing
    assert sleeps[3] == cfg.poll_seconds         # back to normal once it recovers


def test_runner_does_not_overwrite_a_task_closed_while_it_ran(cfg, git_repo, tmp_path):
    """A human merge or `swarm cut` during a run must win over the runner's late publish (Oct 4 2026, T-001)."""
    from swarm.runner import Runner
    from swarm.models import Status
    runner, board = _runner(cfg, git_repo, tmp_path) if "_runner" in globals() else (None, None)
    if runner is None:
        import pytest
        pytest.skip("no runner fixture helper in this module")
    t = board.create_task(Task(id="", title="x", status=Status.READY, agent="claude-a"))
    t.claim_nonce = "abc"; t.status = Status.RUNNING
    board.update_task(t, ["claim_nonce", "status"])
    closed = board.get_task(t.id); closed.status = Status.DONE; board.update_task(closed, ["status"])
    assert runner.publish_outcome(t, Status.FAILED) is False
    assert board.get_task(t.id).status is Status.DONE


def test_request_restart_drains_then_restarts(cfg, git_repo, tmp_path, monkeypatch):
    """SIGUSR1: stop claiming, let in-flight runs finish, then re-exec on the new code (never park busy work)."""
    from swarm.runner import Runner
    from swarm.board.memory import InMemoryBoard
    from swarm.workspace import Workspace
    from swarm.usage import Ledger
    board = InMemoryBoard()
    r = Runner(cfg, board, "host-a", Workspace(git_repo, tmp_path / "wt"), ledger=Ledger(tmp_path / "u.jsonl"), log=lambda *a: None)
    r.auto_update = False     # never pull the real harness checkout from a test
    restarted = []
    monkeypatch.setattr("swarm.selfupdate.restart_self", lambda: restarted.append(True))
    import os
    from swarm.runner import InFlight
    r.active["claude-a"].add("T-001")           # one run in flight, its CLI alive
    r.runs["T-001"] = InFlight("T-001", "claude-a", "S", r.now(), pid=os.getpid(), phase="cli")
    r.request_restart()
    assert r.draining and r.tick() == 0          # nothing new is claimed while draining
    assert not r.finish_drain_if_idle()           # still busy
    r.active["claude-a"].clear()
    assert r.finish_drain_if_idle() and restarted == [True]


def test_rate_limited_task_is_rerouted_away_immediately(cfg, git_repo, tmp_path):
    """Oct 5 2026: T-043 bounced between codex-b's cooldowns for an hour while two Claude agents idled."""
    from swarm.runner import Runner
    from swarm.board.memory import InMemoryBoard
    from swarm.workspace import Workspace
    from swarm.usage import Ledger
    from swarm.models import AgentRow, RunResult, Status, Task, utcnow
    board = InMemoryBoard()
    board.upsert_agent(AgentRow(name="claude-a", status="idle", last_heartbeat=utcnow()))
    board.upsert_agent(AgentRow(name="codex-a", status="running", last_heartbeat=utcnow()))
    r = Runner(cfg, board, "host-a", Workspace(git_repo, tmp_path / "wt"), ledger=Ledger(tmp_path / "u.jsonl"), log=lambda *a: None)
    t = board.create_task(Task(id="", title="bakeoff", status=Status.RUNNING, agent="codex-a", type="eval",
                               importance="critical", claim_nonce="n1"))
    r._rate_limited(t, RunResult(ok=False, exit_code=1, stdout="", stderr="", error="usage limit", rate_limited=True))
    fresh = board.get_task(t.id)
    assert fresh.status is Status.READY and fresh.agent == "claude-a"
    assert board.get_agent("codex-a").status == "cooldown"



def test_tick_backoff_never_exceeds_a_minute():
    """Oct 5 2026: a DNS blip made the runner back off 5 minutes between ticks, its heartbeat went stale and serve
    reaped a healthy worker's task."""
    from swarm.runner import backoff_seconds
    assert [backoff_seconds(n) for n in (1, 2, 3, 4, 8)] == [15, 30, 60, 60, 60]


def test_retry_after_a_failure_continues_from_the_pushed_branch(cfg, git_repo, tmp_path):
    """Q-082: after a crash the retry started at main; the earlier commits had to be cherry-picked by hand."""
    adapter = FakeAdapter(files={"src/a.py": "first attempt\n"}, structured={"status": "failed", "summary": "half"})
    r, board = make_runner(cfg, git_repo, tmp_path, adapter)
    t = ready_task(board)
    assert r.run_task(t).status is Status.FAILED      # pushed, but no resume flag
    stored = board.get_task(t.id)
    assert "resume" not in stored.flags and stored.attempts == 1
    seen = {}
    orig = adapter.run

    def run(spec):
        seen["a"] = (spec.cwd / "src" / "a.py").read_text() if (spec.cwd / "src" / "a.py").exists() else None
        seen["prompt"] = spec.prompt_file.read_text()
        return orig(spec)
    adapter.run = run
    stored.status = Status.READY
    board.update_task(stored, ["status"])
    r.run_task(board.get_task(t.id))
    assert seen["a"] == "first attempt\n" and "previous attempt's commits" in seen["prompt"]


def test_branch_gets_main_that_moved_during_the_run(cfg, git_repo, tmp_path):
    """Q-097/Q-121: main moved while the worker ran; verify and review now see the branch on current main."""
    adapter = FakeAdapter(files={"src/a.py": "x\n"}, structured={"status": "done", "summary": "ok"})
    orig = adapter.run

    def run(spec):
        out = orig(spec)
        _git(git_repo, "checkout", "-q", "main")
        (git_repo / "merged_meanwhile.py").write_text("y\n")
        _git(git_repo, "add", "merged_meanwhile.py"); _git(git_repo, "commit", "-qm", "T-099 merged")
        _git(git_repo, "push", "-q", "origin", "main")
        return out
    adapter.run = run
    r, board = make_runner(cfg, git_repo, tmp_path, adapter)
    t = ready_task(board)
    assert r.run_task(t).status is Status.MERGE_READY
    log = _git(git_repo, "log", "--format=%s", "origin/task/" + t.id)
    assert "T-099 merged" in log


def test_worker_env_puts_the_worktree_first_on_pythonpath(cfg, git_repo, tmp_path, monkeypatch):
    """Six notes (Q-086 … Q-126): scripts run from /tmp imported the main checkout's editable install."""
    monkeypatch.setenv("PYTHONPATH", "/elsewhere")
    adapter = FakeAdapter(files={"src/a.py": "x"}, structured={"status": "done", "summary": "ok"})
    r, board = make_runner(cfg, git_repo, tmp_path, adapter)
    t = ready_task(board)
    r.run_task(t)
    env = adapter.specs[0].env
    wt = str(adapter.specs[0].cwd)
    assert env["PYTHONPATH"].split(":")[0] == wt and env["PYTHONPATH"].endswith("/elsewhere")
    assert env["SWARM_TASK_ID"] == t.id and env["SWARM_WORKTREE"] == wt


def test_adapter_passes_the_run_env_to_the_cli(tmp_path):
    from swarm.adapters.base import Adapter, RunSpec

    class Echo(Adapter):
        def build_command(self, spec):
            return ["sh", "-c", "echo $SWARM_TASK_ID"], None

        def parse_output(self, code, out, err):
            return RunResult(ok=code == 0, exit_code=code, stdout=out, stderr=err)
    pf = tmp_path / "p.md"
    pf.write_text("x")
    res = Echo().run(RunSpec(prompt_file=pf, model="m", effort=None, max_turns=1, budget_usd=None, timeout_s=30,
                             cwd=tmp_path, env={"SWARM_TASK_ID": "T-777"}))
    assert res.stdout.strip() == "T-777"


def test_cut_off_run_with_a_draft_report_resumes_and_keeps_the_draft(cfg, git_repo, tmp_path):
    """Q-096/Q-103/Q-126: a run that hit max turns lost its report. A draft written early is kept and the task
    resumes instead of going to review as if it were finished."""
    adapter = FakeAdapter(files={"src/a.py": "half"}, ok=False, structured=None,
                          report_file={"status": "done", "summary": "DRAFT: adapter written, bench pending"})
    r, board = make_runner(cfg, git_repo, tmp_path, adapter)
    t = ready_task(board)
    assert r.run_task(t).status is Status.CHANGES_REQUESTED
    stored = board.get_task(t.id)
    assert "resume" in stored.flags and "report_missing" not in stored.flags
    assert "DRAFT" in board.reports[t.id][0][1]


def test_usage_limit_cools_down_until_the_reset_or_three_hours(cfg, git_repo, tmp_path):
    """codex-b's exhausted plan (Oct 6 2026) cooled down 15 minutes and came back to fail two more tasks."""
    board = InMemoryBoard()
    board.upsert_agent(AgentRow(name="claude-a", status="idle", last_heartbeat=utcnow()))
    r = Runner(cfg, board, "host-a", Workspace(git_repo, tmp_path / "wt"), ledger=Ledger(tmp_path / "u.jsonl"),
               log=lambda *a: None)
    t = board.create_task(Task(id="", title="x", status=Status.RUNNING, agent="codex-a", type="backend", claim_nonce="n"))
    r._rate_limited(t, RunResult(ok=False, exit_code=1, stdout="", stderr="", rate_limited=True, usage_limited=True,
                                 error="You've hit your usage limit. Upgrade to Pro"))
    row = board.get_agent("codex-a")
    assert row.note.startswith("usage limit until") and "no reset time given" in row.note
    assert row.cooldown_until > utcnow() + timedelta(hours=2, minutes=55)
    assert board.get_task(t.id).last_error.startswith("usage limit:") and board.get_task(t.id).agent == "claude-a"
    reset = utcnow() + timedelta(hours=5)
    t2 = board.create_task(Task(id="", title="y", status=Status.RUNNING, agent="codex-a", type="backend", claim_nonce="m"))
    r._rate_limited(t2, RunResult(ok=False, exit_code=1, stdout="", stderr="", rate_limited=True, usage_limited=True,
                                  reset_at=reset, error="usage limit"))
    assert board.get_agent("codex-a").cooldown_until == reset
    # a plain rate limit keeps the short cooldown
    t3 = board.create_task(Task(id="", title="z", status=Status.RUNNING, agent="claude-a", type="backend", claim_nonce="o"))
    r._rate_limited(t3, RunResult(ok=False, exit_code=1, stdout="", stderr="", rate_limited=True, error="429"))
    row = board.get_agent("claude-a")
    assert row.note.startswith("rate limited") and row.cooldown_until < utcnow() + timedelta(minutes=20)
    # the runner's first heartbeat after a restart keeps the limit note (status and routing read it)
    r.heartbeat(force=True)
    assert board.get_agent("codex-a").note.startswith("usage limit") and board.get_agent("codex-a").status == "cooldown"


def test_worker_turns_have_an_effort_floor():
    """Q-142/Q-143: S tasks hit error_max_turns right before finishing at 30 turns."""
    from swarm.runner import worker_turns
    assert [worker_turns(30, e) for e in ("low", "medium", "high", "xhigh", None)] == [30, 60, 100, 150, 30]
    assert worker_turns(120, "medium") == 120          # a bigger size limit is never lowered


def test_after_max_turns_the_next_prompt_says_finish_do_not_start_over(cfg, git_repo, tmp_path):
    adapter = FakeAdapter(files={"src/a.py": "nearly done\n"}, ok=False, structured=None)
    orig = adapter.run

    def run(spec):
        res = orig(spec)
        res.error = "error_max_turns: "
        return res
    adapter.run = run
    r, board = make_runner(cfg, git_repo, tmp_path, adapter)
    t = ready_task(board, agent="claude-a", model="sonnet", effort="medium", feedback="reviewer: handle empty input")
    assert r.run_task(t).status is Status.CHANGES_REQUESTED
    assert adapter.specs[0].max_turns == 60             # S limit is 30; medium effort raises it
    stored = board.get_task(t.id)
    assert stored.feedback.startswith("The previous attempt ran out of turns right before finishing; its work is on "
                                      "the branch — finish and report, do not start over.")
    assert "reviewer: handle empty input" in stored.feedback
    stored.status = Status.READY
    board.update_task(stored, ["status"])
    r.run_task(board.get_task(t.id))
    assert "do not start over" in adapter.prompts[1]



def test_worker_questions_keep_their_full_text(cfg, git_repo, tmp_path):
    """Q-153 was cut at 190 chars by the runner before it ever reached the board."""
    from swarm.status import render_status
    text = ("Should DEMO.md and CONTRACTS.md both change? " * 40)[:1500].strip()
    adapter = FakeAdapter(files={"src/a.py": "x"}, structured={"status": "blocked", "summary": "stuck",
                                                              "question": {"kind": "blocking", "text": text,
                                                                           "options": [], "proceeding_with": ""}})
    r, board = make_runner(cfg, git_repo, tmp_path, adapter)
    ready_task(board)
    r.run_task(board.list_tasks()[0])
    q = board.list_questions()[0]
    assert q.text == text
    assert text in render_status(cfg, board.list_tasks(), [], board.list_questions(), utcnow())


def _push_from_other_clone(git_repo, tmp_path, branch, files, message):
    """Another laptop's attempt: clone origin, commit on `branch`, push."""
    other = tmp_path / f"other-{abs(hash(message)) % 10_000}"
    remote = _git(git_repo, "remote", "get-url", "origin").strip()
    subprocess.run(["git", "clone", "-q", remote, str(other)], check=True)
    if branch in _git(other, "branch", "-r"):
        _git(other, "checkout", "-q", "-B", branch, f"origin/{branch}")
    else:
        _git(other, "checkout", "-q", "-b", branch)
    for rel, text in files.items():
        (other / rel).parent.mkdir(parents=True, exist_ok=True)
        (other / rel).write_text(text)
    _git(other, "add", "-A")
    _git(other, "-c", "user.email=o@x", "-c", "user.name=o", "commit", "-qm", message)
    _git(other, "push", "-q", "origin", f"HEAD:refs/heads/{branch}")


def test_retry_worktree_contains_every_commit_on_the_remote_task_branch(cfg, git_repo, tmp_path):
    """Q-167 (T-074): the retry got an empty branch fresh from main although origin/task/T-074 held the earlier
    commits. Here the local task branch is left behind origin (a later attempt pushed from another laptop), the
    worktree is gone, and the task comes back Ready without the resume flag, as serve's retry_failed leaves it."""
    adapter = FakeAdapter(files={"src/a.py": "first attempt\n"}, structured={"status": "failed", "summary": "half"})
    r, board = make_runner(cfg, git_repo, tmp_path, adapter)
    t = ready_task(board)
    assert r.run_task(t).status is Status.FAILED
    assert not r.ws.worktree_path(t.id).exists()
    local_tip = _git(git_repo, "rev-parse", f"task/{t.id}").strip()        # the local branch stays at attempt 1
    _push_from_other_clone(git_repo, tmp_path, f"task/{t.id}", {"src/b.py": "second attempt\n"}, "T-001: attempt 2")
    stored = board.get_task(t.id)
    stored.status, stored.flags = Status.READY, [f for f in stored.flags if f != "resume"]
    board.update_task(stored, ["status", "flags"])
    seen = {}

    def run(spec):
        seen["log"] = _git(spec.cwd, "log", "--format=%s", "HEAD")
        seen["files"] = sorted(p.name for p in (spec.cwd / "src").iterdir())
        seen["prompt"] = spec.prompt_file.read_text()
        return RunResult(ok=True, exit_code=0, stdout="", stderr="", structured_output={"status": "done", "summary": "ok"})
    adapter.run = run
    r.run_task(board.get_task(t.id))
    assert seen["files"] == ["a.py", "b.py"] and "T-001: attempt 2" in seen["log"]
    assert local_tip != _git(git_repo, "rev-parse", f"origin/task/{t.id}").strip()
    assert "previous attempt's commits" in seen["prompt"]


def test_requeued_first_attempt_with_a_pr_resumes_from_the_remote_branch(cfg, git_repo, tmp_path):
    """attempts == 0 and no resume flag (a reaped or hand-requeued run), but the branch has a PR: it is this
    task's work and must not be replaced by main and force-pushed over."""
    adapter = FakeAdapter(files={"src/c.py": "x\n"}, structured={"status": "done", "summary": "ok"})
    r, board = make_runner(cfg, git_repo, tmp_path, adapter)
    t = ready_task(board, pr_url="https://gh/pr/9")
    _push_from_other_clone(git_repo, tmp_path, f"task/{t.id}", {"src/b.py": "earlier\n"}, "earlier work")
    seen = {}
    orig = adapter.run

    def run(spec):
        seen["b"] = (spec.cwd / "src" / "b.py").exists()
        return orig(spec)
    adapter.run = run
    r.run_task(t)
    assert seen["b"] is True
    assert _git(git_repo, "show", f"origin/task/{t.id}:src/b.py") == "earlier\n"


def test_stale_rebase_and_conflict_feedback_never_reaches_a_clean_run(cfg, git_repo, tmp_path):
    """Q-164/Q-166 (T-070): a merger's "markers will be left" plus an older runner's "First run `git fetch origin &&
    git rebase origin/main`" reached a worker whose worktree was clean; it followed the rebase."""
    adapter = FakeAdapter(files={"src/a.py": "half\n"}, ok=False, structured=None)
    r, board = make_runner(cfg, git_repo, tmp_path, adapter)
    t = ready_task(board)
    r.run_task(t)                                    # parks work on the branch (resume)
    stored = board.get_task(t.id)
    stored.status = Status.CHANGES_REQUESTED
    stored.feedback = ("Main moved and now conflicts with this branch in: src/a.py. Before your next run the harness "
                       "merges origin/main into your branch and leaves conflict markers in those files. Resolve them, "
                       "`git add` them and `git commit`. Do not run `git rebase`, `git fetch` or `git merge` yourself."
                       "\n\nRebase onto main conflicted in: src/a.py (main changed them in: abc T-9). First run "
                       "`git fetch origin && git rebase origin/main`, resolve every conflict keeping main's intent, "
                       "`git rebase --continue`, then do the task.\n\nReviewer: keep the empty-input test.")
    board.update_task(stored, ["status", "feedback"])
    seen = {}

    def run(spec):
        seen["prompt"] = spec.prompt_file.read_text()
        return RunResult(ok=True, exit_code=0, stdout="", stderr="", structured_output={"status": "done", "summary": "ok"})
    adapter.run = run
    r.run_task(board.get_task(t.id))
    p = seen["prompt"]
    assert "git rebase origin/main" not in p and "git fetch origin &&" not in p and "rebase --continue" not in p
    assert "Main moved and now conflicts" not in p and "## Merge conflicts" not in p
    assert "without conflicts; there are no conflict markers" in p and "Reviewer: keep the empty-input test." in p


def test_verify_feedback_leads_with_the_failing_lint_output(cfg, git_repo, tmp_path):
    """Q-168 (T-074): ruff failed the run, but the feedback was the pytest tail ("834 passed") only."""
    progress = "\n".join("." * 72 + " [%3d%%]" % p for p in range(4, 101, 4))
    _break_verify(git_repo, "#!/bin/sh\necho 'ruff: FAIL'\n"
                  "echo 'src/a.py:1:1: F401 [*] `os` imported but unused'\necho 'Found 1 error.'\n"
                  f"cat <<'EOF'\n{progress}\n834 passed, 34 deselected in 50.08s\nEOF\nexit 1\n")
    adapter = FakeAdapter(files={"src/a.py": "import os\n"}, structured={"status": "done", "summary": "s"})
    r, board = make_runner(cfg, git_repo, tmp_path, adapter)
    t = ready_task(board)
    assert r.run_task(t).status is Status.CHANGES_REQUESTED
    fb = board.get_task(t.id).feedback
    assert "F401 [*] `os` imported but unused" in fb and "The tests passed" in fb
    assert fb.index("F401") < fb.index("834 passed")


def test_notes_and_draft_report_reach_the_next_attempt_verbatim(cfg, git_repo, tmp_path):
    """Q-160/Q-162: a max-turns attempt left no notes; the retry re-derived measurements it had taken."""
    adapter = FakeAdapter(files={"src/a.py": "half\n", ".swarm-run/notes.md": "- replay p50 = 812 ms (eval.replay --clip demo)\n"},
                          ok=False, structured=None, report_file={"status": "done", "summary": "DRAFT: replay measured"})
    orig = adapter.run

    def run(spec):
        res = orig(spec)
        res.error = "error_max_turns: "
        return res
    adapter.run = run
    r, board = make_runner(cfg, git_repo, tmp_path, adapter)
    t = ready_task(board, agent="claude-a", model="sonnet", effort="medium")
    assert r.run_task(t).status is Status.CHANGES_REQUESTED
    assert "replay p50 = 812 ms" in board.reports[t.id][0][1]          # on the board for another laptop too
    stored = board.get_task(t.id)
    stored.status = Status.READY
    board.update_task(stored, ["status"])
    adapter.files = {"src/a.py": "done\n"}
    adapter.report_file = None
    r.run_task(board.get_task(t.id))
    p = adapter.prompts[1]
    assert "## Notes from the previous attempt" in p
    assert "- replay p50 = 812 ms (eval.replay --clip demo)" in p and "DRAFT: replay measured" in p
    assert "Limits for this run: 20 minutes wall-clock, 100 turns (time you spend waiting in `swarm-lock`" in p and "up to 10 more minutes)." in p      # resumed one tier up: opus/high


def test_placeholder_tokens_in_changed_docs_send_the_task_back(cfg, git_repo, tmp_path):
    adapter = FakeAdapter(files={"docs/BENCHMARKS.md": "# Bench\n\n| feed | p50 |\n| natural | TBD |\n",
                                 "src/a.py": "# TODO: tidy\nx = 1\n"},
                          structured={"status": "done", "summary": "s"})
    r, board = make_runner(cfg, git_repo, tmp_path, adapter)
    t = ready_task(board, scope=["src/**", "docs/**"])
    assert r.run_task(t).status is Status.CHANGES_REQUESTED
    fb = board.get_task(t.id).feedback
    assert "- docs/BENCHMARKS.md:4: | natural | TBD |" in fb and "src/a.py" not in fb
    # code comments are not checked, and a project can switch the check off
    adapter.files = {"src/b.py": "# TODO: later\n"}
    r2, board2 = make_runner(cfg, git_repo, tmp_path / "two", adapter)
    t2 = ready_task(board2, id="T-002")
    assert r2.run_task(t2).status is Status.MERGE_READY
    cfg.verify.placeholders = []
    adapter.files = {"docs/X.md": "TBD\n"}
    r3, board3 = make_runner(cfg, git_repo, tmp_path / "three", adapter)
    t3 = ready_task(board3, id="T-003", scope=["docs/**"])
    assert r3.run_task(t3).status is Status.MERGE_READY


def test_busy_runner_drains_once_its_code_is_stale(cfg, git_repo, tmp_path, monkeypatch):
    """laptop-a's claude-a always had work, so it never self-updated and ran pre-6d5d2b3 code for a day."""
    import swarm.selfupdate as su
    adapter = FakeAdapter()
    r, board = make_runner(cfg, git_repo, tmp_path, adapter)
    r.auto_update, r._loaded_head = True, "old"
    calls = {"ff": 0, "restart": 0}
    monkeypatch.setattr(su, "current_head", lambda *a, **k: "new")
    monkeypatch.setattr(su, "upstream_ahead", lambda *a, **k: False)
    monkeypatch.setattr(su, "check_and_update", lambda *a, **k: calls.__setitem__("ff", calls["ff"] + 1))
    monkeypatch.setattr(su, "restart_self", lambda: calls.__setitem__("restart", calls["restart"] + 1))
    now = utcnow()
    r.active["codex-a"].add("T-001")
    r.maybe_self_update(now)
    assert not r.draining and calls == {"ff": 0, "restart": 0}       # busy: never touch the checkout yet
    r.maybe_self_update(now + timedelta(minutes=31))
    assert r.draining and calls["restart"] == 0                      # stale for 30+ min: stop claiming
    r.active["codex-a"].clear()
    assert r.finish_drain_if_idle() and calls == {"ff": 1, "restart": 1}
    # idle and another process already pulled newer code: restart right away
    r2, _ = make_runner(cfg, git_repo, tmp_path / "two", adapter)
    r2.auto_update, r2._loaded_head = True, "old"
    r2.maybe_self_update(now)
    assert calls["restart"] == 2


def test_loser_of_a_forced_reassign_leaves_the_winners_worktree(cfg, git_repo, tmp_path):
    """Q-200 (T-093): `swarm assign --force` moved a claimed task to another agent; the old run's claim watch stopped
    its CLI and its cleanup disposed the worktree path the new owner was already working in."""
    from swarm.board.base import claim_task

    class TransferMidRun(FakeAdapter):
        def run(self, spec):
            res = super().run(spec)
            t = board.get_task(task.id)
            t.agent, t.status, t.claim_nonce = "claude-a", Status.READY, ""     # assign --force
            board.update_task(t, ["agent", "status", "claim_nonce"])
            winner = board.get_task(task.id)
            assert claim_task(board, winner, "claude-a", sleep=lambda s: None)
            wt2 = r.ws.provision(task.id, reuse_branch=False)                     # the new owner's attempt
            (wt2 / "winner.txt").write_text("still here\n")
            return res

    adapter = TransferMidRun(files={"src/a.py": "x = 1\n"}, structured={"status": "done", "summary": "s"})
    r, board = make_runner(cfg, git_repo, tmp_path, adapter)
    task = ready_task(board)
    r.run_task(task)
    wt = r.ws.worktree_path(task.id)
    assert (wt / "winner.txt").read_text() == "still here\n"
    stored = board.get_task(task.id)
    assert stored.agent == "claude-a" and stored.status is Status.RUNNING   # the loser published nothing
    r.ws.dispose(wt)


def test_claim_moved_before_provision_does_not_touch_the_path(cfg, git_repo, tmp_path):
    adapter = FakeAdapter(files={"src/a.py": "x"}, structured={"status": "done", "summary": "s"})
    r, board = make_runner(cfg, git_repo, tmp_path, adapter)
    t = ready_task(board)
    real_claim_moved = r._claim_moved
    r._claim_moved = lambda task, nonce, on_error=True: True
    r.run_task(t)
    assert adapter.specs == [] and not r.ws.worktree_path(t.id).exists()
    r._claim_moved = real_claim_moved


def _max_turns(adapter):
    orig = adapter.run

    def run(spec):
        res = orig(spec)
        res.error = "error_max_turns: "
        res.session_id = "sess-1"
        return res
    adapter.run = run


def test_resumed_prompt_lists_branch_commits_and_quotes_docs_from_the_worktree(cfg, git_repo, tmp_path):
    """Q-180 (T-079): the resume prompt quoted CONTRACTS from the main checkout, which the branch had rewritten."""
    adapter = FakeAdapter(files={"docs/CONTRACTS.md": "# Contracts\n\n## synth_feed\nBRANCH WORDING: back to mixture\n"},
                          ok=False, structured=None)
    _max_turns(adapter)
    r, board = make_runner(cfg, git_repo, tmp_path, adapter)
    t = ready_task(board, agent="claude-a", model="sonnet", effort="medium", scope=["docs/**"])
    assert r.run_task(t).status is Status.CHANGES_REQUESTED
    stored = board.get_task(t.id)
    stored.status = Status.READY
    board.update_task(stored, ["status"])
    adapter.files = {"src/a.py": "x\n"}
    r.run_task(board.get_task(t.id))
    p = adapter.prompts[1]
    assert "## Work already on this branch" in p and f"{t.id}: " in p.split("## Work already on this branch")[1]
    assert "BRANCH WORDING: back to mixture" in p and "## events" not in p     # not the main checkout's copy
    assert "read from your worktree" in p
    assert "## Work already on this branch" not in adapter.prompts[0]


def test_cut_off_run_without_notes_carries_its_last_commands(cfg, git_repo, tmp_path, monkeypatch):
    """Q-183 (T-075): attempt 1 hit max_turns without notes; its iLab job id and RTT were lost."""
    import json as _json
    from swarm import transcript
    home = tmp_path / "claude-home"
    monkeypatch.setattr(transcript.Path, "home", classmethod(lambda cls: home))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    adapter = FakeAdapter(files={"src/a.py": "half\n"}, ok=False, structured=None)
    _max_turns(adapter)
    r, board = make_runner(cfg, git_repo, tmp_path, adapter)
    t = ready_task(board, agent="claude-a", model="sonnet", effort="medium")
    proj = home / ".claude" / "projects" / transcript.project_dir_name(r.ws.worktree_path(t.id))
    proj.mkdir(parents=True)
    lines = [{"message": {"content": [{"type": "tool_use", "id": "u1", "name": "Bash",
                                       "input": {"command": "sbatch gpu.sh"}}]}},
             {"message": {"content": [{"type": "tool_result", "tool_use_id": "u1",
                                       "content": "Submitted batch job 81234"}]}},
             {"message": {"content": [{"type": "tool_use", "id": "u2", "name": "Bash",
                                       "input": {"command": "python -m eval.rtt"}}]}},
             {"message": {"content": [{"type": "tool_result", "tool_use_id": "u2",
                                       "content": [{"type": "text", "text": "rtt p50 5.6 ms"}]}]}}]
    (proj / "sess-1.jsonl").write_text("\n".join(_json.dumps(x) for x in lines) + "\n")
    assert r.run_task(t).status is Status.CHANGES_REQUESTED
    assert (tmp_path / "logs" / t.id / "attempt-1" / "commands.md").exists()
    stored = board.get_task(t.id)
    stored.status = Status.READY
    board.update_task(stored, ["status"])
    r.run_task(board.get_task(t.id))
    p = adapter.prompts[1]
    assert "last shell commands" in p and "$ sbatch gpu.sh\nSubmitted batch job 81234" in p and "rtt p50 5.6 ms" in p


def test_backup_files_added_by_a_task_send_it_back(cfg, git_repo, tmp_path):
    """Q-198: a BSD-sed backup docs/BENCHMARKS.md-e reached main."""
    adapter = FakeAdapter(files={"src/a.py": "x = 1\n", "docs/BENCHMARKS.md-e": "old\n", "src/a.py.orig": "x\n"},
                          structured={"status": "done", "summary": "s"})
    r, board = make_runner(cfg, git_repo, tmp_path, adapter)
    t = ready_task(board, scope=["src/**", "docs/**"])
    assert r.run_task(t).status is Status.CHANGES_REQUESTED
    fb = board.get_task(t.id).feedback
    assert "docs/BENCHMARKS.md-e" in fb and "src/a.py.orig" in fb and "git rm --cached" in fb
    cfg.verify.stray_files = []
    r2, board2 = make_runner(cfg, git_repo, tmp_path / "two", adapter)
    assert r2.run_task(ready_task(board2, scope=["src/**", "docs/**"])).status is Status.MERGE_READY


def test_stray_patterns():
    from swarm.feedback import DEFAULT_STRAY_PATTERNS, stray_files
    paths = ["docs/BENCHMARKS.md-e", "a.orig", "x.rej", "notes.bak", "f.py~", ".DS_Store", "web/.DS_Store",
             "docs/pre-e.md", "scripts/run-e", "src/ok.py", "tests/test_some-e2e.mjs"]
    assert stray_files(paths, DEFAULT_STRAY_PATTERNS) == paths[:7]


def test_finished_attempt_sent_back_gets_its_report_and_notes_restored(cfg, git_repo, tmp_path):
    """Q-202/Q-203/Q-208: a merge-only re-run found only prompt.md and rebuilt its report from docs/decisions."""
    adapter = FakeAdapter(files={"src/a.py": "x = 1\n", ".swarm-run/notes.md": "- eval pair1 F1 0.91\n"},
                          structured={"status": "done", "summary": "association evals done, table in docs"})
    r, board = make_runner(cfg, git_repo, tmp_path, adapter)
    t = ready_task(board, agent="claude-a", model="sonnet", effort="medium")
    assert r.run_task(t).status is Status.MERGE_READY
    stored = board.get_task(t.id)
    stored.status, stored.feedback = Status.CHANGES_REQUESTED, "Main moved and now conflicts with this branch in: docs/X.md."
    board.update_task(stored, ["status", "feedback"])
    seen = {}
    orig = adapter.run

    def run(spec):
        seen["notes"] = (spec.cwd / ".swarm-run" / "notes.md").read_text()
        seen["report"] = (spec.cwd / ".swarm-run" / "previous_report.json").read_text()
        return orig(spec)
    adapter.run = run
    adapter.files = {"src/a.py": "x = 2\n"}
    r.run_task(board.get_task(t.id))
    assert "eval pair1 F1 0.91" in seen["notes"] and "association evals done" in seen["report"]
    assert "association evals done" in adapter.prompts[1] and "update this report instead of rebuilding" in adapter.prompts[1]


def test_prompt_flags_cited_tasks_that_are_not_merged(cfg, git_repo, tmp_path):
    """Q-236/Q-238: 'with the mixer floor from T-096' and 'target agreement from T-100' named work not on main."""
    adapter = FakeAdapter(files={"src/a.py": "x\n"},
                          structured={"status": "done", "summary": "ok", "files_changed": ["src/a.py"]})
    r, board = make_runner(cfg, git_repo, tmp_path, adapter)
    floor = ready_task(board, title="Mixer floor", status=Status.REVIEW)
    merged = ready_task(board, title="Old work", status=Status.DONE)
    t = ready_task(board, description=f"Use the mixer floor from {floor.id}; build on {merged.id}.")
    r.run_task(board.get_task(t.id))
    p = adapter.prompts[0]
    assert "### Tasks this text cites that are not merged yet" in p
    assert f'- {floor.id} "Mixer floor" is Review' in p and f"- {merged.id}" not in p


def test_prompt_lists_generated_data_of_cited_unmerged_tasks(cfg, git_repo, tmp_path):
    """Q-444: T-141's fixtures existed in the shared env dir but the prompt only said not to build on T-141."""
    fixtures = tmp_path / "shared" / "fixtures"
    (fixtures / "t141").mkdir(parents=True)
    (fixtures / "t141" / "a.wav").write_text("x")
    (fixtures / "other").mkdir()
    (fixtures / "other" / "T-141-notes.json").write_text("{}")
    (fixtures / "other" / "t142.json").write_text("{}")
    results = tmp_path / "shared" / "results"
    results.mkdir()
    for i in range(14):
        (results / f"T141_run{i:02d}.json").write_text("{}")
    cfg.env = {"HEARING_FIXTURES_DIR": str(fixtures), "HEARING_RESULTS_DIR": str(results), "OTHER": "not a dir"}
    adapter = FakeAdapter(files={"src/a.py": "x\n"},
                          structured={"status": "done", "summary": "ok", "files_changed": ["src/a.py"]})
    r, board = make_runner(cfg, git_repo, tmp_path, adapter)
    cited = board.create_task(Task(id="T-141", title="Profile eval", status=Status.REVIEW, type="backend"))
    t = ready_task(board, description="Use the pairs from T-141.")
    r.run_task(board.get_task(t.id))
    p = adapter.prompts[0]
    assert "Generated by T-141 (not merged): usable as data, do not build on its code" in p
    assert f"- {fixtures}/t141" in p and f"- {fixtures}/other/T-141-notes.json" in p
    assert "t142.json" not in p
    assert sum(1 for line in p.splitlines() if line.startswith("  - /")) == 10
    assert r._generated_paths("T-999") == []


def test_verify_failing_right_after_a_clean_merge_of_main_says_it_may_be_semantic(cfg, git_repo, tmp_path):
    """Q-228: verify failed after the harness merged main (T-088) into T-092: a constant one task changed and the
    other task's code read. No textual conflict, so the worker saw a bare failure."""
    adapter = FakeAdapter(files={"src/a.py": "x\n"}, structured={"status": "done", "summary": "ok"})
    orig = adapter.run

    def run(spec):
        out = orig(spec)
        _git(git_repo, "checkout", "-q", "main")
        (git_repo / "src").mkdir(exist_ok=True)
        (git_repo / "src" / "const.py").write_text("LIMIT = 2\n")
        (git_repo / "scripts" / "verify_fast.sh").write_text(
            "#!/bin/sh\nif [ -f src/a.py ] && [ -f src/const.py ]; then echo 'tests: FAIL a.py reads LIMIT'; exit 1; fi\n")
        _git(git_repo, "add", "-A"); _git(git_repo, "commit", "-qm", "T-088 changes LIMIT")
        _git(git_repo, "push", "-q", "origin", "main")
        return out
    adapter.run = run
    r, board = make_runner(cfg, git_repo, tmp_path, adapter)
    t = ready_task(board)
    assert r.run_task(t).status is Status.CHANGES_REQUESTED
    fb = board.get_task(t.id).feedback
    assert "semantic merge conflict" in fb and "T-088 changes LIMIT" in fb and "src/const.py" in fb


def test_resume_lists_results_written_to_shared_dirs_since_the_last_attempt(cfg, git_repo, tmp_path):
    """Q-241: attempt 1's notes said one run had finished; its background job had written two result files to
    $HEARING_RESULTS_DIR, which the retry never looked at."""
    import time as _t
    results = tmp_path / "results"
    results.mkdir()
    (results / "old.json").write_text("{}")
    cfg.env = {"HEARING_RESULTS_DIR": str(results), "NOT_A_DIR": "fast"}
    r, board = make_runner(cfg, git_repo, tmp_path, FakeAdapter())
    d = r.log_dir / "T-9" / "attempt-1"
    d.mkdir(parents=True)
    (d / "started").write_text(str(_t.time() + 0.5))
    _t.sleep(0.6)
    (results / "demo_regress_132522.json").write_text("{}")
    (results / "demo_regress_132736.json").write_text("{}")
    carry = r._previous_carry("T-9", 2)
    assert "shared data dirs since attempt 1 started" in carry
    assert "demo_regress_132522.json" in carry and "demo_regress_132736.json" in carry and "old.json" not in carry


def test_rate_limit_on_a_changes_requested_round_keeps_status_and_agent_when_all_are_cooling(cfg, git_repo, tmp_path):
    """Field note 85 (Oct 6 2026): T-095/T-102 were merge-conflict rounds (Changes Requested, PR open); the usage
    limit put them back as Ready on another cooling account."""
    from swarm.runner import Runner
    from swarm.board.memory import InMemoryBoard
    from swarm.workspace import Workspace
    from swarm.usage import Ledger
    from swarm.models import AgentRow, RunResult, Status, Task, utcnow
    board = InMemoryBoard()
    now = utcnow()
    board.upsert_agent(AgentRow(name="codex-a", status="cooldown", last_heartbeat=now,
                                cooldown_until=now + timedelta(hours=1)))
    board.upsert_agent(AgentRow(name="fake-b", status="offline", last_heartbeat=now - timedelta(hours=2)))
    r = Runner(cfg, board, "host-a", Workspace(git_repo, tmp_path / "wt"), ledger=Ledger(tmp_path / "u.jsonl"),
               log=lambda *a: None)
    t = board.create_task(Task(id="", title="x", status=Status.RUNNING, agent="claude-a", type="backend",
                               claim_nonce="n", pr_url="https://x/pull/88", feedback="resolve the conflict"))
    out = r._rate_limited(t, RunResult(ok=False, exit_code=1, stdout="", stderr="", rate_limited=True,
                                       usage_limited=True, error="You've hit your usage limit"),
                          prev_status=Status.CHANGES_REQUESTED)
    got = board.get_task(t.id)
    assert got.status is Status.CHANGES_REQUESTED and out.status is Status.CHANGES_REQUESTED
    assert got.agent == "claude-a" and got.claim_nonce == "" and got.feedback == "resolve the conflict"
    # a Ready claim still goes back to Ready
    t2 = board.create_task(Task(id="", title="y", status=Status.RUNNING, agent="claude-a", type="backend", claim_nonce="m"))
    r._rate_limited(t2, RunResult(ok=False, exit_code=1, stdout="", stderr="", rate_limited=True, error="429"),
                    prev_status=Status.READY)
    assert board.get_task(t2.id).status is Status.READY


def _dead_pid() -> int:
    p = subprocess.Popen(["true"])
    p.wait()
    return p.pid


def _finished_thread():
    import threading
    th = threading.Thread(target=lambda: None)
    th.start()
    th.join()
    return th


def test_drain_counts_a_run_whose_cli_and_thread_are_gone_as_finished(cfg, git_repo, tmp_path, monkeypatch):
    """Field note 90: both runners drained for 11 minutes with no worker CLI alive; a finished run still held its
    slot. A run with no live CLI and no live thread is finished, whatever the bookkeeping says."""
    from swarm.runner import InFlight
    logs = []
    r, _ = make_runner(cfg, git_repo, tmp_path, FakeAdapter())
    r.log = logs.append
    restarted = []
    monkeypatch.setattr("swarm.selfupdate.restart_self", lambda: restarted.append(True))
    r.active["codex-a"].add("T-001")
    r.runs["T-001"] = InFlight("T-001", "codex-a", "S", r.now(), thread=_finished_thread(), pid=_dead_pid(),
                               phase="publish")
    r.active["claude-a"].add("T-002")           # a slot with no run record at all
    r.request_restart()
    assert r.finish_drain_if_idle() and restarted == [True]
    assert not any(r.active.values()) and not r.runs
    assert any("T-001" in line and "counting it as finished" in line for line in logs)
    assert any("T-002" in line and "no run record" in line for line in logs)


def test_drain_waits_on_a_live_thread_and_logs_what_it_waits_on_every_minute(cfg, git_repo, tmp_path, monkeypatch):
    """A run between its CLI and its publish (verify, push) is still in flight; the log names task, pid, elapsed."""
    import threading
    from swarm.runner import InFlight
    clock = [utcnow()]
    logs = []
    r, _ = make_runner(cfg, git_repo, tmp_path, FakeAdapter())
    r.now, r.log = (lambda: clock[0]), logs.append
    monkeypatch.setattr("swarm.selfupdate.restart_self", lambda: None)
    release = threading.Event()
    th = threading.Thread(target=release.wait, daemon=True)
    th.start()
    dead = _dead_pid()
    r.active["codex-a"].add("T-001")
    r.runs["T-001"] = InFlight("T-001", "codex-a", "M", clock[0] - timedelta(minutes=12), thread=th, pid=dead,
                               phase="publish")
    try:
        r.request_restart()
        assert not r.finish_drain_if_idle()
        waits = [line for line in logs if line.startswith("draining: waiting on")]
        assert len(waits) == 1 and "T-001" in waits[0] and f"pid {dead} exited" in waits[0] and "12 min" in waits[0]
        assert "gives up in 40 min" in waits[0]          # one M size limit (40 min in the test config)
        clock[0] += timedelta(seconds=30)
        assert not r.finish_drain_if_idle()
        assert len([line for line in logs if line.startswith("draining: waiting on")]) == 1
        clock[0] += timedelta(seconds=31)
        assert not r.finish_drain_if_idle()
        assert len([line for line in logs if line.startswith("draining: waiting on")]) == 2
    finally:
        release.set()
        th.join()


def test_drain_times_out_after_one_size_limit_and_stops_the_cli(cfg, git_repo, tmp_path, monkeypatch):
    """A drain that is still waiting after one size limit restarts anyway, parking nothing; a CLI still alive is
    stopped so it cannot keep writing the worktree the resumed attempt will reuse."""
    from swarm.runner import InFlight
    clock = [utcnow()]
    logs = []
    r, _ = make_runner(cfg, git_repo, tmp_path, FakeAdapter())
    r.now, r.log = (lambda: clock[0]), logs.append
    restarted = []
    monkeypatch.setattr("swarm.selfupdate.restart_self", lambda: restarted.append(True))
    cli = subprocess.Popen(["sleep", "60"], start_new_session=True)
    try:
        r.active["codex-a"].add("T-001")
        r.runs["T-001"] = InFlight("T-001", "codex-a", "S", clock[0], pid=cli.pid, phase="cli")
        r.request_restart()
        assert not r.finish_drain_if_idle()
        clock[0] += timedelta(minutes=19)
        assert not r.finish_drain_if_idle() and not restarted
        clock[0] += timedelta(minutes=1)             # S = 20 minutes in the test config
        assert r.finish_drain_if_idle() and restarted == [True]
        assert cli.wait(timeout=10) != 0             # the CLI was stopped
        assert any("drain timed out after 20 min" in line and "T-001" in line for line in logs)
    finally:
        if cli.poll() is None:
            cli.kill()
            cli.wait()


def test_a_rerouted_rate_limited_run_releases_its_own_slot(cfg, git_repo, tmp_path):
    """Field note 90's cause: `_rate_limited` moved the task to another agent mid-run (task.agent changed), and the
    run's cleanup released the new agent's slot. The old agent's slot stayed taken: fewer free slots and a drain that
    waited forever with no CLI alive."""
    r, board = make_runner(cfg, git_repo, tmp_path, FakeAdapter(ok=False, rate_limited=True))
    board.upsert_agent(AgentRow(name="claude-a", status="idle", last_heartbeat=utcnow()))
    board.upsert_agent(AgentRow(name="codex-a", status="idle", last_heartbeat=utcnow()))
    t = ready_task(board, id="T-001")
    free = r.free_slots("codex-a")
    assert r.tick() == 1
    assert board.get_task(t.id).agent == "claude-a"      # rerouted away from the limited agent
    assert not any(r.active.values()) and not r.runs
    assert r.free_slots("codex-a") == free


def test_a_long_report_is_carried_whole_and_helper_files_survive_the_next_attempt(cfg, git_repo, tmp_path):
    """Q-257: previous_report.json was cut mid-string at 6000 characters (invalid JSON). Q-250/Q-257/Q-274: helper
    scripts the worker wrote to .swarm-run (enr_phase.py, a3runs.sh) were gone on the resumed attempt."""
    long_summary = "association evals done; " + "x" * 9000
    adapter = FakeAdapter(files={"src/a.py": "x = 1\n", ".swarm-run/notes.md": "- run .swarm-run/a3runs.sh\n",
                                 ".swarm-run/a3runs.sh": "echo shard\n", ".swarm-run/tools/enr_phase.py": "print(1)\n"},
                          structured={"status": "done", "summary": long_summary})
    r, board = make_runner(cfg, git_repo, tmp_path, adapter)
    t = ready_task(board, agent="claude-a", model="sonnet", effort="medium")
    assert r.run_task(t).status is Status.MERGE_READY
    stored = board.get_task(t.id)
    stored.status, stored.feedback = Status.CHANGES_REQUESTED, "Main moved and now conflicts with this branch."
    board.update_task(stored, ["status", "feedback"])
    seen = {}
    orig = adapter.run

    def run(spec):
        rd = spec.cwd / ".swarm-run"
        seen["report"] = json.loads((rd / "previous_report.json").read_text())
        seen["helpers"] = ((rd / "a3runs.sh").read_text(), (rd / "tools" / "enr_phase.py").read_text())
        return orig(spec)
    adapter.run = run
    adapter.files = {"src/a.py": "x = 2\n"}
    r.run_task(board.get_task(t.id))
    assert seen["report"]["summary"] == long_summary
    assert seen["helpers"] == ("echo shard\n", "print(1)\n")
    p = adapter.prompts[1]
    assert "`a3runs.sh`" in p and "`tools/enr_phase.py`" in p and "cut at 6000 characters" in p


def test_resumed_prompt_lists_files_both_sides_changed_that_git_merged_cleanly(cfg, git_repo, tmp_path):
    """Q-249 (T-089): git merged scripts/demo.sh without a conflict, but the branch's earlier `--check)` case hid
    main's new `--check` flag. The prompt names such files so the worker re-reads them."""
    lines = [f"line {i}" for i in range(12)]
    (git_repo / "src").mkdir(exist_ok=True)
    (git_repo / "src" / "demo.sh").write_text("\n".join(lines) + "\n")
    _git(git_repo, "add", "-A"); _git(git_repo, "commit", "-qm", "base demo.sh"); _git(git_repo, "push", "-q", "origin", "main")
    mine = list(lines); mine[0] = "line 0 --check) branch case"
    adapter = FakeAdapter(files={"src/demo.sh": "\n".join(mine) + "\n", "src/other.py": "y\n"},
                          structured={"status": "done", "summary": "ok"})
    r, board = make_runner(cfg, git_repo, tmp_path, adapter)
    t = ready_task(board)
    assert r.run_task(t).status is Status.MERGE_READY
    theirs = list(lines); theirs[11] = "line 11 --check flag from main"
    _git(git_repo, "checkout", "-q", "main")
    (git_repo / "src" / "demo.sh").write_text("\n".join(theirs) + "\n")
    (git_repo / "src" / "unrelated.py").write_text("z\n")
    _git(git_repo, "add", "-A"); _git(git_repo, "commit", "-qm", "T-090 adds --check"); _git(git_repo, "push", "-q", "origin", "main")
    stored = board.get_task(t.id)
    stored.status, stored.feedback = Status.CHANGES_REQUESTED, "Reviewer: tighten the help text."
    board.update_task(stored, ["status", "feedback"])
    adapter.files = {}
    r.run_task(board.get_task(t.id))
    p = adapter.prompts[1]
    assert "## Files main and this branch both changed (merged without a conflict)" in p
    assert "- src/demo.sh (main: " in p and "T-090 adds --check" in p
    assert "src/unrelated.py" not in p.split("## Files main and this branch both changed")[1].split("##")[0]
    assert "## Merge conflicts" not in p


def test_worktree_links_bring_gitignored_shared_inputs_into_fresh_worktrees(cfg, git_repo, tmp_path):
    """Q-250, Q-252, Q-254, Q-258, Q-261, Q-266, Q-274, Q-278: demo manifests are committed but the clip and stems are
    gitignored, so every worktree failed with 'no demo clip' until the worker found the main checkout's copy."""
    (git_repo / ".gitignore").write_text("eval/demo/*/clip.mp4\neval/demo/*/stems/\n")
    demo = git_repo / "eval" / "demo" / "easy"
    (demo / "stems").mkdir(parents=True)
    (demo / "manifest.json").write_text('{"clip": "clip.mp4"}\n')
    _git(git_repo, "add", "-A"); _git(git_repo, "commit", "-qm", "demo manifest"); _git(git_repo, "push", "-q", "origin", "main")
    (demo / "clip.mp4").write_bytes(b"\x00clip")
    (demo / "stems" / "mix.wav").write_bytes(b"RIFF")
    cfg.worktree_links = ["eval/demo"]
    adapter = FakeAdapter(files={"src/a.py": "x\n"}, structured={"status": "done", "summary": "ok"})
    seen = {}
    orig = adapter.run

    def run(spec):
        d = spec.cwd / "eval" / "demo" / "easy"
        seen["clip"] = (d / "clip.mp4").is_symlink() and (d / "clip.mp4").read_bytes() == b"\x00clip"
        seen["stem"] = (d / "stems" / "mix.wav").is_symlink()
        seen["manifest_own"] = not (d / "manifest.json").is_symlink()
        return orig(spec)
    adapter.run = run
    r, board = make_runner(cfg, git_repo, tmp_path, adapter)
    t = ready_task(board)
    r.run_task(t)
    assert seen == {"clip": True, "stem": True, "manifest_own": True}
    committed = _git(git_repo, "ls-tree", "-r", "--name-only", f"origin/{t.branch}")
    assert "src/a.py" in committed and "clip.mp4" not in committed and "mix.wav" not in committed
    assert (demo / "clip.mp4").read_bytes() == b"\x00clip"     # disposing the worktree left main's copy alone


def test_link_ignored_never_leaves_a_link_git_would_commit(git_repo, tmp_path):
    from swarm.workspace import Workspace
    (git_repo / ".gitignore").write_text("data/big.bin\n")
    (git_repo / "data").mkdir()
    (git_repo / "data" / "big.bin").write_bytes(b"1")
    ws = Workspace(git_repo, tmp_path / "wt", gh=fake_gh)
    wt = tmp_path / "wt" / "x"
    _git(git_repo, "worktree", "add", "-q", "-b", "tmp-x", str(wt))
    # .gitignore is not committed, so the worktree does not ignore data/big.bin: the link must not stay
    assert ws.link_ignored(wt, ["data"]) == [] and not (wt / "data" / "big.bin").exists()


def test_an_edited_config_reaches_a_running_runner_without_a_restart(cfg, git_repo, tmp_path, monkeypatch):
    """Field note 97: project bacc228 added `worktree_links` at 21:04; runners that had re-exec'd at 20:02 kept the
    config they loaded and ran T-110/T-112 without the demo clips (Q-286, Q-288, Q-289)."""
    import yaml
    from swarm import selfupdate
    restarts = []
    monkeypatch.setattr(selfupdate, "restart_self", lambda: restarts.append(1))
    logs = []
    r, board = make_runner(cfg, git_repo, tmp_path, FakeAdapter())
    r.log = logs.append
    assert r.maybe_reload_config() is False                     # nothing changed since load
    raw = yaml.safe_load(cfg.path.read_text())
    raw["worktree_links"] = ["eval/fixtures/demo"]
    cfg.path.write_text(yaml.safe_dump(raw))
    assert r.maybe_reload_config() is True
    assert r.cfg.worktree_links == ["eval/fixtures/demo"] and not restarts
    assert any("config reloaded (worktree_links)" in m for m in logs)
    # retro's overlay counts too
    (cfg.path.parent / "tuning.yaml").write_text(yaml.safe_dump({"skills_by_type": {"backend": ["hearing-stack"]}}))
    assert r.maybe_reload_config() is True and r.cfg.skills_by_type["backend"] == ["hearing-stack"]
    # a half-written config is ignored, logged once, and the old one stays
    cfg.path.write_text("agents: [unclosed\n")
    assert r.maybe_reload_config() is False and r.maybe_reload_config() is False
    assert sum("does not load" in m for m in logs) == 1 and r.cfg.worktree_links == ["eval/fixtures/demo"]
    # agents or hosts change: idle → restart now; busy → drain
    raw["agents"]["claude-a"]["parallel"] = 3
    cfg.path.write_text(yaml.safe_dump(raw))
    r.active["claude-a"].add("T-009")
    assert r.maybe_reload_config() is True and r.draining and not restarts
    raw["agents"]["claude-a"]["parallel"] = 1
    cfg.path.write_text(yaml.safe_dump(raw))
    r.draining = False
    r.active["claude-a"].clear()
    assert r.maybe_reload_config() is True and restarts == [1]


def test_a_resumed_attempt_does_not_refile_the_previous_attempts_harness_notes(cfg, git_repo, tmp_path):
    """Q-286/Q-288: T-110's attempt 2 re-filed two of attempt 1's three harness notes, reworded, as a new question."""
    first = [{"what": "The task text said the Mac Enrolled leak on demo-mf-easy is −21 to −25 dB. No Mac run in "
                      "eval/results shows that: whole-run is −7.9 to −9.7 dB, enrolled phase −18.8 dB.",
              "suggestion": "quote the run id"},
             {"what": "demo_regress.sh defaults --clip to the worktree's eval/fixtures/demo, which is gitignored and "
                      "missing in worktrees, so my first locked GPU run failed with rc=2."}]
    second = [{"what": "The task text says the Mac Enrolled leak on demo-mf-easy is −21 to −25 dB; no Mac run in "
                       "eval/results shows that (whole run −7.9 to −9.7 dB, enrolled phase −18.8 dB)."},
              {"what": "demo_regress defaults --clip to the worktree's eval/fixtures/demo, which is gitignored and "
                       "missing in worktrees. Attempt 1's first locked GPU run failed with rc=2."},
              {"what": "T-111 (main) and T-110 both edited the same srun/launch lines in scripts/gpu_up.sh in the same "
                       "milestone, which caused this attempt's merge conflict."}]
    adapter = FakeAdapter(files={"src/a.py": "x\n"},
                          structured={"status": "done", "summary": "s", "harness_feedback": first})
    r, board = make_runner(cfg, git_repo, tmp_path, adapter)
    t = ready_task(board)
    r.run_task(t)
    adapter.structured = {"status": "done", "summary": "s2", "harness_feedback": second}
    adapter.files = {"src/a.py": "y\n"}
    again = board.get_task(t.id)
    again.status = Status.CHANGES_REQUESTED
    board.update_task(again, ["status"])
    r.run_task(board.get_task(t.id))
    notes = [q for q in board.list_questions() if q.kind == "harness"]
    assert len(notes) == 2
    assert notes[0].text.startswith(f"[harness] {t.id}: 2 notes")
    assert notes[1].text.startswith(f"[harness] {t.id}: 1 note ") and "gpu_up.sh" in notes[1].context
    assert "Mac Enrolled" not in notes[1].context
    # the resumed prompt says the earlier notes are filed already
    assert "harness_feedback` is already on the board" in adapter.prompts[1]


def test_heartbeat_thread_keeps_beating_while_the_cli_is_silent_and_the_loop_is_blocked(cfg, git_repo, tmp_path):
    """T-115 (Oct 6) was reaped as 'worker heartbeat stale' at attempt 4 while its worker sat mid-measurement. The
    beat now runs on its own thread: neither a silent CLI (a worker waiting on a remote eval) nor a main loop that
    does not tick stops it, and the row names the running task."""
    import threading
    import time as _time
    from concurrent.futures import ThreadPoolExecutor
    release, started = threading.Event(), threading.Event()

    class Silent(FakeAdapter):
        def run(self, spec):
            started.set()
            release.wait(20)          # no output, no tool calls: a long remote GPU run
            return super().run(spec)

    cfg.heartbeat_seconds = 1
    r, board = make_runner(cfg, git_repo, tmp_path, Silent(files={"src/a.py": "x"},
                                                           structured={"status": "done", "summary": "s"}))
    r.executor = ThreadPoolExecutor(max_workers=1)
    r.HEARTBEAT_STEP_S = 0.05
    t = ready_task(board)
    try:
        r.start_heartbeat()
        assert r.tick() == 1          # the only tick: the main loop never runs again in this test
        assert started.wait(30)
        first = board.get_agent("codex-a").last_heartbeat
        _time.sleep(2.5)
        row = board.get_agent("codex-a")
        assert row.last_heartbeat > first and row.status == "running" and row.current_task == t.id
    finally:
        release.set()
        r.executor.shutdown(wait=True)
        r.stop_heartbeat()
    assert not r.heartbeat_running()


def test_heartbeat_thread_logs_a_board_failure_once_and_keeps_trying(cfg, git_repo, tmp_path):
    import time as _time
    logs = []
    r, board = make_runner(cfg, git_repo, tmp_path, FakeAdapter())
    r.log = logs.append
    r.HEARTBEAT_STEP_S = 0.02
    cfg.heartbeat_seconds = 0
    calls = {"n": 0}
    real = board.upsert_agent

    def flaky(row):
        calls["n"] += 1
        if calls["n"] <= 5:
            raise RuntimeError("Notion 0 transport")
        return real(row)
    board.upsert_agent = flaky
    r.start_heartbeat()
    deadline = _time.monotonic() + 10
    while calls["n"] < 8 and _time.monotonic() < deadline:
        _time.sleep(0.02)
    r.stop_heartbeat()
    assert sum("heartbeat failed" in m for m in logs) == 1
    assert any("heartbeat writes again" in m for m in logs)
    assert board.get_agent("codex-a").last_heartbeat is not None


def test_the_agent_row_says_running_as_soon_as_a_run_is_claimed(cfg, git_repo, tmp_path):
    """T-117 (Oct 7): `swarm status` showed claude-a2 idle while its runner had already claimed the task and
    started the CLI; the row waited for the next beat."""
    seen = {}
    r, board = make_runner(cfg, git_repo, tmp_path, None)

    class Peek(FakeAdapter):
        def run(self, spec):
            row = board.get_agent("codex-a")
            seen["row"] = (row.status, row.current_task)
            return super().run(spec)
    r.adapter_factory = lambda a: Peek(files={"src/a.py": "x"}, structured={"status": "done", "summary": "s"})
    r.heartbeat(force=True)                       # idle row from the last beat
    t = ready_task(board)
    assert r.tick() == 1
    assert seen["row"] == ("running", t.id)


def test_a_cli_left_by_a_killed_runner_is_stopped_on_start(cfg, git_repo, tmp_path):
    """T-117 (Oct 7): the runner was killed by hand 5 s after starting the CLI; the CLI (own session) kept running
    in a deleted worktree for its whole budget while the restarted runner resumed the task with a second CLI."""
    import json as _json
    import os
    import threading
    r, board = make_runner(cfg, git_repo, tmp_path, FakeAdapter())
    orphan = subprocess.Popen(["sleep", "60"], start_new_session=True)
    other = subprocess.Popen(["sleep", "60"], start_new_session=True)       # a CLI of another live runner
    live_runner = subprocess.Popen(["sleep", "60"])
    dead_runner = subprocess.Popen(["true"]); dead_runner.wait()
    reaper = threading.Thread(target=orphan.wait, daemon=True); reaper.start()
    try:
        t = board.create_task(Task(id="", title="orphan", status=Status.RUNNING, agent="codex-a", claim_nonce="x"))
        rec = r.log_dir / t.id / "cli-codex-a.pid"
        rec.parent.mkdir(parents=True)
        rec.write_text(_json.dumps({"pid": orphan.pid, "runner_pid": dead_runner.pid, "task": t.id}))
        keep = r.log_dir / "T-099" / "cli-codex-a.pid"
        keep.parent.mkdir(parents=True)
        keep.write_text(_json.dumps({"pid": other.pid, "runner_pid": live_runner.pid, "task": "T-099"}))
        assert r.stop_orphan_clis(grace_s=5) == [t.id]
        reaper.join(5)
        assert orphan.poll() is not None and not rec.exists()
        assert other.poll() is None and keep.exists()
        assert r.recover_orphans() == 1 and board.get_task(t.id).status is Status.READY
    finally:
        for p in (orphan, other, live_runner):
            if p.poll() is None:
                p.kill(); p.wait()


def test_the_cli_pid_file_lives_exactly_as_long_as_the_cli(cfg, git_repo, tmp_path):
    import json as _json
    seen = {}
    r, board = make_runner(cfg, git_repo, tmp_path, None)

    class Starts(FakeAdapter):
        def run(self, spec):
            spec.on_start(424242)
            f = r.log_dir / spec.env["SWARM_TASK_ID"] / "cli-codex-a.pid"
            seen["rec"] = _json.loads(f.read_text())
            return super().run(spec)
    r.adapter_factory = lambda a: Starts(files={"src/a.py": "x"}, structured={"status": "done", "summary": "s"})
    t = ready_task(board)
    r.run_task(t)
    assert seen["rec"]["pid"] == 424242 and seen["rec"]["task"] == t.id
    assert not (r.log_dir / t.id / "cli-codex-a.pid").exists()


def test_stop_ends_the_cli_at_the_next_poll_instead_of_waiting_for_it(cfg, git_repo, tmp_path):
    """SIGTERM used to wait for every CLI to finish on its own, so restarts by hand ended in kill -9 (T-117)."""
    r, board = make_runner(cfg, git_repo, tmp_path, FakeAdapter())
    t = ready_task(board, status=Status.RUNNING, claim_nonce="n1")
    lost = r._claim_watch(t)
    assert lost() is False
    r.stop()
    assert lost() is True


def _session_sleeper():
    import threading
    p = subprocess.Popen(["sleep", "60"], start_new_session=True)
    threading.Thread(target=p.wait, daemon=True).start()      # reap it as soon as it dies
    return p


def test_sigterm_and_exit_stop_the_worker_clis_process_groups(cfg, git_repo, tmp_path):
    """Q-302: killing the runner never reached its CLI (own session). The SIGTERM handler and an atexit hook now
    signal every live CLI's process group."""
    import signal
    import time as _time
    from swarm.runner import InFlight
    r, board = make_runner(cfg, git_repo, tmp_path, FakeAdapter())
    captured = {}
    r.install_signal_handlers(signal_fn=lambda sig, h: captured.setdefault(sig, h))
    cli = _session_sleeper()
    r.runs["T-001"] = InFlight("T-001", "codex-a", "S", utcnow(), pid=cli.pid, phase="cli")
    try:
        captured[signal.SIGTERM](signal.SIGTERM, None)
        deadline = _time.monotonic() + 5
        while cli.poll() is None and _time.monotonic() < deadline:
            _time.sleep(0.05)
        assert r._stopping is True and cli.poll() is not None
        cli2 = _session_sleeper()
        r.runs["T-002"] = InFlight("T-002", "codex-a", "S", utcnow(), pid=cli2.pid, phase="cli")
        r._kill_clis_at_exit()
        deadline = _time.monotonic() + 5
        while cli2.poll() is None and _time.monotonic() < deadline:
            _time.sleep(0.05)
        assert cli2.poll() is not None
    finally:
        for p in (cli,) + ((cli2,) if "cli2" in locals() else ()):
            if p.poll() is None:
                p.kill()


def test_no_second_worker_starts_while_an_earlier_cli_is_alive_in_the_worktree(cfg, git_repo, tmp_path):
    """Q-302 / T-117: the relaunched runner recovered the task and started a second worker in the same worktree.
    A live CLI recorded in `.swarm-run/cli.pid` blocks the start (when another live runner owns it) or is stopped
    first (when its runner is gone)."""
    import json as _json
    import os
    r, board = make_runner(cfg, git_repo, tmp_path, None)
    calls = []

    class Count(FakeAdapter):
        def run(self, spec):
            calls.append(spec)
            return super().run(spec)
    r.adapter_factory = lambda a: Count(files={"src/a.py": "x"}, structured={"status": "done", "summary": "s"})
    t = ready_task(board, flags=["resume"])
    wt = r.ws.provision(t.id)
    (wt / "half.txt").write_text("the old CLI's work\n")
    cli = _session_sleeper()
    owner = subprocess.Popen(["sleep", "60"])                  # its runner, still alive
    try:
        (wt / ".swarm-run" / "cli.pid").write_text(_json.dumps({"pid": cli.pid, "runner_pid": owner.pid}))
        out = r.run_task(board.get_task(t.id))
        s = board.get_task(t.id)
        assert not calls and out.status is Status.READY and s.status is Status.READY and s.claim_nonce == ""
        assert str(cli.pid) in s.last_error and (wt / "half.txt").exists() and cli.poll() is None
        assert r.tick() == 0                                   # not claimed again while the CLI may be alive
        # its runner is gone: the CLI is stopped and the run goes ahead
        owner.kill(); owner.wait()
        r._cli_busy.clear()
        out = r.run_task(board.get_task(t.id))
        assert calls and cli.poll() is not None and out.status is Status.MERGE_READY
        assert not (r.ws.worktree_path(t.id) / ".swarm-run" / "cli.pid").exists()
    finally:
        for p in (cli, owner):
            if p.poll() is None:
                p.kill()


def test_the_worktree_pid_file_is_written_while_the_cli_runs_and_never_carried(cfg, git_repo, tmp_path):
    import json as _json
    seen = {}
    r, board = make_runner(cfg, git_repo, tmp_path, None)

    class Starts(FakeAdapter):
        def run(self, spec):
            spec.on_start(434343)
            seen["rec"] = _json.loads((spec.cwd / ".swarm-run" / "cli.pid").read_text())
            return super().run(spec)
    r.adapter_factory = lambda a: Starts(files={"src/a.py": "x"}, structured={"status": "done", "summary": "s"})
    t = ready_task(board)
    r.run_task(t)
    assert seen["rec"]["pid"] == 434343
    from swarm.runner import CARRY_SKIP
    assert "cli.pid" in CARRY_SKIP


Q313 = ("Name Call matches the name fuzzily so that Whisper's misspellings count: 'Ariane', 'Arian' and 'Ryan' all "
        "trigger for 'Aryan'. A room where someone named Ryan talks will trigger it.")
Q315 = ("Name Call matches the name fuzzily so that Whisper's misspellings count: 'Ariane', 'Arian' and 'Ryan' all "
        "trigger for 'Aryan'. Someone named Ryan talking in the room will trigger it.")


def test_an_attempt_sees_earlier_answers_and_a_repeated_fyi_is_not_filed_again(cfg, git_repo, tmp_path):
    """T-120 (Oct 7): attempt 2 re-asked attempt 1's fyi as Q-315 because Q-313's answer (add an exclude list and
    hotwords) had not reached its prompt, then merged before the answer landed."""
    from swarm.models import Question
    adapter = FakeAdapter(files={"src/a.py": "x"},
                          structured={"status": "done", "summary": "s",
                                      "question": {"kind": "fyi", "text": Q315, "proceeding_with": "keep fuzzy"}})
    r, board = make_runner(cfg, git_repo, tmp_path, adapter)
    t = ready_task(board)
    other = ready_task(board, title="Other")
    board.create_question(Question(id="", text=Q313, kind="fyi", task_id=t.id, proceeding_with="keep fuzzy",
                                   status="Applied", answer="Keep fuzzy, add control.name.exclude and hotwords."))
    board.create_question(Question(id="", text="Which port?", kind="fyi", task_id=other.id, answer="8080 please"))
    board.create_question(Question(id="", text="[harness] a note", kind="harness", task_id=t.id, answer="fixed"))
    assert r.run_task(t).status is Status.MERGE_READY
    prompt = adapter.prompts[0]
    assert "## Questions this task already asked, with the answers so far" in prompt
    assert "control.name.exclude and hotwords" in prompt and "You proceeded with: keep fuzzy" in prompt
    assert "8080 please" not in prompt and "[harness] a note" not in prompt
    assert [q.kind for q in board.list_questions() if q.task_id == t.id].count("fyi") == 1


def test_an_unanswered_question_is_listed_as_open_and_a_new_question_is_still_filed(cfg, git_repo, tmp_path):
    from swarm.models import Question
    adapter = FakeAdapter(files={"src/a.py": "x"},
                          structured={"status": "done", "summary": "s",
                                      "question": {"kind": "fyi", "text": "The reference clip is 48 kHz but the "
                                                   "fixtures are 16 kHz; I resampled the reference once at load.",
                                                   "proceeding_with": "resample at load"}})
    r, board = make_runner(cfg, git_repo, tmp_path, adapter)
    t = ready_task(board)
    board.create_question(Question(id="", text=Q313, kind="fyi", task_id=t.id, proceeding_with="keep fuzzy"))
    r.run_task(t)
    assert "Not answered yet" in adapter.prompts[0]
    assert len([q for q in board.list_questions() if q.task_id == t.id]) == 2


def test_a_repeated_blocking_question_waits_on_the_open_one_and_an_answered_one_is_asked_again(cfg, git_repo, tmp_path):
    from swarm.models import Question
    text = "The acceptance needs the A100 but the iLab job is down and srun rejects the request; wait or cut?"
    adapter = FakeAdapter(files={"src/a.py": "x"},
                          structured={"status": "blocked", "summary": "s",
                                      "question": {"kind": "blocking", "text": text, "options": ["wait", "cut"]}})
    r, board = make_runner(cfg, git_repo, tmp_path, adapter)
    t = ready_task(board)
    first = board.create_question(Question(id="", text=text, kind="blocking", task_id=t.id))
    assert r.run_task(t).status is Status.BLOCKED
    assert [q.id for q in board.list_questions() if q.task_id == t.id] == [first.id]
    stored = board.questions[first.id]
    stored.status, stored.answer = "Applied", "wait"
    t2 = board.get_task(t.id)
    t2.status = Status.READY
    board.update_task(t2, ["status"])
    assert r.run_task(board.get_task(t.id)).status is Status.BLOCKED
    assert len([q for q in board.list_questions() if q.task_id == t.id]) == 2


def test_scratch_files_and_out_of_scope_lists():
    from swarm.feedback import (DEFAULT_SCRATCH_PATTERNS, out_of_scope_edits, scope_lint_feedback, scope_lint_lines,
                                scratch_files)
    added = ["scratch.py", "patch_judge_x.js", "src/fix.orig", "src/tmp_a.py", "test_tmp_b.py", "debug.log",
             "src/debug_view.py", "tool.py", "src/real.py", "docs/notes.md", "eval/pkg/scratchpad.md"]
    got = scratch_files(added, DEFAULT_SCRATCH_PATTERNS, ["src/real.py"])
    assert got == ["scratch.py", "patch_judge_x.js", "src/fix.orig", "src/tmp_a.py", "test_tmp_b.py", "debug.log",
                   "src/debug_view.py", "tool.py", "eval/pkg/scratchpad.md"]
    # a scope that names the file makes it intended; a top-level .py is only scratch at the top level
    assert scratch_files(["scratch.py", "tool.py", "src/ok.py"], DEFAULT_SCRATCH_PATTERNS, ["scratch.py", "tool.py"]) == []
    assert scratch_files(["src/ok.py", "docs/a.md"], DEFAULT_SCRATCH_PATTERNS, []) == []
    assert scratch_files(["scratch.py"], [], []) == []
    assert scratch_files(["a.py", "x/b.py"], ["/*.py"], []) == ["a.py"]

    changed = ["src/a.py", "eval/pipeline_eval.py", "docs/decisions/T-1.md", "web/x.js", "scripts/run.sh", "scratch.py"]
    out = out_of_scope_edits(changed, ["src/**"], named_in="Edit web/x.js; see run.sh in notes",
                             exempt=("docs/decisions/",), skip=["scratch.py"])
    assert out == ["eval/pipeline_eval.py"]   # web/x.js is named by path, scripts/run.sh by basename
    assert out_of_scope_edits(changed, [], named_in="") == []
    lines = scope_lint_lines(["scratch.py"], ["eval/pipeline_eval.py"])
    assert lines[0] == "scratch files committed: scratch.py"
    assert lines[1].startswith("out-of-scope edits: eval/pipeline_eval.py (")
    assert scope_lint_feedback(lines).startswith("Harness notes (not blocking):\n- scratch files committed")
    assert scope_lint_feedback([]) == ""


def test_publish_surfaces_scratch_and_out_of_scope_without_blocking(cfg, git_repo, tmp_path):
    adapter = FakeAdapter(files={"src/a.py": "x = 1\n", "scratch.py": "print(1)\n", "patch_judge_1.js": "//\n",
                                 "eval/pipeline_eval.py": "y = 2\n", "docs/named.md": "z\n"},
                          structured={"status": "done", "summary": "did it",
                                      "notes_for_reviewer": "outside scope: docs/named.md because acceptance"})
    r, board = make_runner(cfg, git_repo, tmp_path, adapter)
    t = ready_task(board, agent="claude-a", model="sonnet", effort="medium", importance="high")
    assert r.run_task(t).status is Status.REVIEW   # not blocked
    stored = board.get_task(t.id)
    assert stored.feedback == (
        "Harness notes (not blocking):\n- scratch files committed: patch_judge_1.js, scratch.py\n"
        "- out-of-scope edits: eval/pipeline_eval.py (outside the task's scope and not named in its description, "
        "acceptance or the worker's notes_for_reviewer as `outside scope: <file> because <criterion>`)")
    from swarm.reviewer import build_review_prompt
    prompt = build_review_prompt(stored, "diff", "ok", "RULES")
    assert "## Harness notes (not blocking)" in prompt and "scratch files committed: patch_judge_1.js, scratch.py" in prompt
    assert "out-of-scope edits: eval/pipeline_eval.py" in prompt
    assert "Harness notes" not in build_review_prompt(Task(id="T-9", title="x"), "d", "v", "R")


def test_scope_lint_knob_turns_scratch_check_off(cfg, git_repo, tmp_path):
    cfg.verify.scratch_patterns = []
    adapter = FakeAdapter(files={"src/a.py": "x = 1\n", "scratch.py": "print(1)\n"},
                          structured={"status": "done", "summary": "did it", "notes_for_reviewer": "outside scope: scratch.py"})
    r, board = make_runner(cfg, git_repo, tmp_path, adapter)
    t = ready_task(board, agent="claude-a", model="sonnet", effort="medium", importance="high")
    assert r.run_task(t).status is Status.REVIEW
    assert board.get_task(t.id).feedback == ""


def test_scratch_patterns_config_key(project_dir):
    from swarm.config import ConfigError, load_config
    path = project_dir / ".swarm" / "config.yaml"
    assert load_config(path).verify.scratch_patterns is None
    base = path.read_text()
    path.write_text(base + "\nverify:\n  scratch_patterns: ['x*']\n")
    try:
        assert load_config(path).verify.scratch_patterns == ["x*"]
    except ConfigError:
        pass   # the fixture config already has a verify block: covered by the runner test above
    finally:
        path.write_text(base)


def test_report_markdown_lists_harness_notes():
    from swarm.report import report_to_markdown
    r = Report(status="done", summary="s")
    md = report_to_markdown(r, attempt=1, verify_ok=True, verify_tail="", pr_url="", flags=[],
                            harness_notes=["scratch files committed: scratch.py"])
    assert "Harness notes (not blocking):\n- scratch files committed: scratch.py" in md
    assert "Harness notes" not in report_to_markdown(r, attempt=1, verify_ok=True, verify_tail="", pr_url="", flags=[])
