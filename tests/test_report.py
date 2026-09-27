import json

from swarm.models import Task
from swarm.report import (
    REPORT_SCHEMA,
    REVIEW_SCHEMA,
    debts_markdown,
    decisions_markdown,
    parse_report,
    report_to_markdown,
)


def test_schema_shape():
    assert REPORT_SCHEMA["type"] == "object" and "status" in REPORT_SCHEMA["required"]
    assert REPORT_SCHEMA["properties"]["status"]["enum"] == ["done", "blocked", "failed"]
    assert REVIEW_SCHEMA["properties"]["verdict"]["enum"] == ["approve", "request_changes", "escalate"]


def test_parse_structured(tmp_path):
    r = parse_report({"status": "done", "summary": "did it", "files_changed": ["a.py"],
                      "tests": {"command": "pytest", "passed": True},
                      "debts": [{"kind": "todo", "location": "a.py:1", "reason": "r", "fix": "f"}],
                      "decisions": [{"decision": "x", "why": "y", "impact": "high"}],
                      "question": {"kind": "fyi", "text": "ok?", "options": [], "proceeding_with": "x"}},
                     tmp_path, changed_files=["a.py"])
    assert r.status == "done" and r.debts[0]["kind"] == "todo" and r.question["kind"] == "fyi"
    assert not r.synthesized


def test_parse_from_file(tmp_path):
    (tmp_path / ".swarm-run").mkdir()
    (tmp_path / ".swarm-run" / "report.json").write_text(json.dumps(
        {"status": "blocked", "summary": "need input", "question": {"kind": "blocking", "text": "which db?"}}))
    r = parse_report(None, tmp_path, changed_files=[])
    assert r.status == "blocked" and r.question["text"] == "which db?"


def test_parse_missing_synthesizes(tmp_path):
    r = parse_report(None, tmp_path, changed_files=["x.py", "y.py"])
    assert r.synthesized and r.status == "done" and r.files_changed == ["x.py", "y.py"]
    r2 = parse_report({"garbage": True}, tmp_path, changed_files=[])
    assert r2.synthesized and r2.status == "failed"


def test_parse_bad_status_and_empty_question(tmp_path):
    r = parse_report({"status": "weird", "summary": "s", "question": {"text": ""}}, tmp_path, changed_files=["a"])
    assert r.status == "done" and r.question is None


def test_markdown_outputs(tmp_path):
    r = parse_report({"status": "done", "summary": "did it",
                      "decisions": [{"decision": "use tap", "why": "simpler", "impact": "high"}],
                      "debts": [{"kind": "mock", "location": "b.py:3", "reason": "no api", "fix": "wire it"}]},
                     tmp_path, changed_files=["b.py"])
    md = report_to_markdown(r, attempt=2, verify_ok=False, verify_tail="FAIL 1/3", pr_url="https://gh/1",
                            flags=["out_of_scope"])
    assert "attempt 2" in md.lower() and "FAIL 1/3" in md and "https://gh/1" in md
    assert "out_of_scope" in md and "use tap" in md
    t = Task(id="T-009", title="Cards")
    assert "T-009" in decisions_markdown(t, r) and "use tap" in decisions_markdown(t, r)
    assert "b.py:3" in debts_markdown(t, r)
    assert decisions_markdown(t, parse_report({"status": "done"}, tmp_path, changed_files=["a"])) == ""
