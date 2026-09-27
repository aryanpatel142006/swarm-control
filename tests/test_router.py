from datetime import timedelta

from swarm.models import AgentRow, Task, utcnow
from swarm.router import (
    RouteContext,
    escalate_importance,
    is_available,
    model_for,
    route,
    scopes_overlap,
    tier_for,
)


def ctx(**kw):
    base = dict(rows={}, queue_depth={}, scopes_by_agent={}, now=utcnow())
    base.update(kw)
    return RouteContext(**base)


def test_tier_and_model(cfg):
    assert tier_for(Task(id="T", title="", importance="critical"), cfg) == "best"
    assert tier_for(Task(id="T", title="", importance="high"), cfg) == "high"
    assert model_for(cfg.agents["claude-a"], "best", "backend", cfg) == ("fable", "high")
    assert model_for(cfg.agents["claude-a"], "best", "frontend", cfg) == ("opus", "high")  # override
    assert model_for(cfg.agents["codex-a"], "mid", "backend", cfg) == ("gpt-6-sol", "medium")
    assert model_for(cfg.agents["fake-b"], "low", "docs", cfg) == ("x", None)


def test_route_picks_highest_strength(cfg):
    t = Task(id="T-1", title="", type="frontend", importance="critical")
    assert route(t, cfg, ctx()) == ("claude-a", "opus", "high")
    t2 = Task(id="T-2", title="", type="backend", importance="normal")
    assert route(t2, cfg, ctx()) == ("codex-a", "gpt-6-sol", "medium")


def test_route_tie_breaks_by_queue_then_cost(cfg):
    t = Task(id="T-3", title="", type="tests", importance="normal")  # claude 4, codex 5
    assert route(t, cfg, ctx())[0] == "codex-a"
    t2 = Task(id="T-4", title="", type="ml_audio", importance="normal")  # all default 3
    agent, _, _ = route(t2, cfg, ctx(queue_depth={"claude-a": 2, "codex-a": 0, "fake-b": 0}))
    assert agent in ("codex-a", "fake-b")


def test_route_spills_when_queue_deep(cfg):
    t = Task(id="T-5", title="", type="backend", importance="normal")  # codex 5, claude 4
    assert route(t, cfg, ctx(queue_depth={"codex-a": 5}))[0] == "claude-a"
    tc = Task(id="T-6", title="", type="backend", importance="critical")
    assert route(tc, cfg, ctx(queue_depth={"codex-a": 9}))[0] == "codex-a"  # critical never spills


def test_route_skips_cooldown_and_offline_for_non_critical(cfg):
    now = utcnow()
    rows = {"codex-a": AgentRow(name="codex-a", status="cooldown", cooldown_until=now + timedelta(minutes=5))}
    t = Task(id="T-7", title="", type="backend", importance="high")
    assert route(t, cfg, ctx(rows=rows, now=now))[0] == "claude-a"
    tc = Task(id="T-8", title="", type="backend", importance="critical")
    assert route(tc, cfg, ctx(rows=rows, now=now))[0] == "codex-a"
    rows2 = {"codex-a": AgentRow(name="codex-a", status="offline")}
    assert route(t, cfg, ctx(rows=rows2, now=now))[0] == "claude-a"
    expired = {"codex-a": AgentRow(name="codex-a", status="cooldown", cooldown_until=now - timedelta(minutes=1))}
    assert route(t, cfg, ctx(rows=expired, now=now))[0] == "codex-a"


def test_soft_cap_blocks_normal_only(cfg):
    rows = {"claude-a": AgentRow(name="claude-a", cost_5h_usd=39.0)}  # cap 40 → above 80%
    t = Task(id="T-9", title="", type="frontend", importance="normal")
    assert route(t, cfg, ctx(rows=rows))[0] != "claude-a"
    th = Task(id="T-10", title="", type="frontend", importance="high")
    assert route(th, cfg, ctx(rows=rows))[0] == "claude-a"


def test_scope_affinity(cfg):
    t = Task(id="T-11", title="", type="ml_audio", importance="normal", scope=["ml/audio/**"])
    agent, _, _ = route(t, cfg, ctx(scopes_by_agent={"fake-b": ["ml/audio/**"]}))
    assert agent == "fake-b"
    assert scopes_overlap(["ml/audio/**"], ["ml/audio/sep.py"]) and not scopes_overlap(["a/**"], ["b/**"])
    assert scopes_overlap([], ["x"]) is False


def test_is_available_and_escalate(cfg):
    now = utcnow()
    assert is_available(cfg.agents["claude-a"], None, importance="low", now=now)
    assert escalate_importance("low") == "normal" and escalate_importance("critical") == "critical"


def test_unroutable_type_still_assigns(cfg):
    t = Task(id="T-12", title="", type="infra", importance="low")
    agent, model, _ = route(t, cfg, ctx())
    assert agent == "codex-a" and model == "gpt-6-luna"
