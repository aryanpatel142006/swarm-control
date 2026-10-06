import json
import subprocess
from datetime import timedelta

from swarm.board.memory import InMemoryBoard
from swarm.models import AgentRow, RunResult, Status, Task, Usage, utcnow
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
    restarted = []
    monkeypatch.setattr("swarm.selfupdate.restart_self", lambda: restarted.append(True))
    r.active["claude-a"].add("T-001")           # one run in flight
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
