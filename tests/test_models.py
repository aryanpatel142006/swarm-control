from datetime import timezone

from swarm.models import (
    IMPORTANCES,
    SIZES,
    TASK_TYPES,
    TIERS,
    Report,
    RunResult,
    Status,
    Task,
    Usage,
    next_id,
    utcnow,
)


def test_status_values_match_spec():
    assert [s.value for s in Status] == [
        "Backlog", "Ready", "Running", "Review", "Changes Requested",
        "Merge Ready", "Blocked", "Failed", "Done", "Cut",
    ]


def test_task_defaults_and_branch():
    t = Task(id="T-007", title="Do thing")
    assert t.status is Status.BACKLOG
    assert t.branch == "task/T-007"
    assert t.title_with_id() == "T-007 · Do thing"
    assert t.depends_on == [] and t.scope == [] and t.flags == []


def test_next_id_pads_and_increments():
    assert next_id("T", []) == "T-001"
    assert next_id("T", ["T-001", "T-009", "junk"]) == "T-010"
    assert next_id("Q", ["Q-120"]) == "Q-121"


def test_constants():
    assert "ml_fusion" in TASK_TYPES and len(TASK_TYPES) == 13
    assert IMPORTANCES == ["critical", "high", "normal", "low"]
    assert SIZES == ["S", "M", "L"] and TIERS == ["best", "high", "mid", "low"]


def test_utcnow_is_aware():
    assert utcnow().tzinfo is timezone.utc


def test_report_and_runresult_defaults():
    r = Report(status="done", summary="ok")
    assert r.files_changed == [] and r.question is None and r.synthesized is False
    rr = RunResult(ok=True, exit_code=0, stdout="", stderr="")
    assert rr.usage == Usage() and rr.rate_limited is False and rr.timed_out is False
