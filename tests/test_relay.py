from swarm.board.memory import InMemoryBoard
from swarm.models import Report, Status, Task
from swarm.relay import deliver_messages, tell
from swarm.report import REPORT_SCHEMA, parse_report


def _board():
    b = InMemoryBoard()
    b.create_task(Task(id="", title="server", status=Status.READY, agent="codex-b", type="backend"))      # T-001
    b.create_task(Task(id="", title="console", status=Status.RUNNING, agent="claude-a", type="frontend"))  # T-002
    b.create_task(Task(id="", title="old", status=Status.DONE, agent="claude-a"))                          # T-003
    return b


def test_messages_parse_and_are_delivered_into_the_target_tasks_prompt(tmp_path):
    assert "messages" in REPORT_SCHEMA["properties"]
    r = parse_report({"status": "done", "summary": "ok",
                      "messages": [{"to": "T-001", "text": "telemetry now carries latency_ms per stage"},
                                   {"to": "T-003", "text": "ignored: done"}, {"to": "T-999", "text": "ignored: unknown"},
                                   {"to": "", "text": "ignored: no target"}]},
                     tmp_path, changed_files=["a.py"])
    assert len(r.messages) == 4
    board = _board()
    sender = Task(id="T-002", title="console", agent="claude-a")
    notes = deliver_messages(board, sender, r)
    t1 = board.get_task("T-001")
    assert "Message from T-002 (claude-a): telemetry now carries latency_ms per stage" in t1.feedback
    assert board.get_task("T-003").feedback == ""
    assert len(notes) == 1 and notes[0].kind == "relay" and notes[0].text.startswith("[relay] T-002 → T-001")
    assert notes[0].status == "Applied"      # nothing for a human to answer; it is a log line on the board


def test_tell_from_the_orchestrator():
    board = _board()
    q = tell(board, "T-002", "use PCM16 LE on the wire, not float32", sender="orchestrator")
    assert "Message from orchestrator: use PCM16 LE" in board.get_task("T-002").feedback
    assert q.kind == "relay" and q.asked_by == "orchestrator"
    assert tell(board, "T-404", "nobody home") is None
