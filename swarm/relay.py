"""Worker-to-worker and orchestrator-to-worker messages, relayed through the task's prompt.

A worker cannot talk to another worker directly (different laptops, different processes). It puts
`messages: [{to: "T-012", text: ...}]` in its report; the runner appends each one to the target task's
`feedback`, which the prompt compiler injects into that task's next run, and files a `relay` note on the
board so the orchestrator sees the conversation. `swarm tell T-012 "..."` does the same from the orchestrator.
"""
from __future__ import annotations

from .models import QUESTION_TEXT_CAP, Question, Report, Status, Task

CLOSED = {Status.DONE, Status.CUT}


FEEDBACK_CAP = 8000


def clip_keep_newest(text: str, cap: int = FEEDBACK_CAP) -> str:
    """Drop the OLDEST notes when feedback grows too long; a tail cut used to lose the newest message (Q-080)."""
    if len(text) <= cap:
        return text
    notes = text.split("\n\n")
    while len(notes) > 1 and len("\n\n".join(notes)) > cap - 40:
        notes.pop(0)
    kept = "\n\n".join(notes)
    return ("[older notes trimmed]\n\n" + kept)[:cap] if len(kept) <= cap - 40 else kept[-cap:]


def append_feedback(board, target: Task, line: str) -> bool:
    """Append one note to a task's feedback unless the same note is already there (Q-046: a rebase instruction
    was relayed twice word for word). Returns True when something was written."""
    line = line.strip()
    if not line or line in target.feedback:
        return False
    target.feedback = clip_keep_newest((target.feedback.rstrip() + "\n\n" + line).strip())
    board.update_task(target, ["feedback"])
    return True


_append_feedback = append_feedback


def _note(board, *, text: str, context: str, task_id: str, asked_by: str) -> Question:
    q = Question(id="", text=text[:QUESTION_TEXT_CAP], kind="relay", context=context[:1900], impact="low",
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
