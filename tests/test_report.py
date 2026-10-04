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


def test_tools_used_flow_into_the_decision_log_and_report(tmp_path):
    from swarm.models import Task
    from swarm.report import REPORT_SCHEMA, decisions_markdown, report_to_markdown
    assert "tools_used" in REPORT_SCHEMA["properties"]
    r = parse_report({"status": "done", "summary": "s",
                      "tools_used": [{"name": "playwright", "kind": "mcp", "helped": True,
                                      "note": "verified the page headless, no console errors"},
                                     {"name": "frontend-design", "kind": "skill", "helped": True}]},
                     tmp_path, changed_files=["a"])
    assert [t["name"] for t in r.tools_used] == ["playwright", "frontend-design"]
    dec = decisions_markdown(Task(id="T-9", title="Page"), r)
    assert "Tools used" in dec and "playwright (mcp, helped)" in dec and "no console errors" in dec
    md = report_to_markdown(r, attempt=1, verify_ok=True, verify_tail="", pr_url="", flags=[])
    assert "playwright" in md
    plain = parse_report({"status": "done", "summary": "s", "tools_used": ["context7", {"name": "x"}, 7]},
                         tmp_path, changed_files=["a"])
    assert [t["name"] for t in plain.tools_used] == ["context7", "x"]
    assert decisions_markdown(Task(id="T-9", title="Page"), parse_report(
        {"status": "done", "summary": "s"}, tmp_path, changed_files=["a"])) == ""


def test_synthesized_report_keeps_the_real_error(tmp_path):
    """A spawn failure said 'Report missing and no files changed'; the CLI's own error must survive."""
    r = parse_report(None, tmp_path, changed_files=[], error="cli not found: claude")
    assert r.synthesized and "cli not found: claude" in r.summary


def test_harness_feedback_is_parsed_logged_and_becomes_a_board_note(tmp_path):
    from swarm.report import REPORT_SCHEMA, harness_feedback_question
    assert "harness_feedback" in REPORT_SCHEMA["properties"]
    r = parse_report({"status": "done", "summary": "ok",
                      "harness_feedback": [{"what": "verify_fast.sh needs ruff but the venv lacks it",
                                            "suggestion": "add ruff to the dev extra"},
                                           "the hearing-stack skill says 48 kHz for DFN but io.py has no resampler",
                                           {"what": ""}]},
                     tmp_path, changed_files=["a.py"])
    assert len(r.harness_feedback) == 2
    assert r.harness_feedback[0]["suggestion"] == "add ruff to the dev extra"
    assert r.harness_feedback[1]["what"].startswith("the hearing-stack skill")
    task = Task(id="T-007", title="DFN3 stage", type="ml_audio", importance="high", size="M", status="Running",
                agent="claude-a")
    q = harness_feedback_question(task, r)
    assert q["kind"] == "fyi" and q["text"].startswith("[harness] T-007")
    assert "ruff" in q["context"] and "resampler" in q["context"]
    md = decisions_markdown(task, r)
    assert "Harness feedback" in md and "add ruff" in md
    empty = parse_report({"status": "done", "summary": "ok"}, tmp_path, changed_files=["a.py"])
    assert empty.harness_feedback == [] and harness_feedback_question(task, empty) is None
