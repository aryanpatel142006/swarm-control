from datetime import datetime, timezone

from swarm.board.notion_props import (
    TASKS_SCHEMA,
    p_title,
    agent_to_props,
    p_date,
    p_rich,
    p_select,
    page_to_agent,
    page_to_question,
    page_to_task,
    question_to_props,
    r_date,
    r_rich,
    r_select,
    task_to_props,
)
from swarm.models import AgentRow, Question, Status, Task


def _page(pid, props):
    return {"id": pid, "properties": {k: {**v, "type": next(iter(v))} for k, v in props.items()}}


def test_p_rich_chunks_long_text():
    long = "x" * 4500
    prop = p_rich(long)
    assert [len(c["text"]["content"]) for c in prop["rich_text"]] == [2000, 2000, 500]
    assert r_rich(prop) == long


def test_p_rich_empty_and_select_none():
    assert p_rich("") == {"rich_text": []}
    assert p_select("") == {"select": None}
    assert r_select({"select": None}) == ""


def test_date_roundtrip():
    dt = datetime(2026, 10, 10, 16, 0, tzinfo=timezone.utc)
    assert r_date(p_date(dt)) == dt
    assert p_date(None) == {"date": None} and r_date({"date": None}) is None


def test_task_roundtrip():
    t = Task(id="T-012", title="Degraded state", description="d", acceptance="a", type="frontend",
             importance="high", size="M", milestone="M1", priority=5, agent="claude-a", model="opus",
             effort="high", status=Status.REVIEW, depends_on=["T-003", "T-004"], scope=["frontend/**"],
             feedback="fix x", pr_url="https://github.com/x/y/pull/1", attempts=2, review_rounds=1,
             claim_nonce="abc", last_error="boom", flags=["report_missing"],
             started=datetime(2026, 10, 10, 16, 0, tzinfo=timezone.utc))
    props = task_to_props(t)
    assert props["Name"]["title"][0]["text"]["content"] == "T-012 · Degraded state"
    assert props["Status"]["select"]["name"] == "Review"
    assert props["Depends On"]["rich_text"][0]["text"]["content"] == "T-003, T-004"
    back = page_to_task(_page("pg1", props))
    assert back.page_id == "pg1" and back.id == "T-012" and back.title == "Degraded state"
    assert back.depends_on == ["T-003", "T-004"] and back.flags == ["report_missing"]
    assert back.status is Status.REVIEW and back.attempts == 2 and back.started == t.started


def test_task_partial_fields():
    t = Task(id="T-001", title="x", status=Status.RUNNING, claim_nonce="n")
    props = task_to_props(t, ["status", "claim_nonce"])
    assert set(props) == {"Status", "Claim Nonce"}


def test_question_and_agent_roundtrip():
    q = Question(id="Q-004", text="Tap or rail?", kind="fyi", context="c", options=["tap", "rail"],
                 proceeding_with="tap", impact="high", task_id="T-011", asked_by="claude-a",
                 answer="rail", needs_follow_up=True)
    props = question_to_props(q, task_page_id="pg-t011")
    assert props["Task"]["relation"] == [{"id": "pg-t011"}]
    back = page_to_question(_page("pgq", props))
    assert back.id == "Q-004" and back.options == ["tap", "rail"] and back.needs_follow_up is True
    a = AgentRow(name="claude-a", provider="claude", host="h", status="cooldown", runs=4, cost_usd=1.5,
                 cooldown_until=datetime(2026, 10, 10, 16, 0, tzinfo=timezone.utc))
    aback = page_to_agent(_page("pga", agent_to_props(a)))
    assert aback.name == "claude-a" and aback.runs == 4 and aback.cooldown_until == a.cooldown_until


def test_tasks_schema_has_agent_options():
    schema = TASKS_SCHEMA(["claude-a", "codex-a"])
    names = [o["name"] for o in schema["Agent"]["select"]["options"]]
    assert names == ["claude-a", "codex-a"]
    assert "Backlog" in [o["name"] for o in schema["Status"]["select"]["options"]]


def test_page_without_id_has_empty_id():
    t = page_to_task(_page("pg", {"Name": p_title("Hand made"), "Status": p_select("Ready")}))
    assert t.id == "" and t.title == "Hand made" and t.status is Status.READY
    assert page_to_task(_page("pg2", {"Name": p_title("T-005 · Old style")})).id == "T-005"


def test_select_options_carry_colors():
    schema = TASKS_SCHEMA(["claude-a"])
    status = {o["name"]: o.get("color") for o in schema["Status"]["select"]["options"]}
    assert status["Blocked"] == "red" and status["Failed"] == "red" and status["Done"] == "green"
    assert status["Running"] == "yellow" and status["Ready"] == "blue" and status["Backlog"] == "gray"
    imp = {o["name"]: o.get("color") for o in schema["Importance"]["select"]["options"]}
    assert imp["critical"] == "red" and imp["low"] == "gray"
    from swarm.board.notion_props import AGENTS_SCHEMA, QUESTIONS_SCHEMA
    ag = {o["name"]: o.get("color") for o in AGENTS_SCHEMA["Status"]["select"]["options"]}
    assert ag["offline"] == "red" and ag["running"] == "green" and ag["cooldown"] == "yellow"
    q = {o["name"]: o.get("color") for o in QUESTIONS_SCHEMA("ds")["Kind"]["select"]["options"]}
    assert q["blocking"] == "red" and q["fyi"] == "blue"
