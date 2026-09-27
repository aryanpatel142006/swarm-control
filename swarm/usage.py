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
               now: datetime | None = None) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        entry = {"ts": (now or utcnow()).isoformat(), "agent": agent, "model": model, "task": task_id,
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
