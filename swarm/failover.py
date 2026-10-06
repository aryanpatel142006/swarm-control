"""Reviewer failover: one exhausted account must never stall the board.

Oct 6 2026: the reviewer (claude-a2, Sonnet) hit its 5-hour session limit ("resets 3:50pm (America/New_York)") and
serve deferred every review for ~2 hours while nine finished tasks sat in Review and every worker went idle. Now the
review moves to a fallback agent at once (`reviewer.fallback_agents`, by default every other agent of the same provider
on the same host) and comes back to the primary after its reset.

The state lives on the board (the agent row's cooldown, the same one the runner writes for its own limits) plus the
reviewer's in-memory record, so `swarm status` in any shell computes the same active reviewer serve uses.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .config import Config
from .models import AgentRow


@dataclass
class ReviewerState:
    primary: str = ""
    active: str | None = None          # the agent that reviews now; None = every candidate is cooling down
    model: str = ""
    limited: dict[str, datetime] = field(default_factory=dict)   # candidate -> cooling down until

    @property
    def failed_over(self) -> bool:
        return bool(self.active and self.active != self.primary)

    @property
    def primary_reset(self) -> datetime | None:
        return self.limited.get(self.primary)

    @property
    def next_reset(self) -> datetime | None:
        return min(self.limited.values()) if self.limited else None


def reviewer_candidates(cfg: Config) -> list[str]:
    """The primary first, then the fallbacks: `reviewer.fallback_agents` when configured (an empty list turns
    failover off), otherwise every other agent of the primary's provider on the primary's host (serve runs the
    review CLI on its own machine, so another host's account directory is not there)."""
    role = cfg.reviewer
    if role is None:
        return []
    if role.fallback_agents is not None:
        rest = [a for a in role.fallback_agents if a in cfg.agents and a != role.agent]
    else:
        primary = cfg.agents[role.agent]
        rest = sorted(n for n, a in cfg.agents.items()
                      if n != role.agent and a.provider == primary.provider and a.host == primary.host)
    return [role.agent, *dict.fromkeys(rest)]


def reviewer_model(cfg: Config, agent: str) -> str:
    """The configured model for the primary; a fallback reviews with its own `mid` model (its account may not
    offer the primary's model name, and mid is what reviews are sized for)."""
    role = cfg.reviewer
    a = cfg.agents[agent]
    if role is not None and agent == role.agent:
        return role.model or a.models.get("mid", "")
    return a.models.get("mid") or (role.model if role else "")


def reviewer_state(cfg: Config, rows: dict[str, AgentRow] | list[AgentRow], now: datetime,
                   limits: dict[str, datetime] | None = None) -> ReviewerState:
    """Which agent reviews now. A candidate is out while its board row is cooling down (any limit: the runner's or
    the reviewer's own) or while the reviewer's in-memory record says so."""
    if isinstance(rows, list):
        rows = {r.name: r for r in rows}
    role = cfg.reviewer
    if role is None:
        return ReviewerState()
    st = ReviewerState(primary=role.agent)
    for name in reviewer_candidates(cfg):
        until = None
        row = rows.get(name)
        if row and row.cooldown_until and row.cooldown_until > now:
            until = row.cooldown_until
        mem = (limits or {}).get(name)
        if mem and mem > now:
            until = max(until, mem) if until else mem
        if until:
            st.limited[name] = until
        elif st.active is None:
            st.active = name
    if st.active:
        st.model = reviewer_model(cfg, st.active)
    return st


def reviewer_status_line(cfg: Config, rows, now: datetime, waiting: int) -> tuple[str | None, str | None]:
    """(status line, RISK text or None) for `swarm status`."""
    st = reviewer_state(cfg, rows, now)
    if not st.primary:
        return None, None
    if st.active and not st.failed_over:
        return f"Reviewer: {st.active} ({st.model})", None
    until = st.primary_reset
    when = f" until {until:%H:%M} UTC" if until else ""
    if st.active:
        return f"Reviewer: {st.active} ({st.model}) · failover from {st.primary}{when}", None
    nxt = st.next_reset
    line = (f"Reviewer: none · {', '.join(f'{n} limited until {u:%H:%M} UTC' for n, u in st.limited.items())}"
            + ("" if len(st.limited) > 1 else ", no fallback"))
    risk = None if not waiting else (f"reviewer exhausted, no fallback, {waiting} tasks waiting in Review"
            + (f" (first reset {nxt:%H:%M} UTC)" if nxt else "")
            + ": add another agent to reviewer.fallback_agents or review by hand")
    return line, risk
