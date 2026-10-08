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
    assert q["kind"] == "harness" and q["text"].startswith("[harness] T-007")
    assert "ruff" in q["context"] and "resampler" in q["context"]
    md = decisions_markdown(task, r)
    assert "Harness feedback" in md and "add ruff" in md
    empty = parse_report({"status": "done", "summary": "ok"}, tmp_path, changed_files=["a.py"])
    assert empty.harness_feedback == [] and harness_feedback_question(task, empty) is None


def test_reworded_notes_from_earlier_attempts_count_as_repeats_and_other_notes_do_not():
    """T-115's attempts 2-5 re-filed attempt 1's notes reworded (Q-295, Q-297, Q-300, Oct 6)."""
    from swarm.report import repeated_feedback
    earlier = [
        "scripts/gpu_up.sh documents ILAB_CPUS as a working option, but the cluster rejects every CPU flag. → remove it",
        "The task text did not say that HEARING_HQ_BACKEND=remote is needed for demo_regress to use the A100. Without "
        "it, runs silently use the Mac. → print the backend",
        "The task is sized S but needs several 60 s live runs plus a baseline, and the 20-minute limit is too short "
        "for that. → Re-plan it as M.",
    ]
    for again in ("scripts/gpu_up.sh documents ILAB_CPUS (srun --cpus-per-task) as working, but iLab rejects every "
                  "CPU flag.",
                  "demo_regress needs HEARING_HQ_BACKEND=remote as well as HEARING_REMOTE_URL. Without it, runs "
                  "silently use the Mac.",
                  "The task needs a 6-run A/B plus server restarts, but it is sized S with a 20-minute limit."):
        assert repeated_feedback({"what": again}, earlier), again
    for new in ("demo_regress has no cold-start metric; I had to score out.wav slices by hand.",
                "The task assumed a cold-start-only tier is possible on the remote ladder. The ladder runs every tier "
                "every chunk, so any CPU tier added for the first seconds costs CPU for the whole run.",
                "gpu_up.sh ilab always closes the tunnel forward in the shared state dir, so a second job for an A/B "
                "kills the live job's tunnel."):
        assert not repeated_feedback({"what": new}, earlier), new


def test_repeated_questions_match_reworded_pairs_and_not_different_questions():
    """Q-313/Q-315 (T-120), Q-306/Q-308 (T-116), Q-285/Q-287 (T-110): the same fyi asked on consecutive attempts."""
    from swarm.models import Question
    from swarm.report import earlier_questions, earlier_questions_note, repeated_question, says_the_same
    pairs = [
        ("Name Call matches the name fuzzily so that Whisper's misspellings count: 'Ariane', 'Arian' and 'Ryan' all "
         "trigger for 'Aryan'. A room where someone named Ryan talks will trigger it.",
         "Name Call matches the name fuzzily so that Whisper's misspellings count: 'Ariane', 'Arian' and 'Ryan' all "
         "trigger for 'Aryan'. Someone named Ryan talking in the room will trigger it."),
        ("Idle ladder tiers are now suspended by default on both the Mac and remote ladders. Tiers needing `healthy` "
         "(tse) and remote tiers always run. The dfn_ll cold-start bridge on the A100 is still off until a GPU A/B "
         "is run.",
         "Idle ladder tiers are now suspended by default on both the Mac and the remote ladders. Tse (`healthy`) "
         "tiers and remote tiers always run. The dfn_ll cold-start bridge on the A100 stays off until a GPU A/B is "
         "run."),
        ("The acceptance compares GPU leak to a Mac '−21 to −25 dB', which no Mac run on demo-mf-easy shows. Mac "
         "whole-run leak is −8 to −10 dB and its enrolled phase is −18.8 dB. I compared enrolled phase to enrolled "
         "phase.",
         "The acceptance compares GPU leak with a Mac '−21 to −25 dB' that no Mac run on demo-mf-easy shows. Mac "
         "whole-run leak is −8 to −10 dB and its enrolled phase is −18.8 dB, so I compared enrolled phase to "
         "enrolled phase."),
    ]
    for a, b in pairs:
        assert says_the_same(a, b) and says_the_same(b, a)
    assert not says_the_same(pairs[0][0], pairs[1][0]) and not says_the_same(pairs[1][0], pairs[2][0])
    assert not says_the_same("Which port should the judge page use?", "Which port should the API use?")

    fyi = Question(id="Q-313", text=pairs[0][0], kind="fyi", task_id="T-120", status="Applied", answer="add exclude")
    blocking_open = Question(id="Q-320", text=pairs[1][0], kind="blocking", task_id="T-120")
    blocking_done = Question(id="Q-321", text=pairs[2][0], kind="blocking", task_id="T-120", status="Applied",
                             answer="enrolled")
    noise = [Question(id="Q-9", text=pairs[0][0], kind="fyi", task_id="T-999"),
             Question(id="Q-10", text="[harness] " + pairs[0][0], kind="harness", task_id="T-120")]
    earlier = earlier_questions([blocking_done, blocking_open, fyi] + noise, "T-120")
    assert [q.id for q in earlier] == ["Q-313", "Q-320", "Q-321"]
    assert repeated_question({"kind": "fyi", "text": pairs[0][1]}, earlier).id == "Q-313"
    assert repeated_question({"kind": "fyi", "text": pairs[2][1]}, earlier).id == "Q-321"
    assert repeated_question({"kind": "blocking", "text": pairs[1][1]}, earlier).id == "Q-320"
    assert repeated_question({"kind": "blocking", "text": pairs[2][1]}, earlier) is None    # answered: ask again
    assert repeated_question({"kind": "blocking", "text": pairs[0][1]}, earlier) is None    # only an fyi matched
    note = earlier_questions_note(earlier)
    assert "**Answer:** add exclude" in note and "Not answered yet" in note
    short = earlier_questions_note(earlier, cap=400)
    assert short.startswith("(") and "Q-321" in short and "Q-313" not in short
