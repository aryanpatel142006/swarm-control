from __future__ import annotations

import time
import uuid
from typing import Callable, Iterable, Protocol

from ..models import AgentRow, Question, Status, Task, utcnow


class Board(Protocol):
    def create_task(self, task: Task) -> Task: ...
    def get_task(self, task_id: str, *, page_id: str | None = None,
                 agent: str | None = None) -> Task | None: ...
    def list_tasks(self, *, status: Iterable[Status] | None = None,
                   agent: Iterable[str] | None = None) -> list[Task]: ...
    def update_task(self, task: Task, fields: Iterable[str]) -> Task: ...
    def append_task_report(self, task: Task, heading: str, markdown: str) -> None: ...
    def create_question(self, q: Question) -> Question: ...
    def list_questions(self, *, status: str | None = None) -> list[Question]: ...
    def update_question(self, q: Question, fields: Iterable[str]) -> Question: ...
    def upsert_agent(self, row: AgentRow) -> AgentRow: ...
    def get_agent(self, name: str) -> AgentRow | None: ...
    def list_agents(self) -> list[AgentRow]: ...
    def write_status_page(self, text: str) -> None: ...
    def write_headline(self, text: str, color: str) -> None: ...
    def next_task_id(self) -> str: ...
    def next_question_id(self) -> str: ...


def claim_task(board: Board, task: Task, agent: str, *, sleep: Callable[[float], None] = time.sleep,
               nonce: str | None = None, wait_s: float = 1.5) -> bool:
    """Optimistic claim: write Running + nonce, wait, re-read, confirm the nonce survived."""
    nonce = nonce or uuid.uuid4().hex
    fresh = board.get_task(task.id, page_id=task.page_id or None, agent=agent)
    if fresh is None or fresh.agent != agent or fresh.status not in (Status.READY, Status.CHANGES_REQUESTED):
        return False  # reassigned, cut, or already claimed since we polled
    task.status = Status.RUNNING
    task.claim_nonce = nonce
    task.agent = agent
    task.started = utcnow()
    board.update_task(task, ["status", "claim_nonce", "agent", "started"])
    sleep(wait_s)
    fresh = board.get_task(task.id, page_id=task.page_id or None, agent=agent)
    if fresh is None or fresh.claim_nonce != nonce or fresh.status is not Status.RUNNING:
        return False
    return True


def fresh_flags(board: Board, task: Task, *, add: Iterable[str] = (),
                drop: Callable[[str], bool] | None = None) -> list[str]:
    """The flags to write: the board's current list, re-read by page id, minus `drop`, plus `add`. Never a list
    built from an older copy of the task: that is a lost update. The orchestrator pinned T-361 to laptop-b
    (`host:laptop-b`) and then answered its question; serve wrote back the flags it had read before the pin, and
    rebalance moved the task to the wrong host (Oct 10 2026). Falls back to the caller's copy when the read fails."""
    try:
        fresh = board.get_task(task.id, page_id=task.page_id or None)
    except Exception:  # noqa: BLE001 - a failed read must not block the write it guards
        fresh = None
    base = fresh.flags if fresh is not None else task.flags
    return list(dict.fromkeys([f for f in base if not (drop and drop(f))] + list(add)))
