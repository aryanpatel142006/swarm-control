from swarm.board.base import claim_task
from swarm.board.memory import InMemoryBoard
from swarm.models import AgentRow, Question, Status, Task


def test_create_assigns_ids_and_lists():
    b = InMemoryBoard()
    t1 = b.create_task(Task(id="", title="one", status=Status.READY, agent="a"))
    t2 = b.create_task(Task(id="", title="two", status=Status.BACKLOG))
    assert (t1.id, t2.id) == ("T-001", "T-002")
    assert [t.id for t in b.list_tasks(status=[Status.READY])] == ["T-001"]
    assert [t.id for t in b.list_tasks(agent=["a"])] == ["T-001"]
    assert b.list_tasks(status=[Status.DONE]) == []


def test_get_task_by_id_not_title():
    b = InMemoryBoard()
    b.create_task(Task(id="T-001", title="same"))
    b.create_task(Task(id="T-002", title="same"))
    assert b.get_task("T-002").id == "T-002"
    assert b.get_task("same") is None


def test_update_only_named_fields():
    b = InMemoryBoard()
    t = b.create_task(Task(id="", title="x", status=Status.READY))
    t.status = Status.RUNNING
    t.title = "changed locally"
    b.update_task(t, ["status"])
    stored = b.get_task(t.id)
    assert stored.status is Status.RUNNING and stored.title == "x"


def test_reports_questions_agents_status():
    b = InMemoryBoard()
    t = b.create_task(Task(id="", title="x"))
    b.append_task_report(t, "Report — attempt 1", "did stuff")
    assert b.reports[t.id][0] == ("Report — attempt 1", "did stuff")
    q = b.create_question(Question(id="", text="which?", task_id=t.id))
    assert q.id == "Q-001" and b.list_questions(status="Open")[0].text == "which?"
    q.answer = "b"
    b.update_question(q, ["answer"])
    assert b.list_questions()[0].answer == "b"
    row = b.upsert_agent(AgentRow(name="a", provider="claude"))
    row.runs = 3
    b.upsert_agent(row)
    assert b.get_agent("a").runs == 3 and len(b.list_agents()) == 1
    b.write_status_page("hello")
    assert b.status_page == "hello"


def test_claim_task_succeeds_and_detects_race():
    b = InMemoryBoard()
    t = b.create_task(Task(id="", title="x", status=Status.READY, agent="a"))
    assert claim_task(b, t, "a", sleep=lambda s: None, nonce="n1") is True
    assert b.get_task(t.id).status is Status.RUNNING
    # simulate another claimant overwriting the nonce mid-claim
    t2 = b.create_task(Task(id="", title="y", status=Status.READY, agent="a"))

    def hijack(_):
        stored = b.get_task(t2.id)
        stored.claim_nonce = "other"
        b.update_task(stored, ["claim_nonce"])

    assert claim_task(b, t2, "a", sleep=hijack, nonce="mine") is False


def test_claim_backs_off_when_task_was_reassigned():
    b = InMemoryBoard()
    t = b.create_task(Task(id="", title="x", status=Status.READY, agent="a"))
    stale = b.get_task(t.id)          # what the runner fetched on its last poll
    moved = b.get_task(t.id)
    moved.agent = "b"                 # orchestrator reassigned it meanwhile
    b.update_task(moved, ["agent"])
    assert claim_task(b, stale, "a", sleep=lambda s: None, nonce="n") is False
    assert b.get_task(t.id).status is Status.READY and b.get_task(t.id).agent == "b"


def test_memory_board_keeps_the_headline():
    from swarm.board.memory import InMemoryBoard
    b = InMemoryBoard()
    b.write_headline("All good", "green_background")
    assert b.headline == ("All good", "green_background")
