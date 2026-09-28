"""The status page: hours left, agents, milestones, what needs a human, risk lines."""
from __future__ import annotations

from datetime import datetime

from .config import Config
from .models import AgentRow, Question, Status, Task

EST_HOURS_PER_TASK = 0.6


def render_status(cfg: Config, tasks: list[Task], agents: list[AgentRow], questions: list[Question],
                  now: datetime) -> str:
    hours_left = max(0.0, (cfg.event_end - now).total_seconds() / 3600)
    lines = [f"SWARM STATUS · {now.strftime('%Y-%m-%d %H:%M UTC')} · {hours_left:.1f} h left", ""]
    parts = []
    for a in sorted(agents, key=lambda x: x.name):
        if a.name == "serve":
            continue
        never_seen = a.last_heartbeat is None and a.status == "offline"
        bit = f"{a.name} no heartbeat yet" if never_seen else f"{a.name} {a.status}"
        if a.current_task:
            bit += f"({a.current_task})"
        if a.status == "cooldown" and a.cooldown_until:
            bit += f" until {a.cooldown_until.strftime('%H:%M')}"
        bit += f" ${a.cost_5h_usd:.1f}/5h"
        parts.append(bit)
    lines.append("Agents: " + (" · ".join(parts) or "(none yet)"))
    lines.append("")
    lines.append("Milestones:")
    by_ms: dict[str, list[Task]] = {}
    for t in tasks:
        if t.status is Status.CUT:
            continue
        by_ms.setdefault(t.milestone or "-", []).append(t)
    active_agents = max(1, len([a for a in agents if a.name != "serve" and a.status != "offline"]))
    risks = []
    for ms in sorted(by_ms):
        group = by_ms[ms]
        done = sum(1 for t in group if t.status is Status.DONE)
        counts = {s: sum(1 for t in group if t.status is s) for s in
                  (Status.RUNNING, Status.REVIEW, Status.MERGE_READY, Status.BLOCKED, Status.FAILED, Status.READY)}
        extras = " · ".join(f"{n} {s.value.lower()}" for s, n in counts.items() if n)
        lines.append(f"  {ms:<4} {done}/{len(group)} done" + (f" · {extras}" if extras else ""))
        open_n = len(group) - done
        est = open_n * EST_HOURS_PER_TASK / active_agents
        if open_n and est > hours_left:
            risks.append(f"{ms} has {open_n} open tasks (~{est:.1f} h of work) but only {hours_left:.1f} h remain")
        if counts[Status.BLOCKED] + counts[Status.FAILED] >= 2:
            risks.append(f"{ms} has {counts[Status.BLOCKED]} blocked and {counts[Status.FAILED]} failed tasks")
    lines.append("")
    needs = [f"{q.id} ({q.task_id}) \"{q.text}\"" for q in questions if q.status == "Open"]
    needs += [f"{t.id} failed ×{t.attempts}: {t.last_error[:60]}" for t in tasks if t.status is Status.FAILED]
    needs += [f"{t.id} blocked" for t in tasks
              if t.status is Status.BLOCKED and not any(q.task_id == t.id for q in questions)]
    lines.append("Needs you: " + ("; ".join(needs) if needs else "nothing"))
    lines.append("")
    for r in risks:
        lines.append(f"RISK: {r}")
    if not risks:
        lines.append("RISK: none flagged")
    return "\n".join(lines)
