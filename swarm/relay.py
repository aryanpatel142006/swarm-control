"""Worker-to-worker and orchestrator-to-worker messages, relayed through the task's prompt.

A worker cannot talk to another worker directly (different laptops, different processes). It puts
`messages: [{to: "T-012", text: ...}]` in its report; the runner appends each one to the target task's
`feedback`, which the prompt compiler injects into that task's next run, and files a `relay` note on the
board so the orchestrator sees the conversation. `swarm tell T-012 "..."` does the same from the orchestrator.

Origin labels (Oct 10 2026): any tool on any laptop can run `swarm tell`, and laptop-b's Codex/Gemini posted
"orchestrator" messages into laptop-a tasks (Q-561, Q-584, Q-585, Q-586) that told workers to use another person's GPU
job. Every relay now records `<sender>@<host>` in the note's Asked By, and the worker sees a label naming the real
origin. "[orchestrator @ <host>]" appears only for `swarm tell` / `swarm answer` run on the orchestrator host
(config `orchestrator_host`, default the host that runs serve); anything else that claims to be the orchestrator,
and every legacy note without a host, reads "[relay from <host> (unverified 'orchestrator' label)]".
"""
from __future__ import annotations

import os
import re

from .models import QUESTION_TEXT_CAP, Question, Report, Status, Task

ORCHESTRATOR = "orchestrator"
UNVERIFIED = "unverified 'orchestrator' label"
_ORCH_LABEL = re.compile(r"\[\s*orchestrator\s*@\s*([^\]]*?)\s*\]", re.I)
_LEGACY_ORCH = re.compile(r"^Message from orchestrator:\s*", re.I)


def sender_tag(sender: str, host: str) -> str:
    """What a relay note stores in Asked By (and an answer in Answered By): `<sender>@<host>`."""
    return f"{sender}@{host}" if host else sender


def parse_sender(tag: str) -> tuple[str, str]:
    """`orchestrator@laptop-a` → ("orchestrator", "laptop-a"); a legacy tag without a host → (tag, "")."""
    tag = (tag or "").strip()
    sender, at, host = tag.rpartition("@")
    return (sender.strip(), host.strip()) if at and sender.strip() else (tag, "")


HARNESS_ASKERS = ("serve", "reviewer")


def blocks_task(q, task, extra_askers=()) -> bool:
    """A blocking question only blocks (and its answer only steers) the task when its own worker asked it, the
    harness filed it (serve, reviewer, the reviewer agent), or an orchestrator did. Another agent's blocking
    question that merely names the task (muse-b on T-377 and T-365, Oct 10 2026) is treated as fyi. An empty
    Asked By is a legacy row and still counts."""
    if getattr(q, "kind", "") != "blocking":
        return False
    who = (q.asked_by or "").strip()
    if not who or who.lower().startswith(ORCHESTRATOR):
        return True
    name = parse_sender(who)[0]
    ok = {x for x in (getattr(task, "agent", ""), *HARNESS_ASKERS, *extra_askers) if x}
    return who in ok or name in ok


def cli_sender(default: str = ORCHESTRATOR) -> str:
    """Who is running `swarm tell` / `swarm answer`: a worker run (its env has SWARM_TASK_ID) is never the
    orchestrator, whatever host it is on."""
    task = os.environ.get("SWARM_TASK_ID", "").strip()
    return f"worker on {task}" if task else default


def orchestrator_host(cfg, board=None, fallback: str = "") -> str:
    """config `orchestrator_host`, else the host whose serve row is on the board, else `fallback`."""
    if getattr(cfg, "orchestrator_host", ""):
        return cfg.orchestrator_host
    if board is not None:
        try:
            row = board.get_agent("serve")
            if row and row.host and row.host != "serve":
                return row.host
        except Exception:  # noqa: BLE001 - a board hiccup only costs the verified label
            pass
    return fallback


def origin_label(sender: str, host: str, orch_host: str) -> str:
    """The label a worker sees in front of a relayed message or answer."""
    sender, host = (sender or "").strip(), (host or "").strip()
    if sender == ORCHESTRATOR and host and orch_host and host == orch_host:
        return f"[orchestrator @ {host}]"
    where = host or "unknown host"
    if not sender or ORCHESTRATOR in sender.lower():
        return f"[relay from {where} ({UNVERIFIED})]"
    return f"[relay from {where} ({sender})]"


def neutralize(text: str) -> str:
    """A relayed text cannot carry its own orchestrator label: `[orchestrator @ x]` inside it is quoted."""
    return _ORCH_LABEL.sub(lambda m: f"[quoted label: orchestrator @ {m.group(1)}]", text or "")


def label_feedback(feedback: str, orch_host: str) -> str:
    """Prompt-time check of a task's feedback: a legacy "Message from orchestrator:" note (no host recorded) and an
    "[orchestrator @ X]" label whose X is not the orchestrator host are shown as unverified."""
    def fix(m: re.Match) -> str:
        host = m.group(1).strip()
        return m.group(0) if orch_host and host == orch_host else f"[relay from {host or 'unknown host'} ({UNVERIFIED})]"

    notes = []
    for note in (feedback or "").split("\n\n"):
        note = _ORCH_LABEL.sub(fix, note)
        note = _LEGACY_ORCH.sub(f"[relay from unknown host ({UNVERIFIED})] ", note)
        notes.append(note)
    return "\n\n".join(notes)

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


def deliver_messages(board, sender: Task, report: Report, *, host: str = "") -> list[Question]:
    """Append every message to its target task (open tasks only) and file one relay note per delivery. `host` is
    the sending runner's host; a worker's message is never labelled as the orchestrator's."""
    notes: list[Question] = []
    who = f"{sender.agent or 'worker'} on {sender.id}"
    label = origin_label(who, host, "")
    for m in report.messages:
        to, text = str(m.get("to") or "").strip(), str(m.get("text") or "").strip()
        if not to or not text or to == sender.id:
            continue
        target = board.get_task(to)
        if target is None or target.status in CLOSED:
            continue
        _append_feedback(board, target, f"{label} {neutralize(text)}")
        notes.append(_note(board, text=f"[relay] {sender.id} ({sender_tag(sender.agent or 'worker', host)}) → {to}: "
                           f"{text}", context=text, task_id=to, asked_by=sender_tag(sender.agent or sender.id, host)))
    return notes


def tell(board, task_id: str, text: str, *, sender: str = ORCHESTRATOR, host: str = "",
         orch_host: str = "") -> Question | None:
    """`swarm tell`: `host` is SWARM_HOST of the laptop running it, `orch_host` the configured orchestrator host."""
    target = board.get_task(task_id)
    if target is None or target.status in CLOSED or not text.strip():
        return None
    label = origin_label(sender, host, orch_host)
    _append_feedback(board, target, f"{label} {neutralize(text.strip())}")
    return _note(board, text=f"[relay] {sender_tag(sender, host or 'unknown host')} → {task_id}: {text.strip()}",
                 context=text, task_id=task_id, asked_by=sender_tag(sender, host))
