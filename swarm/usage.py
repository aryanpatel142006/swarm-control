"""Per-laptop usage ledger (JSONL) with rolling-window sums for soft caps."""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

from .models import Usage, utcnow


def default_ledger_path(project: str) -> Path:
    return Path.home() / ".swarm" / project / "usage.jsonl"


class Ledger:
    def __init__(self, path: Path):
        self.path = Path(path)

    def append(self, *, agent: str, model: str, task_id: str, usage: Usage, duration_s: float, ok: bool,
               now: datetime | None = None, role: str = "worker") -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        entry = {"ts": (now or utcnow()).isoformat(), "agent": agent, "model": model, "task": task_id, "role": role,
                 "in": usage.input_tokens, "out": usage.output_tokens, "cost": usage.cost_usd,
                 "cache_w": usage.cache_write_tokens, "cache_r": usage.cache_read_tokens,
                 "duration_s": round(duration_s, 1), "ok": ok}
        with self.path.open("a") as f:
            f.write(json.dumps(entry) + "\n")

    def _rows(self) -> list[dict]:
        if not self.path.exists():
            return []
        rows = []
        for line in self.path.read_text().splitlines():
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
        return rows

    @staticmethod
    def _sum(rows) -> Usage:
        u = Usage(cost_usd=0.0)
        for r in rows:
            u.input_tokens += int(r.get("in") or 0)
            u.output_tokens += int(r.get("out") or 0)
            u.cost_usd += float(r.get("cost") or 0.0)
            u.cache_write_tokens += int(r.get("cache_w") or 0)
            u.cache_read_tokens += int(r.get("cache_r") or 0)
        return u

    def window(self, agent: str, hours: float = 5, now: datetime | None = None) -> Usage:
        now = now or utcnow()
        cutoff = now - timedelta(hours=hours)
        rows = [r for r in self._rows() if r.get("agent") == agent
                and datetime.fromisoformat(r["ts"]) >= cutoff]
        return self._sum(rows)

    def totals(self, agent: str) -> Usage:
        return self._sum([r for r in self._rows() if r.get("agent") == agent])

    def all_agents(self) -> list[str]:
        return sorted({r.get("agent") for r in self._rows() if r.get("agent")})


def usage_report(ledger: Ledger, done: list[str] | None = None) -> dict:
    """Where the money went: by role, per task, cache write vs read, and what counts as waste
    (failed runs, worker attempts after the first successful one, review rounds after the first)."""
    rows = ledger._rows()
    by_role: dict[str, float] = {}
    tasks: dict[str, dict] = {}
    cache_w = cache_r = 0
    waste = {"failed_runs": 0.0, "extra_attempts": 0.0, "extra_review_rounds": 0.0}
    for r in rows:
        role = r.get("role") or "worker"
        cost = float(r.get("cost") or 0.0)
        by_role[role] = round(by_role.get(role, 0.0) + cost, 4)
        cache_w += int(r.get("cache_w") or 0)
        cache_r += int(r.get("cache_r") or 0)
        t = tasks.setdefault(r.get("task", "?"), {"task": r.get("task", "?"), "cost": 0.0, "worker_runs": 0,
                                                    "ok_runs": 0, "failed_runs": 0, "review_runs": 0, "models": [],
                                                    "seconds": 0.0})
        t["cost"] = round(t["cost"] + cost, 4)
        t["seconds"] += float(r.get("duration_s") or 0.0)
        if r.get("model") and r["model"] not in t["models"]:
            t["models"].append(r["model"])
        if role == "reviewer":
            t["review_runs"] += 1
            if t["review_runs"] > 1:
                waste["extra_review_rounds"] = round(waste["extra_review_rounds"] + cost, 4)
        elif role == "worker":
            t["worker_runs"] += 1
            if not r.get("ok"):
                t["failed_runs"] += 1
                waste["failed_runs"] = round(waste["failed_runs"] + cost, 4)
            else:
                t["ok_runs"] += 1
                if t["ok_runs"] > 1:
                    waste["extra_attempts"] = round(waste["extra_attempts"] + cost, 4)
    total = round(sum(by_role.values()), 4)
    done = [d for d in (done or []) if d in tasks]
    waste["total"] = round(sum(waste.values()), 4)
    return {"total_cost": total, "by_role": by_role, "tasks": list(tasks.values()),
            "cost_per_done_task": round(total / len(done), 4) if done else None,   # all-in: planner + reviews included
            "waste": waste, "cache": {"write": cache_w, "read": cache_r}, "runs": len(rows)}
