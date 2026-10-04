"""Worker-to-worker and orchestrator-to-worker messages, relayed through the task's prompt.

A worker cannot talk to another worker directly (different laptops, different processes). It puts
`messages: [{to: "T-012", text: ...}]` in its report; the runner appends each one to the target task's
`feedback`, which the prompt compiler injects into that task's next run, and files a `relay` note on the
board so the orchestrator sees the conversation. `swarm tell T-012 "..."` does the same from the orchestrator.
"""
from __future__ import annotations

from .models import Question, Report, Status, Task

CLOSED = {Status.DONE, Status.CUT}


def _append_feedback(board, target: Task, line: str) -> None:
    target.feedback = (target.feedback.rstrip() + "\n\n" + line).strip()[:1900]
    board.update_task(target, ["feedback"])


def _note(board, *, text: str, context: str, task_id: str, asked_by: str) -> Question:
    q = Question(id="", text=text[:190], kind="relay", context=context[:1900], impact="low",
                 task_id=task_id, asked_by=asked_by, status="Applied")
    return board.create_question(q)


def deliver_messages(board, sender: Task, report: Report) -> list[Question]:
    """Append every message to its target task (open tasks only) and file one relay note per delivery."""
    notes: list[Question] = []
    for m in report.messages:
        to, text = str(m.get("to") or "").strip(), str(m.get("text") or "").strip()
        if not to or not text or to == sender.id:
            continue
        target = board.get_task(to)
        if target is None or target.status in CLOSED:
            continue
        _append_feedback(board, target, f"Message from {sender.id} ({sender.agent or 'worker'}): {text}")
        notes.append(_note(board, text=f"[relay] {sender.id} → {to}: {text}", context=text,
                           task_id=to, asked_by=sender.agent or sender.id))
    return notes


def tell(board, task_id: str, text: str, *, sender: str = "orchestrator") -> Question | None:
    target = board.get_task(task_id)
    if target is None or target.status in CLOSED or not text.strip():
        return None
    _append_feedback(board, target, f"Message from {sender}: {text.strip()}")
    return _note(board, text=f"[relay] {sender} → {task_id}: {text.strip()}", context=text, task_id=task_id,
                 asked_by=sender)
