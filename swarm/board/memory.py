from __future__ import annotations

import copy
from typing import Iterable

from ..models import AgentRow, Question, Status, Task, next_id, utcnow


class InMemoryBoard:
    """Board implementation for tests and dry runs. Never persists."""

    def __init__(self) -> None:
        self.tasks: dict[str, Task] = {}
        self.questions: dict[str, Question] = {}
        self.agents: dict[str, AgentRow] = {}
        self.reports: dict[str, list[tuple[str, str]]] = {}
        self.status_page = ""
        self.headline: tuple[str, str] | None = None

    # ----- tasks -----
    def next_task_id(self) -> str:
        return next_id("T", self.tasks.keys())

    def create_task(self, task: Task) -> Task:
        task = copy.deepcopy(task)
        if not task.id:
            task.id = self.next_task_id()
        task.page_id = task.page_id or f"page-{task.id}"
        task.updated = utcnow()
        self.tasks[task.id] = task
        return copy.deepcopy(task)

    def get_task(self, task_id: str, *, page_id: str | None = None,
                 agent: str | None = None) -> Task | None:
        t = self.tasks.get(task_id)
        return copy.deepcopy(t) if t else None

    def list_tasks(self, *, status: Iterable[Status] | None = None,
                   agent: Iterable[str] | None = None) -> list[Task]:
        status = set(status) if status is not None else None
        agent = set(agent) if agent is not None else None
        out = [t for t in self.tasks.values()
               if (status is None or t.status in status) and (agent is None or t.agent in agent)]
        out.sort(key=lambda t: (t.priority, t.id))
        return [copy.deepcopy(t) for t in out]

    def update_task(self, task: Task, fields: Iterable[str]) -> Task:
        stored = self.tasks.get(task.id)
        if stored is None:  # id may have just been assigned; find the row by page handle
            key, stored = next(((k, t) for k, t in self.tasks.items() if t.page_id == task.page_id), (None, None))
            if stored is None:
                raise KeyError(task.id)
            del self.tasks[key]
            self.tasks[task.id] = stored
        for f in fields:
            setattr(stored, f, copy.deepcopy(getattr(task, f)))
        stored.updated = utcnow()
        return copy.deepcopy(stored)

    def append_task_report(self, task: Task, heading: str, markdown: str) -> None:
        self.reports.setdefault(task.id, []).append((heading, markdown))

    # ----- questions -----
    def next_question_id(self) -> str:
        return next_id("Q", self.questions.keys())

    def create_question(self, q: Question) -> Question:
        q = copy.deepcopy(q)
        if not q.id:
            q.id = self.next_question_id()
        q.page_id = q.page_id or f"page-{q.id}"
        self.questions[q.id] = q
        return copy.deepcopy(q)

    def list_questions(self, *, status: str | None = None) -> list[Question]:
        out = [q for q in self.questions.values() if status is None or q.status == status]
        out.sort(key=lambda q: q.id)
        return [copy.deepcopy(q) for q in out]

    def update_question(self, q: Question, fields: Iterable[str]) -> Question:
        stored = self.questions[q.id]
        for f in fields:
            setattr(stored, f, copy.deepcopy(getattr(q, f)))
        return copy.deepcopy(stored)

    # ----- agents -----
    def upsert_agent(self, row: AgentRow) -> AgentRow:
        row = copy.deepcopy(row)
        row.page_id = row.page_id or f"page-agent-{row.name}"
        self.agents[row.name] = row
        return copy.deepcopy(row)

    def get_agent(self, name: str) -> AgentRow | None:
        a = self.agents.get(name)
        return copy.deepcopy(a) if a else None

    def list_agents(self) -> list[AgentRow]:
        return [copy.deepcopy(a) for a in self.agents.values()]

    # ----- status page -----
    def write_status_page(self, text: str) -> None:
        self.status_page = text

    def write_headline(self, text: str, color: str) -> None:
        self.headline = (text, color)
