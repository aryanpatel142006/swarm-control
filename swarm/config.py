from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import yaml

from .models import IMPORTANCES, PROVIDERS, REVIEW_POLICIES, SIZES, TASK_TYPES, TIERS


class ConfigError(ValueError):
    pass


@dataclass
class TaskLimit:
    turns: int
    minutes: int
    budget_usd: float | None = None


@dataclass
class VerifyConfig:
    setup_worktree: str | None = None
    fast: str | None = None
    full: str | None = None


@dataclass
class HostConfig:
    name: str
    max_parallel: dict[str, int] = field(default_factory=dict)


@dataclass
class AgentConfig:
    name: str
    provider: str
    host: str
    parallel: int = 1
    models: dict[str, str] = field(default_factory=dict)
    effort: dict[str, str] = field(default_factory=dict)
    strengths: dict[str, int] = field(default_factory=dict)
    soft_cap_5h_usd: float | None = None
    sandbox: str | None = None
    experimental: bool = False
    command_template: str | None = None
    extra_args: list[str] = field(default_factory=list)


@dataclass
class RoutingConfig:
    importance_to_tier: dict[str, str] = field(default_factory=dict)
    best_tier_only_when: dict[str, str] = field(default_factory=dict)
    type_model_overrides: dict[str, dict[str, dict[str, str]]] = field(default_factory=dict)


@dataclass
class RoleConfig:
    agent: str
    model: str
    effort: str | None = None


@dataclass
class NotionIds:
    parent_page_id: str = ""
    tasks_db: str = ""
    tasks_ds: str = ""
    questions_db: str = ""
    questions_ds: str = ""
    agents_db: str = ""
    agents_ds: str = ""
    status_page: str = ""
    status_block: str = ""


@dataclass
class Config:
    path: Path
    repo_root: Path
    project: str
    repo: str
    main_branch: str
    worktree_root: Path
    event_start: datetime
    event_end: datetime
    poll_seconds: int
    idle_poll_seconds: int
    heartbeat_seconds: int
    serve_seconds: int
    review_policy: str
    max_review_rounds: int
    max_attempts: int
    heartbeat_stale_minutes: int
    max_queue_depth: int
    verify: VerifyConfig
    task_limits: dict[str, TaskLimit]
    hosts: dict[str, HostConfig]
    agents: dict[str, AgentConfig]
    routing: RoutingConfig
    docs_by_type: dict[str, list[str]]
    reviewer: RoleConfig | None
    planner: RoleConfig | None
    notion: NotionIds

    def agents_on_host(self, host: str) -> list[AgentConfig]:
        return [a for a in self.agents.values() if a.host == host]

    def limit_for(self, size: str) -> TaskLimit:
        return self.task_limits[size]


def _dt(value, default: datetime) -> datetime:
    if value is None:
        return default
    dt = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _role(raw, agents: dict[str, AgentConfig], key: str) -> RoleConfig | None:
    if not raw:
        return None
    if raw.get("agent") not in agents:
        raise ConfigError(f"{key}.agent '{raw.get('agent')}' is not a configured agent")
    return RoleConfig(agent=raw["agent"], model=str(raw.get("model", "")), effort=raw.get("effort"))


def load_config(path: Path | str) -> Config:
    path = Path(path).resolve()
    raw = yaml.safe_load(path.read_text()) or {}
    repo_root = path.parent.parent
    notion_raw = {}
    notion_file = path.parent / "notion.yaml"
    if notion_file.exists():
        notion_raw = yaml.safe_load(notion_file.read_text()) or {}
    notion_raw = {**(raw.get("notion") or {}), **notion_raw}

    hosts = {name: HostConfig(name=name, max_parallel=dict((h or {}).get("max_parallel", {})))
             for name, h in (raw.get("hosts") or {}).items()}

    agents: dict[str, AgentConfig] = {}
    for name, a in (raw.get("agents") or {}).items():
        a = a or {}
        provider = a.get("provider")
        if provider not in PROVIDERS:
            raise ConfigError(f"agents.{name}.provider '{provider}' not in {PROVIDERS}")
        host = a.get("host")
        if host not in hosts:
            raise ConfigError(f"agents.{name}: unknown host '{host}'")
        models = dict(a.get("models") or {})
        for tier in TIERS:
            if tier not in models:
                raise ConfigError(f"agents.{name}.models.{tier} is required")
        strengths = {t: int((a.get("strengths") or {}).get(t, 3)) for t in TASK_TYPES}
        if provider == "generic" and not a.get("command_template"):
            raise ConfigError(f"agents.{name}: generic provider requires command_template")
        agents[name] = AgentConfig(
            name=name, provider=provider, host=host, parallel=int(a.get("parallel", 1)),
            models=models, effort=dict(a.get("effort") or {}), strengths=strengths,
            soft_cap_5h_usd=a.get("soft_cap_5h_usd"), sandbox=a.get("sandbox"),
            experimental=bool(a.get("experimental", False)),
            command_template=a.get("command_template"), extra_args=list(a.get("extra_args") or []),
        )
    if not agents:
        raise ConfigError("at least one agent is required")

    review_policy = raw.get("review_policy", "high_and_above")
    if review_policy not in REVIEW_POLICIES:
        raise ConfigError(f"review_policy must be one of {REVIEW_POLICIES}")

    limits_raw = raw.get("task_limits") or {}
    task_limits = {}
    for size in SIZES:
        lim = limits_raw.get(size)
        if not lim:
            raise ConfigError(f"task_limits.{size} is required")
        task_limits[size] = TaskLimit(turns=int(lim["turns"]), minutes=int(lim["minutes"]),
                                      budget_usd=lim.get("budget_usd"))

    routing_raw = raw.get("routing") or {}
    routing = RoutingConfig(
        importance_to_tier=dict(routing_raw.get("importance_to_tier")
                                or {"critical": "best", "high": "high", "normal": "mid", "low": "low"}),
        best_tier_only_when=dict(routing_raw.get("best_tier_only_when") or {"importance": "critical"}),
        type_model_overrides=dict(routing_raw.get("type_model_overrides") or {}),
    )
    for imp in IMPORTANCES:
        if routing.importance_to_tier.get(imp) not in TIERS:
            raise ConfigError(f"routing.importance_to_tier.{imp} must be one of {TIERS}")

    verify_raw = raw.get("verify") or {}
    event = raw.get("event") or {}
    now = datetime.now(timezone.utc)
    worktree_root = (repo_root / raw.get("worktree_root", f"../{raw.get('project', 'project')}-wt")).resolve()

    return Config(
        path=path, repo_root=repo_root,
        project=str(raw.get("project", repo_root.name)), repo=str(raw.get("repo", "")),
        main_branch=str(raw.get("main_branch", "main")), worktree_root=worktree_root,
        event_start=_dt(event.get("start"), now), event_end=_dt(event.get("end"), now),
        poll_seconds=int(raw.get("poll_seconds", 15)), idle_poll_seconds=int(raw.get("idle_poll_seconds", 30)),
        heartbeat_seconds=int(raw.get("heartbeat_seconds", 60)), serve_seconds=int(raw.get("serve_seconds", 30)),
        review_policy=review_policy, max_review_rounds=int(raw.get("max_review_rounds", 2)),
        max_attempts=int(raw.get("max_attempts", 3)),
        heartbeat_stale_minutes=int(raw.get("heartbeat_stale_minutes", 10)),
        max_queue_depth=int(raw.get("max_queue_depth", 4)),
        verify=VerifyConfig(setup_worktree=verify_raw.get("setup_worktree"), fast=verify_raw.get("fast"),
                            full=verify_raw.get("full")),
        task_limits=task_limits, hosts=hosts, agents=agents, routing=routing,
        docs_by_type={k: list(v or []) for k, v in (raw.get("docs_by_type") or {}).items()},
        reviewer=_role(raw.get("reviewer"), agents, "reviewer"),
        planner=_role(raw.get("planner"), agents, "planner"),
        notion=NotionIds(**{k: str(v) for k, v in notion_raw.items() if k in NotionIds.__dataclass_fields__}),
    )


def save_notion_ids(cfg: Config, ids: dict) -> Path:
    notion_file = cfg.path.parent / "notion.yaml"
    existing = (yaml.safe_load(notion_file.read_text()) if notion_file.exists() else {}) or {}
    existing.update({k: v for k, v in ids.items() if k in NotionIds.__dataclass_fields__})
    notion_file.write_text(yaml.safe_dump(existing, sort_keys=True))
    return notion_file
