from __future__ import annotations

import os
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
    # Placeholder tokens rejected in lines a task adds (Q-160: a cut-off attempt left them in a doc). Regexes; []
    # turns the check off. Files are globs; code files are checked only if the project adds them (e.g. "**/*.py").
    placeholders: list[str] | None = None
    placeholder_files: list[str] | None = None
    # Regexes for backup/merge leftovers a task must not add (`*.md-e`, `*.orig`, …; Q-198). [] turns it off.
    stray_files: list[str] | None = None
    # fnmatch globs (on the basename; a leading "/" = repo top level only) for scratch files a task adds. They are
    # reported to the reviewer, never blocked (Q-439). None = defaults, [] turns it off.
    scratch_patterns: list[str] | None = None
    # A normal verify queued behind `swarm-lock --exclusive` measurements for longer than this proceeds without a
    # slot and says so (swarm/hostlock.py, Q-478). 0 = wait for as long as the exclusive holders run.
    exclusive_max_minutes: float = 25.0
    # Light verify (swarm/lightverify.py): a branch whose every changed file matches `light_paths` and none a
    # protected glob runs `light_command` (default: `fast`) in review and merge instead of the full verify.
    # None/[] = always full (the default). `protected_paths` extends the built-in never-light list.
    light_paths: list[str] | None = None
    light_command: str | None = None
    protected_paths: list[str] | None = None
    # Quick slot (swarm/lightverify.py quick_slot_wait): a harness verify of a branch whose every changed file matches
    # `quick_paths` (changes that cannot perturb an audio/latency measurement: web UI, docs) waits at most
    # `quick_wait_seconds` behind `swarm-lock --exclusive` measurements, then runs without a slot. None = the
    # built-in defaults (docs/**, *.md, web/**, frontend/**); [] turns it off.
    quick_paths: list[str] | None = None
    quick_wait_seconds: float = 60.0
    # Web verify (swarm/lightverify.py web_choice): a branch whose every changed file matches `web_paths` runs
    # `web_command` in pre-publish verify, review and merge instead of verify.fast/verify.full, without a verify
    # slot or any wait behind exclusive measurements. Applies to critical tasks too. None/[] = off (the default).
    web_paths: list[str] | None = None
    web_command: str | None = None


@dataclass
class HostConfig:
    name: str
    max_parallel: dict[str, int] = field(default_factory=dict)
    # verify runs at once on this machine (swarm/hostlock.py); the rest wait for a slot (Q-175, Q-185, Q-190)
    max_parallel_verify: int = 2
    # disk-backed scratch for workers and verify scripts on this machine, exported as TMPDIR and SWARM_SCRATCH.
    # Empty = the system default. laptop-c's /tmp is a 3.8 GiB tmpfs and a test needs 5 GiB (Q-1113). Must lie
    # outside every git repo: checkout-based scratch broke tests that assume no git parent.
    scratch_dir: str = ""


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
    # perplexity (swarm/adapters/perplexity.py): CLI binary, argv template ({prompt_file} {model} {cwd}) and the
    # auto-approve flags; None = the adapter's (unverified) defaults
    cli: str | None = None
    args_template: str | None = None
    approve_args: list[str] | None = None
    extra_args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)      # extra environment for the CLI (e.g. CLAUDE_CONFIG_DIR for a 2nd account)
    mcp: list[str] = field(default_factory=list)          # MCP server names this agent always gets
    # minutes of heartbeat silence before serve calls this agent offline; None = the project-wide
    # heartbeat_stale_minutes. For agents that write their own row on a slower cadence than a runner (muse-b, Oct 10)
    heartbeat_stale_minutes: int | None = None
    # allowlist of task types (or families: `ml` covers ml_audio) this agent may be given, by routing, rebalance,
    # redistribute, reroute, `swarm add` and `swarm assign` (unless --force). None = any type. Low strengths alone did
    # not keep Muse agents off app code: T-354 (frontend) went to muse-c twice (Aryan, Oct 10)
    task_types: list[str] | None = None

    def takes(self, task_type: str) -> bool:
        """True when this agent may be given a task of this type (its own key or its family key)."""
        if self.task_types is None:
            return True
        return task_type in self.task_types or task_type.split("_", 1)[0] in self.task_types


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
    # reviewer only: who reviews while `agent` is rate/usage limited (swarm/failover.py). None = every other agent
    # of the same provider on the same host; [] = no failover (reviews wait for the reset)
    fallback_agents: list[str] | None = None
    # reviewer only: reviews `swarm serve` runs at once, each in its own thread (1 = one after another, as before).
    # Every review runs verify_full, so this multiplies verify load on the reviewer's host (its verify slots cap it).
    parallel: int = 1


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
    home_page: str = ""
    headline_block: str = ""


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
    mcp_by_type: dict[str, list[str]] = field(default_factory=dict)
    mcp_by_importance: dict[str, list[str]] = field(default_factory=dict)
    skills_by_type: dict[str, list[str]] = field(default_factory=dict)
    skills_by_importance: dict[str, list[str]] = field(default_factory=dict)
    plugins_required: list[str] = field(default_factory=list)
    plugins_by_type: dict[str, list[str]] = field(default_factory=dict)
    mcp_servers: dict = field(default_factory=dict)   # inline MCP server definitions, override discovered ones
    # Environment for every worker CLI and every harness-run verify script. `{repo_root}` expands to the main
    # checkout, so shared data outside the worktrees is found without `source scripts/_py.sh` (Q-198:
    # HEARING_FIXTURES_DIR / HEARING_MODELS_DIR were only set by one script).
    env: dict[str, str] = field(default_factory=dict)
    # Directories whose gitignored files the runner symlinks from the main checkout into each fresh worktree (Q-250,
    # Q-252, Q-254 … nine tasks lost runs to 'no demo clip': manifests are committed, clips and stems are not).
    worktree_links: list[str] = field(default_factory=list)
    # The laptop whose `swarm tell` / `swarm answer` count as the orchestrator. Empty = the host that runs serve
    # (its row on the Agents board). Relays from any other host are labelled with their real origin (Q-561, Q-584).
    orchestrator_host: str = ""

    def project_env(self) -> dict[str, str]:
        return {k: v.replace("{repo_root}", str(self.repo_root)) for k, v in self.env.items()}

    def scratch_env(self, host: str | None) -> dict[str, str]:
        """TMPDIR and SWARM_SCRATCH for a host with `scratch_dir` (created on first use); {} elsewhere, or when the
        directory cannot be created (the system temp dir is still better than a failed run)."""
        h = self.hosts.get(host or "")
        if h is None or not h.scratch_dir:
            return {}
        try:
            Path(h.scratch_dir).mkdir(parents=True, exist_ok=True)
        except OSError:
            return {}
        return {"TMPDIR": h.scratch_dir, "SWARM_SCRATCH": h.scratch_dir}

    def mcp_for(self, task_type: str, agent: AgentConfig, importance: str | None = None) -> list[str]:
        """MCP servers a worker run gets: the task type's, the importance tier's, then the agent's own."""
        return list(dict.fromkeys(by_type(self.mcp_by_type, task_type)
                                  + list(self.mcp_by_importance.get(importance or "", []))
                                  + list(agent.mcp)))

    def skills_for(self, task_type: str, importance: str | None = None) -> list[str]:
        """Skills the worker is told to invoke for this task type and importance tier."""
        return list(dict.fromkeys(by_type(self.skills_by_type, task_type)
                                  + list(self.skills_by_importance.get(importance or "", []))))

    def plugins_for(self, task_type: str) -> list[str]:
        """Plugins that must be installed on the host before a task of this type runs."""
        return list(dict.fromkeys(list(self.plugins_required) + by_type(self.plugins_by_type, task_type)))

    def stale_minutes_for(self, agent: str) -> int:
        """Heartbeat silence (minutes) after which serve treats this agent as gone."""
        a = self.agents.get(agent)
        return a.heartbeat_stale_minutes if a and a.heartbeat_stale_minutes else self.heartbeat_stale_minutes

    def agents_on_host(self, host: str) -> list[AgentConfig]:
        return [a for a in self.agents.values() if a.host == host]

    def limit_for(self, size: str) -> TaskLimit:
        return self.task_limits[size]


def by_type(mapping: dict[str, list[str]], task_type: str) -> list[str]:
    """Entries for a task type: its own key first, then its family key (`ml` covers ml_audio, ml_vision, ml_fusion)."""
    family = task_type.split("_", 1)[0]
    return list(mapping.get(task_type, [])) + (list(mapping.get(family, [])) if family != task_type else [])


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
    fallback = raw.get("fallback_agents")
    if fallback is not None:
        if isinstance(fallback, str):
            fallback = [fallback]
        unknown = [str(a) for a in fallback if a not in agents]
        if unknown:
            raise ConfigError(f"{key}.fallback_agents names {', '.join(unknown)}, not configured agents")
        fallback = [str(a) for a in fallback]
    try:
        parallel = int(raw.get("parallel", 1) or 1)
    except (TypeError, ValueError):
        raise ConfigError(f"{key}.parallel must be a whole number, got {raw.get('parallel')!r}") from None
    if parallel < 1:
        raise ConfigError(f"{key}.parallel must be 1 or more, got {parallel}")
    return RoleConfig(agent=raw["agent"], model=str(raw.get("model", "")), effort=raw.get("effort"),
                      fallback_agents=fallback, parallel=parallel)


def _deep_merge(base: dict, overlay: dict) -> dict:
    """Mappings merge recursively; anything else (lists, scalars) in the overlay replaces the base value."""
    out = dict(base)
    for k, v in (overlay or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _str_list(value, key: str) -> list[str] | None:
    if value is None:
        return None
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ConfigError(f"{key} must be a list of strings")
    if key in ("verify.placeholders", "verify.stray_files"):
        import re
        for v in value:
            try:
                re.compile(v)
            except re.error as e:
                raise ConfigError(f"{key}: bad regex {v!r}: {e}") from e
    return list(value)


# Changes to these need a fresh process (another board, other agents or slots); the rest is re-read in place.
RESTART_KEYS = ("path", "repo_root", "project", "repo", "main_branch", "worktree_root", "hosts", "agents", "notion")


def config_signature(path: Path | str | None) -> str | None:
    """A hash of config.yaml and the overlays load_config merges over it (tuning.yaml, notion.yaml); None when the
    config cannot be read. Long-running processes compare it to notice an edited config (field note 97)."""
    import hashlib
    if not path:
        return None
    path = Path(path)
    h = hashlib.sha256()
    try:
        h.update(path.read_bytes())
    except OSError:
        return None
    for extra in ("tuning.yaml", "notion.yaml"):
        f = path.parent / extra
        try:
            h.update(b"\0" + extra.encode() + b"\0" + f.read_bytes())
        except OSError:
            continue
    return h.hexdigest()


def config_changes(old: "Config", new: "Config") -> list[str]:
    """Names of the Config fields that differ between two loads."""
    from dataclasses import fields
    return [f.name for f in fields(old) if getattr(old, f.name, None) != getattr(new, f.name, None)]


def _scratch_dir(host: str, value) -> str:
    """hosts.<name>.scratch_dir: an absolute path (~ expanded) outside any git checkout, or "" for the default."""
    if not value:
        return ""
    p = Path(os.path.expanduser(str(value)))
    if not p.is_absolute():
        raise ConfigError(f"hosts.{host}.scratch_dir must be an absolute path, got '{value}'")
    for parent in (p, *p.parents):
        if (parent / ".git").exists():
            raise ConfigError(f"hosts.{host}.scratch_dir '{value}' is inside the git checkout at {parent}; "
                              "use a directory outside every repo")
    return str(p)


def load_config(path: Path | str) -> Config:
    path = Path(path).resolve()
    raw = yaml.safe_load(path.read_text()) or {}
    repo_root = path.parent.parent
    tuning_file = path.parent / "tuning.yaml"   # written by `swarm retro`; overlays config.yaml, comments survive
    if tuning_file.exists():
        raw = _deep_merge(raw, yaml.safe_load(tuning_file.read_text()) or {})
    notion_raw = {}
    notion_file = path.parent / "notion.yaml"
    if notion_file.exists():
        notion_raw = yaml.safe_load(notion_file.read_text()) or {}
    notion_raw = {**(raw.get("notion") or {}), **notion_raw}

    hosts = {name: HostConfig(name=name, max_parallel=dict((h or {}).get("max_parallel", {})),
                              max_parallel_verify=max(1, int((h or {}).get("max_parallel_verify", 2))),
                              scratch_dir=_scratch_dir(name, (h or {}).get("scratch_dir")))
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
        if a.get("cli") is not None and not str(a.get("cli")).strip():
            raise ConfigError(f"agents.{name}.cli must be a non-empty command")
        if a.get("args_template") is not None:
            from .adapters.perplexity import PLACEHOLDERS, template_fields
            try:
                unknown = template_fields(str(a["args_template"])) - PLACEHOLDERS
            except ValueError as e:
                raise ConfigError(f"agents.{name}.args_template: {e}") from e
            if unknown:
                raise ConfigError(f"agents.{name}.args_template: unknown placeholder(s) {sorted(unknown)}; "
                                  f"use {{prompt_file}} {{model}} {{cwd}}")
        if a.get("approve_args") is not None and not isinstance(a.get("approve_args"), list):
            raise ConfigError(f"agents.{name}.approve_args must be a list of arguments")
        agents[name] = AgentConfig(
            name=name, provider=provider, host=host, parallel=int(a.get("parallel", 1)),
            models=models, effort=dict(a.get("effort") or {}), strengths=strengths,
            soft_cap_5h_usd=a.get("soft_cap_5h_usd"), sandbox=a.get("sandbox"),
            experimental=bool(a.get("experimental", False)),
            command_template=a.get("command_template"),
            cli=str(a["cli"]).strip() if a.get("cli") is not None else None,
            args_template=str(a["args_template"]) if a.get("args_template") is not None else None,
            approve_args=[str(x) for x in a["approve_args"]] if a.get("approve_args") is not None else None,
            extra_args=list(a.get("extra_args") or []),
            env={str(k): str(v) for k, v in (a.get("env") or {}).items()},
            mcp=[str(m) for m in (a.get("mcp") or [])],
            heartbeat_stale_minutes=(int(a["heartbeat_stale_minutes"]) if a.get("heartbeat_stale_minutes") is not None
                                     else None),
            task_types=_str_list(a.get("task_types"), f"agents.{name}.task_types"),
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
                            full=verify_raw.get("full"),
                            placeholders=_str_list(verify_raw.get("placeholders"), "verify.placeholders"),
                            placeholder_files=_str_list(verify_raw.get("placeholder_files"),
                                                        "verify.placeholder_files"),
                            stray_files=_str_list(verify_raw.get("stray_files"), "verify.stray_files"),
                            scratch_patterns=_str_list(verify_raw.get("scratch_patterns"), "verify.scratch_patterns"),
                            exclusive_max_minutes=max(0.0, float(verify_raw.get("exclusive_max_minutes", 25))),
                            light_paths=_str_list(verify_raw.get("light_paths"), "verify.light_paths"),
                            light_command=(str(verify_raw["light_command"]) if verify_raw.get("light_command")
                                           else None),
                            protected_paths=_str_list(verify_raw.get("protected_paths"), "verify.protected_paths"),
                            quick_paths=_str_list(verify_raw.get("quick_paths"), "verify.quick_paths"),
                            quick_wait_seconds=max(0.0, float(verify_raw.get("quick_wait_seconds", 60))),
                            web_paths=_str_list(verify_raw.get("web_paths"), "verify.web_paths"),
                            web_command=(str(verify_raw["web_command"]) if verify_raw.get("web_command") else None)),
        task_limits=task_limits, hosts=hosts, agents=agents, routing=routing,
        docs_by_type={k: list(v or []) for k, v in (raw.get("docs_by_type") or {}).items()},
        reviewer=_role(raw.get("reviewer"), agents, "reviewer"),
        planner=_role(raw.get("planner"), agents, "planner"),
        notion=NotionIds(**{k: str(v) for k, v in notion_raw.items() if k in NotionIds.__dataclass_fields__}),
        mcp_by_type=_str_lists(raw.get("mcp_by_type")),
        mcp_by_importance=_str_lists(raw.get("mcp_by_importance")),
        skills_by_type=_str_lists(raw.get("skills_by_type")),
        skills_by_importance=_str_lists(raw.get("skills_by_importance")),
        plugins_required=[str(x) for x in (raw.get("plugins_required") or [])],
        plugins_by_type=_str_lists(raw.get("plugins_by_type")),
        mcp_servers={str(k): dict(v or {}) for k, v in (raw.get("mcp_servers") or {}).items()},
        env={str(k): str(v) for k, v in (raw.get("env") or {}).items()},
        worktree_links=_str_list(raw.get("worktree_links"), "worktree_links") or [],
        orchestrator_host=str(raw.get("orchestrator_host") or ""),
    )


def _str_lists(block) -> dict[str, list[str]]:
    return {str(k): [str(m) for m in (v or [])] for k, v in (block or {}).items()}


def save_notion_ids(cfg: Config, ids: dict) -> Path:
    notion_file = cfg.path.parent / "notion.yaml"
    existing = (yaml.safe_load(notion_file.read_text()) if notion_file.exists() else {}) or {}
    existing.update({k: v for k, v in ids.items() if k in NotionIds.__dataclass_fields__})
    notion_file.write_text(yaml.safe_dump(existing, sort_keys=True))
    return notion_file
