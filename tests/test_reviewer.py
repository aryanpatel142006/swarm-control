import json

from swarm.board.memory import InMemoryBoard
from swarm.models import RunResult, Status, Task, Usage
from swarm.reviewer import Reviewer, build_review_prompt, parse_verdict
from swarm.workspace import CmdResult, Workspace


class FakeReviewAdapter:
    def __init__(self, structured):
        self.structured = structured
        self.specs = []
        self.prompts = []

    def run(self, spec):
        self.specs.append(spec)
        self.prompts.append(spec.prompt_file.read_text())
        return RunResult(ok=True, exit_code=0, stdout="", stderr="", structured_output=self.structured,
                         usage=Usage())


def setup(cfg, git_repo, tmp_path, structured, verify_full_ok=True):
    board = InMemoryBoard()
    ws = Workspace(git_repo, tmp_path / "wt", gh=lambda a, c: CmdResult(0, "", ""))
    t = board.create_task(Task(id="", title="Cards", type="frontend", importance="high", status=Status.REVIEW,
                               acceptance="- renders", agent="claude-a", scope=["src/**"]))
    wt = ws.provision(t.id)
    (wt / "src").mkdir()
    (wt / "src" / "c.py").write_text("x = 1\n")
    (wt / "scripts" / "verify_full.sh").write_text(
        "#!/bin/sh\nexit 0\n" if verify_full_ok else "#!/bin/sh\necho FULLFAIL; exit 1\n")
    ws.commit_all(wt, "change")
    ws.push(wt, t.branch)
    ws.dispose(wt)
    adapter = FakeReviewAdapter(structured)
    rev = Reviewer(cfg, board, ws, adapter_factory=lambda a: adapter, log=lambda *a: None,
                   prompt_text="REVIEW RULES")
    return rev, board, t, adapter


def test_parse_verdict_variants(tmp_path):
    v = parse_verdict({"verdict": "approve", "summary": "fine", "findings": []}, tmp_path)
    assert v.verdict == "approve"
    (tmp_path / ".swarm-run").mkdir()
    (tmp_path / ".swarm-run" / "review.json").write_text(json.dumps(
        {"verdict": "request_changes", "summary": "s", "findings": [{"severity": "high", "issue": "x"}]}))
    v2 = parse_verdict(None, tmp_path)
    assert v2.verdict == "request_changes" and v2.findings[0]["issue"] == "x"
    v3 = parse_verdict({"verdict": "nonsense"}, tmp_path / "nowhere")
    assert v3.verdict == "escalate"


def test_prompt_contains_pieces():
    t = Task(id="T-1", title="Cards", acceptance="- renders", scope=["src/**"])
    p = build_review_prompt(t, "diff --git a", "verify tail", "RULES")
    assert "RULES" in p and "- renders" in p and "diff --git a" in p and "verify tail" in p and "T-1" in p


def test_approve_goes_merge_ready(cfg, git_repo, tmp_path):
    rev, board, t, adapter = setup(cfg, git_repo, tmp_path, {"verdict": "approve", "summary": "good", "findings": []})
    out = rev.process(t)
    assert out.status is Status.MERGE_READY and board.get_task(t.id).status is Status.MERGE_READY
    assert adapter.specs[0].read_only is True and adapter.specs[0].model == "gpt-6-sol"
    assert "REVIEW RULES" in adapter.prompts[0] and "x = 1" in adapter.prompts[0]
    assert board.reports[t.id][0][0] == "Review — round 1"


def test_request_changes_then_escalate(cfg, git_repo, tmp_path):
    rev, board, t, adapter = setup(cfg, git_repo, tmp_path, {
        "verdict": "request_changes", "summary": "nope",
        "findings": [{"severity": "high", "file": "src/c.py", "line": 1, "issue": "wrong", "fix": "make right"}]})
    out = rev.process(t)
    stored = board.get_task(t.id)
    assert out.status is Status.CHANGES_REQUESTED and stored.review_rounds == 1
    assert "src/c.py" in stored.feedback and "make right" in stored.feedback and "resume" in stored.flags
    stored.status = Status.REVIEW
    board.update_task(stored, ["status"])
    rev.process(board.get_task(t.id))
    stored2 = board.get_task(t.id)
    assert stored2.status is Status.CHANGES_REQUESTED and stored2.review_rounds == 2
    stored2.status = Status.REVIEW
    board.update_task(stored2, ["status"])
    rev.process(board.get_task(t.id))
    assert board.get_task(t.id).status is Status.BLOCKED
    assert board.list_questions(status="Open")[0].kind == "blocking"


def test_verify_full_failure_skips_model(cfg, git_repo, tmp_path):
    rev, board, t, adapter = setup(cfg, git_repo, tmp_path, {"verdict": "approve", "summary": "x"},
                                   verify_full_ok=False)
    out = rev.process(t)
    assert out.status is Status.CHANGES_REQUESTED and "FULLFAIL" in board.get_task(t.id).feedback
    assert adapter.specs == []


def test_escalate_creates_question(cfg, git_repo, tmp_path):
    rev, board, t, adapter = setup(cfg, git_repo, tmp_path,
                                   {"verdict": "escalate", "summary": "task conflicts with contracts"})
    assert rev.process(t).status is Status.BLOCKED
    assert "conflicts" in board.list_questions()[0].text


def test_reviewer_run_is_recorded_in_the_ledger(cfg, git_repo, tmp_path):
    from swarm.usage import Ledger
    rev, board, t, adapter = setup(cfg, git_repo, tmp_path, {"verdict": "approve", "summary": "good", "findings": []})
    ledger = Ledger(tmp_path / "usage.jsonl")
    rev.ledger = ledger
    rev.process(t)
    assert ledger.totals("codex-a").input_tokens == 0 and len(ledger._rows()) == 1
    assert ledger._rows()[0]["task"] == t.id and ledger._rows()[0]["model"] == "gpt-6-sol"


def test_rate_limited_reviewer_defers_instead_of_blocking(cfg, git_repo, tmp_path):
    """Oct 5 2026: the reviewer hit the Claude session limit and escalated T-022 as if the code were bad."""
    from swarm.reviewer import Reviewer, Verdict
    from swarm.board.memory import InMemoryBoard
    from swarm.workspace import Workspace
    from swarm.models import Status, Task
    board = InMemoryBoard()
    t = board.create_task(Task(id="", title="x", status=Status.REVIEW, agent="claude-a", review_rounds=0))
    rv = Reviewer(cfg, board, Workspace(git_repo, tmp_path / "wt"), log=lambda *a: None)
    rv.apply(t, Verdict("defer", "reviewer rate limited; retry later", []))
    fresh = board.get_task(t.id)
    assert fresh.status is Status.REVIEW and fresh.review_rounds == 0 and not board.list_questions()


def test_long_reviews_keep_every_finding_with_its_text():
    """Q-080: a flat [:1900] cut delivered '- [medium] hearing/tse/av_mossformer.py:1' with no text at all."""
    from swarm.reviewer import Verdict, findings_to_feedback
    long = "x" * 900
    v = Verdict("request_changes", "s" * 700, [
        {"severity": "high", "file": "tests/a.py", "line": 50, "issue": long, "fix": long},
        {"severity": "high", "file": "b.py", "issue": long, "fix": long},
        {"severity": "medium", "file": "hearing/tse/av_mossformer.py", "line": 1, "issue": "MODELS.md row missing",
         "fix": "add the row"},
        {"severity": "low", "file": "c.py", "summary": "only a summary key"},
        {"severity": "low", "file": "d.py"},
    ])
    fb = findings_to_feedback(v, cap=2000)
    assert len(fb) <= 2000
    assert "hearing/tse/av_mossformer.py:1: MODELS.md row missing → add the row" in fb
    assert "only a summary key" in fb and "d.py: (the reviewer gave no text" in fb
    assert "…" in fb   # the long items were shortened instead


def _setup_full(cfg, git_repo, tmp_path, branch_script, main_script=None):
    import subprocess
    if main_script is not None:
        (git_repo / "scripts" / "verify_full.sh").write_text(main_script)
        subprocess.run(["git", "-C", str(git_repo), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(git_repo), "commit", "-qm", "main verify_full"], check=True)
        subprocess.run(["git", "-C", str(git_repo), "push", "-q", "origin", "main"], check=True)
    rev, board, t, adapter = setup(cfg, git_repo, tmp_path, {"verdict": "approve", "summary": "ok", "findings": []})
    wt = rev.ws.provision(t.id, reuse_branch=True)
    (wt / "scripts" / "verify_full.sh").write_text(branch_script)
    rev.ws.commit_all(wt, "branch verify_full")
    rev.ws.push(wt, t.branch, force_with_lease=True)
    rev.ws.dispose(wt)
    return rev, board, t, adapter


def test_flaky_verify_full_is_rerun_and_not_sent_back(cfg, git_repo, tmp_path):
    """Q-191/Q-189: a timing test in an untouched area failed under load and sent a docs-only task back."""
    counter = tmp_path / "runs.txt"
    script = (f'#!/bin/sh\necho x >> "{counter}"\n[ "$(wc -l < "{counter}")" -ge 2 ] && exit 0\n'
              'echo "FAILED tests/test_tts.py::test_cold_kokoro - wait_idle(2.0) timed out"; exit 1\n')
    rev, board, t, adapter = _setup_full(cfg, git_repo, tmp_path, script)
    out = rev.process(t)
    assert out.status is Status.MERGE_READY and len(adapter.prompts) == 1
    assert "failed once and passed on a rerun" in adapter.prompts[0]
    qs = [q for q in board.list_questions() if q.kind == "harness"]
    assert len(qs) == 1 and qs[0].text.startswith("[test-hygiene]") and "test_cold_kokoro" in qs[0].context


def test_verify_full_failing_on_main_too_is_not_the_tasks(cfg, git_repo, tmp_path):
    bad = '#!/bin/sh\necho "FAILED tests/test_tts.py::test_cold_kokoro - timeout"; exit 1\n'
    rev, board, t, adapter = _setup_full(cfg, git_repo, tmp_path, bad, main_script=bad)
    out = rev.process(t)
    assert out.status is Status.MERGE_READY
    assert "fails on current main as well" in adapter.prompts[0]
    qs = [q for q in board.list_questions() if q.kind == "harness"]
    assert len(qs) == 1 and "fails on main too" in qs[0].text and "test_cold_kokoro" in qs[0].text
    stored = board.get_task(t.id)                       # a second review does not file the same note again
    stored.status = Status.REVIEW
    board.update_task(stored, ["status"])
    rev.process(board.get_task(t.id))
    assert len([q for q in board.list_questions() if q.kind == "harness"]) == 1
    assert not (rev.ws.worktree_root / "_main-verify").exists()


def test_verify_full_failing_only_on_the_branch_goes_back(cfg, git_repo, tmp_path):
    bad = '#!/bin/sh\necho "FAILED tests/test_new.py::test_mine - assert 1 == 2"; exit 1\n'
    rev, board, t, adapter = _setup_full(cfg, git_repo, tmp_path, bad, main_script="#!/bin/sh\nexit 0\n")
    out = rev.process(t)
    stored = board.get_task(t.id)
    assert out.status is Status.CHANGES_REQUESTED and adapter.prompts == []
    assert "passes on current main" in stored.feedback and "test_mine" in stored.feedback
    assert [q for q in board.list_questions() if q.kind == "harness"] == []


def test_failing_tests_parses_the_short_summary():
    from swarm.reviewer import failing_tests
    out = ("....F\n=== short test summary info ===\nFAILED tests/a.py::test_x - AssertionError\n"
           "ERROR tests/b.py::test_y\nFAILED tests/c.py\n1 failed")
    assert failing_tests(out) == {"tests/a.py::test_x", "tests/b.py::test_y", "tests/c.py"}
