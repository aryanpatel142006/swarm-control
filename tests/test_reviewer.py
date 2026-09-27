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
