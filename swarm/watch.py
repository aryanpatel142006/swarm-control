"""`swarm watch`: one line per board change, for an orchestrator's Monitor.

Replaces tools/board_watch.py from the HackRU run, plus the grep filters the orchestrator put on it. Two failures
it fixes (Oct 11 2026): (1) muting other laptops' chatter also hid muse-b's repeated requests to laptop-a for the
fine-tuned checkpoints (Q-1733, Q-1805, Q-1820) and blocking questions on laptop-b/c tasks; (2) questions were keyed by
id, so a duplicated id (Q-1978 twice) hid the second row. Rows are keyed by page id, and `--mute-host` never drops a
question that is blocking, a harness note, or addressed to the orchestrator (names the orchestrator, its host, one
of its host's agents, or a word given with `--always`).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .models import Question, Task

ALWAYS_KINDS = ("blocking", "harness")


def _num(tid: str) -> int:
    m = re.search(r"-(\d+)", tid or "")
    return int(m.group(1)) if m else 0


@dataclass
class Snapshot:
    tasks: dict[str, tuple[str, str, str]]          # page id (or id) -> (task id, status, agent)
    questions: dict[str, Question]                  # page id (or id+kind+task) -> question


def snapshot(tasks: list[Task], questions: list[Question], *, min_task: int = 0) -> Snapshot:
    ts = {(t.page_id or t.id): (t.id, t.status.value, t.agent or "") for t in tasks if _num(t.id) >= min_task}
    qs = {(q.page_id or f"{q.id}|{q.kind}|{q.task_id}"): q for q in questions}
    return Snapshot(ts, qs)


class Watcher:
    def __init__(self, cfg, *, local_host: str = "", mute_hosts: list[str] | tuple = (), always: list[str] | tuple = (),
                 orch_host: str = ""):
        self.cfg = cfg
        self.mute_hosts = set(mute_hosts)
        self.local_host = local_host
        self.orch_host = orch_host or getattr(cfg, "orchestrator_host", "") or local_host
        local_agents = [a.name for a in cfg.agents.values() if a.host in {self.orch_host, local_host} - {""}]
        words = ["orchestrator", *[w for w in (self.orch_host, local_host) if w], *local_agents, *always]
        self._addressed = re.compile(r"(?<![\w-])(?:" + "|".join(re.escape(w) for w in words) + r")(?![\w-])", re.I)

    def host_of_agent(self, agent: str) -> str:
        a = self.cfg.agents.get(agent or "")
        return a.host if a else ""

    def question_host(self, q: Question, tasks_by_id: dict[str, str]) -> str:
        if q.asked_by and "@" in q.asked_by:
            return q.asked_by.rsplit("@", 1)[1]
        return self.host_of_agent(tasks_by_id.get(q.task_id, ""))

    def must_show(self, q: Question) -> bool:
        """Blocking questions, harness notes and anything addressed to the orchestrator are never muted."""
        return q.kind in ALWAYS_KINDS or bool(self._addressed.search(f"{q.text}\n{q.context}"))

    def diff(self, prev: Snapshot, cur: Snapshot) -> list[str]:
        out = []
        agent_by_task = {tid: agent for tid, _st, agent in cur.tasks.values()}
        for key, (tid, status, agent) in cur.tasks.items():
            before = prev.tasks.get(key)
            if before is not None and before[1] == status:
                continue
            if self.host_of_agent(agent) in self.mute_hosts:
                continue
            out.append(f"TASK {tid}: {before[1] if before else 'new'} -> {status} ({agent or '-'})")
        for key, q in cur.questions.items():
            old = prev.questions.get(key)
            new = old is None
            reopened = old is not None and old.status != "Open" and q.status == "Open"
            if not (new or reopened):
                continue
            if q.status != "Open" and not new:
                continue
            host = self.question_host(q, agent_by_task)
            if host in self.mute_hosts and not self.must_show(q):
                continue
            flag = " [for orchestrator]" if self._addressed.search(f"{q.text}\n{q.context}") else ""
            text = " ".join((q.text or "").split())[:160]
            out.append(f"{'NEW' if new else 'REOPENED'} {q.id} [{q.kind}]{flag} {q.task_id or '-'}"
                       f"{' @' + host if host else ''}: {text}")
        return out
