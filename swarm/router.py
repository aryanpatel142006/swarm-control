"""Deterministic routing: strongest agent for the task type, model tier from importance."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .config import AgentConfig, Config
from .models import IMPORTANCES, AgentRow, Status, Task, utcnow

PROVIDER_COST_RANK = {"generic": 0, "gemini": 1, "antigravity": 1, "grok": 2, "codex": 3, "claude": 4}


@dataclass
class RouteContext:
    rows: dict[str, AgentRow] = field(default_factory=dict)
    queue_depth: dict[str, int] = field(default_factory=dict)
    scopes_by_agent: dict[str, list[str]] = field(default_factory=dict)
    now: datetime = field(default_factory=utcnow)


def escalate_importance(importance: str) -> str:
    i = IMPORTANCES.index(importance) if importance in IMPORTANCES else IMPORTANCES.index("normal")
    return IMPORTANCES[max(0, i - 1)]


def tier_for(task: Task, cfg: Config) -> str:
    tier = cfg.routing.importance_to_tier.get(task.importance, "mid")
    only_when = cfg.routing.best_tier_only_when.get("importance")
    if tier == "best" and only_when and task.importance != only_when:
        tier = "high"
    return tier


def model_for(agent: AgentConfig, tier: str, task_type: str, cfg: Config) -> tuple[str, str | None]:
    override = (cfg.routing.type_model_overrides.get(agent.provider) or {}).get(task_type) or {}
    model = override.get(tier) or agent.models[tier]
    return model, agent.effort.get(tier)


def _prefix(glob: str) -> str:
    return glob.split("*")[0].rstrip("/")


def scopes_overlap(a: list[str], b: list[str]) -> bool:
    for x in a:
        for y in b:
            px, py = _prefix(x), _prefix(y)
            if px == py or px.startswith(py + "/") or py.startswith(px + "/") or px == "" or py == "":
                return True
    return False


def is_available(agent: AgentConfig, row: AgentRow | None, *, importance: str, now: datetime) -> bool:
    if row is None:
        return True   # pure routing (tests, dry runs): no board state to consult
    if row.status == "offline" and row.last_heartbeat is None:
        return False  # never checked in: only in config, no runner behind it yet
    critical = importance == "critical"
    if row.status == "offline" and not critical:
        return False
    if row.cooldown_until and row.cooldown_until > now and not critical:
        return False
    if (agent.soft_cap_5h_usd and importance in ("normal", "low")
            and row.cost_5h_usd >= 0.8 * agent.soft_cap_5h_usd):
        return False
    return True


def route(task: Task, cfg: Config, ctx: RouteContext) -> tuple[str, str, str | None]:
    agents = list(cfg.agents.values())
    candidates = [a for a in agents
                  if is_available(a, ctx.rows.get(a.name), importance=task.importance, now=ctx.now)]
    if not candidates:   # nobody fully available: prefer agents that are alive (capped or cooling) over dead ones
        candidates = [a for a in agents if (r := ctx.rows.get(a.name)) is None
                      or (r.status != "offline" and r.last_heartbeat is not None)] or agents

    def score(a: AgentConfig) -> int:
        return a.strengths.get(task.type, 3)

    def sort_key(a: AgentConfig):
        return (-score(a), ctx.queue_depth.get(a.name, 0), PROVIDER_COST_RANK.get(a.provider, 5), a.name)

    ranked = sorted(candidates, key=sort_key)
    best = ranked[0]
    chosen = best
    # scope affinity: someone already working in these files, within one point of the best
    if task.scope:
        for a in ranked:
            if scopes_overlap(task.scope, ctx.scopes_by_agent.get(a.name, [])) and score(a) >= score(best) - 1:
                chosen = a
                break
    # spill when the queue is deep and the task is not critical
    if chosen is best and task.importance != "critical" and ctx.queue_depth.get(best.name, 0) > cfg.max_queue_depth:
        for a in ranked[1:]:
            if score(a) >= score(best) - 1 and ctx.queue_depth.get(a.name, 0) <= cfg.max_queue_depth:
                chosen = a
                break
    tier = tier_for(task, cfg)
    model, effort = model_for(chosen, tier, task.type, cfg)
    return chosen.name, model, effort


def context_from_board(board, cfg: Config, now: datetime | None = None) -> RouteContext:
    now = now or utcnow()
    rows = {a.name: a for a in board.list_agents()}
    for name in cfg.agents:   # configured but never synced or seen: treat as offline with no heartbeat
        rows.setdefault(name, AgentRow(name=name, status="offline", last_heartbeat=None))
    depth: dict[str, int] = {}
    scopes: dict[str, list[str]] = {}
    for t in board.list_tasks(status=[Status.READY, Status.RUNNING, Status.CHANGES_REQUESTED]):
        if t.agent:
            depth[t.agent] = depth.get(t.agent, 0) + 1
            scopes.setdefault(t.agent, []).extend(t.scope)
    return RouteContext(rows=rows, queue_depth=depth, scopes_by_agent=scopes, now=now)
