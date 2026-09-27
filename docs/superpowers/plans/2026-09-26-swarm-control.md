# swarm-control Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build `swarm-control`, a Python CLI that runs many AI coding agents across two laptops from one Notion task board, with git worktrees per task, evidence-based status transitions, an independent reviewer, automatic merging, and a human question channel.

**Architecture:** A `Board` protocol (Notion implementation + in-memory implementation for tests) holds tasks, questions, and agent rows. `swarm run` on each laptop polls the board for its agents' tasks and drives provider adapters (Claude Code, Codex, Antigravity, Gemini, Grok, generic) headless inside git worktrees, then pushes, opens PRs, and publishes reports. `swarm serve` on one laptop reaps stale tasks, relays human answers, promotes dependents, reviews, merges, reroutes, and writes a status page. Deterministic Python owns every state transition; models only edit code and produce a structured report.

**Tech Stack:** Python 3.11+, `httpx`, `pyyaml`, `typer`, `rich`; `pytest` for tests; `git` and `gh` CLIs; Notion API version `2026-03-11`.

**Spec:** `docs/superpowers/specs/2026-09-26-swarm-control-design.md`

## Global Constraints

- Python `>= 3.11`. Runtime deps only: `httpx`, `pyyaml`, `typer`, `rich`. Dev deps: `pytest`, `pytest-httpx` is NOT used (use `httpx.MockTransport`).
- Notion header `Notion-Version: 2026-03-11`. Rate limit handling: honor `Retry-After` on 429 and 529, exponential backoff with jitter, max 5 retries.
- Task IDs: `T-001` style, zero-padded to 3. Question IDs: `Q-001`. Branch names: `task/T-001`.
- Task status set (exact strings): `Backlog, Ready, Running, Review, Changes Requested, Merge Ready, Blocked, Failed, Done, Cut`.
- Task types: `frontend, backend, realtime, ml_audio, ml_vision, ml_fusion, eval, tests, docs, research, bugfix, integration, infra`. Importance: `critical, high, normal, low`. Size: `S, M, L`. Tiers: `best, high, mid, low`.
- Runtime files inside a worktree live in `.swarm-run/` (gitignored). Project config lives in `.swarm/config.yaml` (committed); Notion ids in `.swarm/notion.yaml` (committed, written by `swarm init`).
- Worker rules file: `prompts/rules.md`. Report file the model may write: `.swarm-run/report.json`.
- Rich text fields written to Notion are chunked at 2,000 characters; at most 100 chunks.
- Workers never call the network from inside the model CLI; the runner pushes and opens PRs with `gh`.
- All datetimes are timezone-aware UTC.
- Every module gets unit tests; no test touches the real network or real `gh` unless env `SWARM_NOTION_TOKEN` (integration) is set.

## Review Focus

1. **A task whose `Agent` select was changed by hand in Notion to an agent not in config** should be treated as unrouted by the runner (skipped) and rerouted by `serve`, never crash. Test added to Task 12 (`test_runner_skips_unknown_agent`) and Task 15 (`test_reroute_unknown_agent`).
2. **A rich_text value longer than 2,000 characters** (a long feedback tail or report summary) must be chunked, not rejected by Notion. Test in Task 4 (`test_p_rich_chunks_long_text`).
3. **Two tasks with the same title but different IDs** must not be confused by the in-memory or Notion board; lookups are by `ID` property, never by title. Test in Task 3 (`test_get_task_by_id_not_title`).
4. **A model that returns a report with `status: done` but no changed files** should not be merged as success; the runner marks it Failed with `last_error = "no changes"`. Test in Task 12 (`test_done_without_changes_is_failed`).
5. **A `Retry-After` header that exceeds 60 s** must still be honored (sleep the full value, capped at 300 s) rather than treated as an error. Test in Task 5 (`test_retry_after_long_wait_is_capped`).

---

## File structure

```
swarm-control/
├── pyproject.toml
├── README.md
├── ORCHESTRATOR.md
├── prompts/
│   ├── rules.md            # worker standing rules (appended to every worker prompt)
│   ├── planner.md          # decomposition instructions
│   └── reviewer.md         # reviewer instructions
├── template/               # hackathon-base template repo contents
├── docs/
│   ├── RUNBOOK.md
│   └── superpowers/...
├── swarm/
│   ├── __init__.py
│   ├── models.py           # dataclasses + enums shared by everything
│   ├── config.py           # load/validate .swarm/config.yaml + .swarm/notion.yaml
│   ├── board/
│   │   ├── __init__.py
│   │   ├── base.py         # Board protocol + claim helper
│   │   ├── memory.py       # InMemoryBoard for tests and dry runs
│   │   ├── notion_props.py # Notion property builders/readers + DB schemas
│   │   └── notion.py       # NotionClient (httpx, retries) + NotionBoard
│   ├── workspace.py        # git worktrees, commits, push, gh PR
│   ├── adapters/
│   │   ├── __init__.py     # get_adapter()
│   │   ├── base.py         # RunSpec, Adapter base with subprocess + timeout + rate-limit regex
│   │   ├── claude.py  codex.py  antigravity.py  gemini.py  grok.py  generic.py
│   ├── report.py           # REPORT_SCHEMA, parse_report, synthesize, markdown
│   ├── prompt.py           # compile_prompt, doc selection
│   ├── router.py           # route(), escalate()
│   ├── usage.py            # Ledger (usage.jsonl), 5h windows
│   ├── runner.py           # Runner: claim, run_task, publish, tick, loop
│   ├── reviewer.py         # review policy + review run
│   ├── merge.py            # Merger: rebase, verify, push, gh merge, logs
│   ├── status.py           # render_status()
│   ├── serve.py            # Server: reap, retry, relay, promote, review, merge, reroute, status
│   ├── planner.py          # swarm plan / split
│   ├── doctor.py           # environment checks
│   └── cli.py              # typer app
└── tests/
    ├── conftest.py
    ├── fake_agent.sh       # generic-adapter stand-in used by e2e
    └── test_*.py
```

---

### Task 0: Project scaffold

**Files:**
- Create: `pyproject.toml`, `swarm/__init__.py`, `tests/__init__.py`, `tests/conftest.py`, `.gitignore`, `README.md` (stub)

**Interfaces:**
- Produces: importable package `swarm`; `pytest` runs from repo root; `swarm` console script → `swarm.cli:app`.

- [ ] **Step 1: Write pyproject.toml**

```toml
[build-system]
requires = ["setuptools>=68", "wheel"]
build-backend = "setuptools.build_meta"

[project]
name = "swarm-control"
version = "0.1.0"
description = "Multi-agent hackathon harness: Notion board + git worktrees + headless coding CLIs"
requires-python = ">=3.11"
dependencies = [
  "httpx>=0.27",
  "pyyaml>=6.0",
  "typer>=0.12",
  "rich>=13.0",
]

[project.optional-dependencies]
dev = ["pytest>=8.0"]

[project.scripts]
swarm = "swarm.cli:app"

[tool.setuptools.packages.find]
include = ["swarm*"]

[tool.pytest.ini_options]
testpaths = ["tests"]
addopts = "-q"
```

- [ ] **Step 2: Write package init, gitignore, conftest**

`swarm/__init__.py`:
```python
"""swarm-control: multi-agent hackathon harness."""
__version__ = "0.1.0"
```

`.gitignore`:
```
__pycache__/
*.pyc
.pytest_cache/
*.egg-info/
build/
dist/
.venv/
.env
.swarm-run/
```

`tests/__init__.py`: empty.

`tests/conftest.py`:
```python
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
import yaml

SAMPLE_CONFIG = {
    "project": "demo",
    "repo": "example/demo",
    "main_branch": "main",
    "worktree_root": "../demo-wt",
    "event": {"start": "2026-10-10T12:00:00-04:00", "end": "2026-10-11T12:00:00-04:00"},
    "poll_seconds": 15,
    "idle_poll_seconds": 30,
    "heartbeat_seconds": 60,
    "serve_seconds": 30,
    "review_policy": "high_and_above",
    "max_review_rounds": 2,
    "max_attempts": 3,
    "heartbeat_stale_minutes": 10,
    "max_queue_depth": 4,
    "verify": {
        "setup_worktree": "scripts/setup_worktree.sh",
        "fast": "scripts/verify_fast.sh",
        "full": "scripts/verify_full.sh",
    },
    "task_limits": {
        "S": {"turns": 30, "minutes": 20, "budget_usd": 3},
        "M": {"turns": 60, "minutes": 40, "budget_usd": 8},
        "L": {"turns": 120, "minutes": 75, "budget_usd": 20},
    },
    "hosts": {
        "host-a": {"max_parallel": {"claude": 3, "codex": 3, "generic": 2}},
        "host-b": {"max_parallel": {"codex": 3, "antigravity": 2, "generic": 2}},
    },
    "routing": {
        "importance_to_tier": {"critical": "best", "high": "high", "normal": "mid", "low": "low"},
        "best_tier_only_when": {"importance": "critical"},
        "type_model_overrides": {"claude": {"frontend": {"best": "opus"}}},
    },
    "docs_by_type": {
        "_all": ["PLAN.md#summary", "AGENTS.md"],
        "frontend": ["docs/DESIGN.md", "docs/CONTRACTS.md"],
        "backend": ["docs/CONTRACTS.md", "docs/ARCHITECTURE.md"],
    },
    "agents": {
        "claude-a": {
            "provider": "claude", "host": "host-a", "parallel": 2,
            "models": {"best": "fable", "high": "opus", "mid": "sonnet", "low": "haiku"},
            "effort": {"best": "high", "high": "high", "mid": "medium", "low": "low"},
            "strengths": {"frontend": 5, "backend": 4, "docs": 5, "tests": 4},
            "soft_cap_5h_usd": 40,
        },
        "codex-a": {
            "provider": "codex", "host": "host-a",
            "models": {"best": "gpt-6-astra", "high": "gpt-6-astra", "mid": "gpt-6-sol", "low": "gpt-6-luna"},
            "effort": {"best": "xhigh", "high": "high", "mid": "medium", "low": "low"},
            "strengths": {"frontend": 3, "backend": 5, "tests": 5, "infra": 5},
            "sandbox": "workspace-write",
        },
        "fake-b": {
            "provider": "generic", "host": "host-b",
            "models": {"best": "x", "high": "x", "mid": "x", "low": "x"},
            "strengths": {"backend": 3, "docs": 4},
            "command_template": "bash tests/fake_agent.sh {prompt_file} {model} {cwd}",
        },
    },
    "reviewer": {"agent": "codex-a", "model": "gpt-6-sol", "effort": "medium"},
    "planner": {"agent": "claude-a", "model": "opus", "effort": "high"},
}


@pytest.fixture
def sample_config_dict():
    import copy
    return copy.deepcopy(SAMPLE_CONFIG)


@pytest.fixture
def project_dir(tmp_path, sample_config_dict):
    """A fake project repo dir with .swarm/config.yaml and docs."""
    root = tmp_path / "demo"
    (root / ".swarm").mkdir(parents=True)
    (root / "docs").mkdir()
    (root / ".swarm" / "config.yaml").write_text(yaml.safe_dump(sample_config_dict))
    (root / "PLAN.md").write_text("# Demo\n\n## Summary\nA demo product.\n\n## Milestones\n- M1\n")
    (root / "AGENTS.md").write_text("Rules: stay in scope.\n")
    (root / "docs" / "DESIGN.md").write_text("# Design\nBlue buttons.\n")
    (root / "docs" / "CONTRACTS.md").write_text("# Contracts\n\n## events\n{\"a\":1}\n\n## http\nGET /x\n")
    (root / "docs" / "ARCHITECTURE.md").write_text("# Arch\n\n## ml\nml section\n")
    return root


@pytest.fixture
def cfg(project_dir):
    from swarm.config import load_config
    return load_config(project_dir / ".swarm" / "config.yaml")


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True).stdout


@pytest.fixture
def git_repo(tmp_path):
    """A local git repo with an 'origin' bare remote and one commit on main."""
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True)
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "tester")
    (repo / "README.md").write_text("# demo\n")
    (repo / "scripts").mkdir()
    (repo / "scripts" / "verify_fast.sh").write_text("#!/bin/sh\nexit 0\n")
    os.chmod(repo / "scripts" / "verify_fast.sh", 0o755)
    (repo / ".gitignore").write_text(".swarm-run/\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "push", "-q", "-u", "origin", "main")
    return repo
```

- [ ] **Step 3: Install in editable mode and verify pytest runs**

Run: `cd swarm-control && python3 -m venv .venv && source .venv/bin/activate && pip install -e ".[dev]" && pytest`
Expected: `no tests ran` (exit 5) — that is fine for an empty suite.

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml swarm/__init__.py tests/__init__.py tests/conftest.py .gitignore
git commit -m "chore: scaffold swarm-control package"
```

---

### Task 1: Models

**Files:**
- Create: `swarm/models.py`
- Test: `tests/test_models.py`

**Interfaces:**
- Produces: `Status` enum; constants `TASK_TYPES, IMPORTANCES, SIZES, TIERS, STATUS_ORDER`; dataclasses `Task, Question, AgentRow, Usage, RunResult, Report`; helpers `Task.branch`, `Task.title_with_id()`, `next_id(prefix, existing_ids)`, `utcnow()`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_models.py
from datetime import timezone
from swarm.models import (
    Status, Task, Question, AgentRow, Usage, RunResult, Report,
    TASK_TYPES, IMPORTANCES, SIZES, TIERS, next_id, utcnow,
)


def test_status_values_match_spec():
    assert [s.value for s in Status] == [
        "Backlog", "Ready", "Running", "Review", "Changes Requested",
        "Merge Ready", "Blocked", "Failed", "Done", "Cut",
    ]


def test_task_defaults_and_branch():
    t = Task(id="T-007", title="Do thing")
    assert t.status is Status.BACKLOG
    assert t.branch == "task/T-007"
    assert t.title_with_id() == "T-007 · Do thing"
    assert t.depends_on == [] and t.scope == [] and t.flags == []


def test_next_id_pads_and_increments():
    assert next_id("T", []) == "T-001"
    assert next_id("T", ["T-001", "T-009", "junk"]) == "T-010"
    assert next_id("Q", ["Q-120"]) == "Q-121"


def test_constants():
    assert "ml_fusion" in TASK_TYPES and len(TASK_TYPES) == 13
    assert IMPORTANCES == ["critical", "high", "normal", "low"]
    assert SIZES == ["S", "M", "L"] and TIERS == ["best", "high", "mid", "low"]


def test_utcnow_is_aware():
    assert utcnow().tzinfo is timezone.utc


def test_report_and_runresult_defaults():
    r = Report(status="done", summary="ok")
    assert r.files_changed == [] and r.question is None and r.synthesized is False
    rr = RunResult(ok=True, exit_code=0, stdout="", stderr="")
    assert rr.usage == Usage() and rr.rate_limited is False and rr.timed_out is False
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_models.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'swarm.models'`

- [ ] **Step 3: Implement models.py**

```python
# swarm/models.py
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


class Status(str, Enum):
    BACKLOG = "Backlog"
    READY = "Ready"
    RUNNING = "Running"
    REVIEW = "Review"
    CHANGES_REQUESTED = "Changes Requested"
    MERGE_READY = "Merge Ready"
    BLOCKED = "Blocked"
    FAILED = "Failed"
    DONE = "Done"
    CUT = "Cut"


STATUS_ORDER = [s.value for s in Status]
TASK_TYPES = [
    "frontend", "backend", "realtime", "ml_audio", "ml_vision", "ml_fusion",
    "eval", "tests", "docs", "research", "bugfix", "integration", "infra",
]
IMPORTANCES = ["critical", "high", "normal", "low"]
SIZES = ["S", "M", "L"]
TIERS = ["best", "high", "mid", "low"]
PROVIDERS = ["claude", "codex", "antigravity", "gemini", "grok", "generic"]
REVIEW_POLICIES = ["all", "high_and_above", "critical_only", "none"]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


_ID_RE = re.compile(r"^([A-Z]+)-(\d+)$")


def next_id(prefix: str, existing_ids) -> str:
    high = 0
    for eid in existing_ids:
        m = _ID_RE.match(eid or "")
        if m and m.group(1) == prefix:
            high = max(high, int(m.group(2)))
    return f"{prefix}-{high + 1:03d}"


@dataclass
class Task:
    id: str
    title: str
    description: str = ""
    acceptance: str = ""
    type: str = "backend"
    importance: str = "normal"
    size: str = "M"
    milestone: str = ""
    priority: int = 100
    agent: str | None = None
    model: str | None = None
    effort: str | None = None
    status: Status = Status.BACKLOG
    depends_on: list[str] = field(default_factory=list)
    scope: list[str] = field(default_factory=list)
    feedback: str = ""
    pr_url: str = ""
    attempts: int = 0
    review_rounds: int = 0
    claim_nonce: str = ""
    last_error: str = ""
    flags: list[str] = field(default_factory=list)
    started: datetime | None = None
    updated: datetime | None = None
    page_id: str = ""

    @property
    def branch(self) -> str:
        return f"task/{self.id}"

    def title_with_id(self) -> str:
        return f"{self.id} · {self.title}"


@dataclass
class Question:
    id: str
    text: str
    kind: str = "blocking"  # blocking | fyi
    context: str = ""
    options: list[str] = field(default_factory=list)
    proceeding_with: str = ""
    impact: str = "medium"
    task_id: str = ""
    asked_by: str = ""
    status: str = "Open"  # Open | Applied
    answer: str = ""
    needs_follow_up: bool = False
    page_id: str = ""


@dataclass
class AgentRow:
    name: str
    provider: str = ""
    host: str = ""
    status: str = "idle"  # idle | running | cooldown | offline
    current_task: str = ""
    last_heartbeat: datetime | None = None
    cooldown_until: datetime | None = None
    runs: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    cost_5h_usd: float = 0.0
    note: str = ""
    page_id: str = ""


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float | None = None


@dataclass
class RunResult:
    ok: bool
    exit_code: int
    stdout: str
    stderr: str
    structured_output: dict | None = None
    usage: Usage = field(default_factory=Usage)
    session_id: str | None = None
    rate_limited: bool = False
    reset_at: datetime | None = None
    timed_out: bool = False
    error: str = ""


@dataclass
class Report:
    status: str  # done | blocked | failed
    summary: str = ""
    files_changed: list[str] = field(default_factory=list)
    tests: dict = field(default_factory=dict)
    debts: list[dict] = field(default_factory=list)
    decisions: list[dict] = field(default_factory=list)
    question: dict | None = None
    notes_for_reviewer: str = ""
    synthesized: bool = False
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_models.py -v`
Expected: 6 passed

- [ ] **Step 5: Commit**

```bash
git add swarm/models.py tests/test_models.py
git commit -m "feat: core dataclasses and enums"
```

---

### Task 2: Config loader

**Files:**
- Create: `swarm/config.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Consumes: `swarm.models` constants.
- Produces: `load_config(path: Path) -> Config`; dataclasses `Config, AgentConfig, HostConfig, TaskLimit, VerifyConfig, RoutingConfig, RoleConfig, NotionIds`; `Config.repo_root: Path`; `Config.agents_on_host(host) -> list[AgentConfig]`; `Config.limit_for(size) -> TaskLimit`; `save_notion_ids(cfg, ids: dict)`; `ConfigError`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_config.py
import pytest
import yaml
from swarm.config import load_config, ConfigError, save_notion_ids


def test_loads_sample(project_dir):
    cfg = load_config(project_dir / ".swarm" / "config.yaml")
    assert cfg.project == "demo"
    assert cfg.repo_root == project_dir
    assert cfg.worktree_root == (project_dir / ".." / "demo-wt").resolve()
    assert cfg.limit_for("M").turns == 60
    assert cfg.agents["claude-a"].parallel == 2
    assert cfg.agents["codex-a"].parallel == 1
    assert [a.name for a in cfg.agents_on_host("host-b")] == ["fake-b"]
    assert cfg.agents["claude-a"].strengths["ml_audio"] == 3  # default when missing
    assert cfg.event_start.tzinfo is not None
    assert cfg.notion.tasks_ds == ""  # no notion.yaml yet


def test_rejects_unknown_host(project_dir, sample_config_dict):
    sample_config_dict["agents"]["claude-a"]["host"] = "nowhere"
    (project_dir / ".swarm" / "config.yaml").write_text(yaml.safe_dump(sample_config_dict))
    with pytest.raises(ConfigError, match="unknown host"):
        load_config(project_dir / ".swarm" / "config.yaml")


def test_rejects_missing_tier(project_dir, sample_config_dict):
    del sample_config_dict["agents"]["codex-a"]["models"]["low"]
    (project_dir / ".swarm" / "config.yaml").write_text(yaml.safe_dump(sample_config_dict))
    with pytest.raises(ConfigError, match="models.low"):
        load_config(project_dir / ".swarm" / "config.yaml")


def test_rejects_bad_review_policy(project_dir, sample_config_dict):
    sample_config_dict["review_policy"] = "sometimes"
    (project_dir / ".swarm" / "config.yaml").write_text(yaml.safe_dump(sample_config_dict))
    with pytest.raises(ConfigError, match="review_policy"):
        load_config(project_dir / ".swarm" / "config.yaml")


def test_generic_requires_command_template(project_dir, sample_config_dict):
    del sample_config_dict["agents"]["fake-b"]["command_template"]
    (project_dir / ".swarm" / "config.yaml").write_text(yaml.safe_dump(sample_config_dict))
    with pytest.raises(ConfigError, match="command_template"):
        load_config(project_dir / ".swarm" / "config.yaml")


def test_notion_ids_roundtrip(project_dir):
    cfg = load_config(project_dir / ".swarm" / "config.yaml")
    save_notion_ids(cfg, {"tasks_db": "db1", "tasks_ds": "ds1"})
    cfg2 = load_config(project_dir / ".swarm" / "config.yaml")
    assert cfg2.notion.tasks_db == "db1" and cfg2.notion.tasks_ds == "ds1"
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_config.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement config.py**

```python
# swarm/config.py
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
    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.fromisoformat(str(value))
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
        l = limits_raw.get(size)
        if not l:
            raise ConfigError(f"task_limits.{size} is required")
        task_limits[size] = TaskLimit(turns=int(l["turns"]), minutes=int(l["minutes"]),
                                      budget_usd=l.get("budget_usd"))

    routing_raw = raw.get("routing") or {}
    routing = RoutingConfig(
        importance_to_tier=dict(routing_raw.get("importance_to_tier") or
                                {"critical": "best", "high": "high", "normal": "mid", "low": "low"}),
        best_tier_only_when=dict(routing_raw.get("best_tier_only_when") or {"importance": "critical"}),
        type_model_overrides=dict(routing_raw.get("type_model_overrides") or {}),
    )
    for imp in IMPORTANCES:
        if routing.importance_to_tier.get(imp) not in TIERS:
            raise ConfigError(f"routing.importance_to_tier.{imp} must be one of {TIERS}")

    verify_raw = raw.get("verify") or {}
    event = raw.get("event") or {}
    now = datetime.now(timezone.utc)
    worktree_root = (repo_root / raw.get("worktree_root", f"../{raw.get('project','project')}-wt")).resolve()

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
    existing = yaml.safe_load(notion_file.read_text()) if notion_file.exists() else {}
    existing = existing or {}
    existing.update({k: v for k, v in ids.items() if k in NotionIds.__dataclass_fields__})
    notion_file.write_text(yaml.safe_dump(existing, sort_keys=True))
    return notion_file
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_config.py -v`
Expected: 6 passed

- [ ] **Step 5: Commit**

```bash
git add swarm/config.py tests/test_config.py
git commit -m "feat: config loader with validation and notion id merge"
```

---

### Task 3: Board protocol and in-memory board

**Files:**
- Create: `swarm/board/__init__.py`, `swarm/board/base.py`, `swarm/board/memory.py`
- Test: `tests/test_board_memory.py`

**Interfaces:**
- Consumes: `swarm.models`.
- Produces: `Board` Protocol with methods below; `InMemoryBoard`; `claim_task(board, task, agent, *, sleep, nonce) -> bool`.

```python
class Board(Protocol):
    def create_task(self, task: Task) -> Task: ...           # assigns id if empty, page_id
    def get_task(self, task_id: str) -> Task | None: ...
    def list_tasks(self, *, status=None, agent=None) -> list[Task]: ...  # iterables of Status / agent names
    def update_task(self, task: Task, fields: Iterable[str]) -> Task: ...  # writes only named attrs
    def append_task_report(self, task: Task, heading: str, markdown: str) -> None: ...
    def create_question(self, q: Question) -> Question: ...
    def list_questions(self, *, status=None) -> list[Question]: ...
    def update_question(self, q: Question, fields: Iterable[str]) -> Question: ...
    def upsert_agent(self, row: AgentRow) -> AgentRow: ...
    def get_agent(self, name: str) -> AgentRow | None: ...
    def list_agents(self) -> list[AgentRow]: ...
    def write_status_page(self, text: str) -> None: ...
    def next_task_id(self) -> str: ...
    def next_question_id(self) -> str: ...
```

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_board_memory.py
from swarm.board.memory import InMemoryBoard
from swarm.board.base import claim_task
from swarm.models import Task, Question, AgentRow, Status


def test_create_assigns_ids_and_lists():
    b = InMemoryBoard()
    t1 = b.create_task(Task(id="", title="one", status=Status.READY, agent="a"))
    t2 = b.create_task(Task(id="", title="two", status=Status.BACKLOG))
    assert (t1.id, t2.id) == ("T-001", "T-002")
    assert [t.id for t in b.list_tasks(status=[Status.READY])] == ["T-001"]
    assert [t.id for t in b.list_tasks(agent=["a"])] == ["T-001"]
    assert b.list_tasks(status=[Status.DONE]) == []


def test_get_task_by_id_not_title():
    b = InMemoryBoard()
    b.create_task(Task(id="T-001", title="same"))
    b.create_task(Task(id="T-002", title="same"))
    assert b.get_task("T-002").id == "T-002"
    assert b.get_task("same") is None


def test_update_only_named_fields():
    b = InMemoryBoard()
    t = b.create_task(Task(id="", title="x", status=Status.READY))
    t.status = Status.RUNNING
    t.title = "changed locally"
    b.update_task(t, ["status"])
    stored = b.get_task(t.id)
    assert stored.status is Status.RUNNING and stored.title == "x"


def test_reports_questions_agents_status():
    b = InMemoryBoard()
    t = b.create_task(Task(id="", title="x"))
    b.append_task_report(t, "Report — attempt 1", "did stuff")
    assert b.reports[t.id][0] == ("Report — attempt 1", "did stuff")
    q = b.create_question(Question(id="", text="which?", task_id=t.id))
    assert q.id == "Q-001" and b.list_questions(status="Open")[0].text == "which?"
    q.answer = "b"
    b.update_question(q, ["answer"])
    assert b.list_questions()[0].answer == "b"
    row = b.upsert_agent(AgentRow(name="a", provider="claude"))
    row.runs = 3
    b.upsert_agent(row)
    assert b.get_agent("a").runs == 3 and len(b.list_agents()) == 1
    b.write_status_page("hello")
    assert b.status_page == "hello"


def test_claim_task_succeeds_and_detects_race():
    b = InMemoryBoard()
    t = b.create_task(Task(id="", title="x", status=Status.READY, agent="a"))
    assert claim_task(b, t, "a", sleep=lambda s: None, nonce="n1") is True
    assert b.get_task(t.id).status is Status.RUNNING
    # simulate another claimant overwriting the nonce mid-claim
    t2 = b.create_task(Task(id="", title="y", status=Status.READY, agent="a"))

    def hijack(_):
        stored = b.get_task(t2.id)
        stored.claim_nonce = "other"
        b.update_task(stored, ["claim_nonce"])

    assert claim_task(b, t2, "a", sleep=hijack, nonce="mine") is False
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_board_memory.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement base.py and memory.py**

`swarm/board/__init__.py`:
```python
from .base import Board, claim_task  # noqa: F401
from .memory import InMemoryBoard  # noqa: F401
```

`swarm/board/base.py`:
```python
from __future__ import annotations

import time
import uuid
from typing import Callable, Iterable, Protocol

from ..models import AgentRow, Question, Status, Task, utcnow


class Board(Protocol):
    def create_task(self, task: Task) -> Task: ...
    def get_task(self, task_id: str) -> Task | None: ...
    def list_tasks(self, *, status: Iterable[Status] | None = None,
                   agent: Iterable[str] | None = None) -> list[Task]: ...
    def update_task(self, task: Task, fields: Iterable[str]) -> Task: ...
    def append_task_report(self, task: Task, heading: str, markdown: str) -> None: ...
    def create_question(self, q: Question) -> Question: ...
    def list_questions(self, *, status: str | None = None) -> list[Question]: ...
    def update_question(self, q: Question, fields: Iterable[str]) -> Question: ...
    def upsert_agent(self, row: AgentRow) -> AgentRow: ...
    def get_agent(self, name: str) -> AgentRow | None: ...
    def list_agents(self) -> list[AgentRow]: ...
    def write_status_page(self, text: str) -> None: ...
    def next_task_id(self) -> str: ...
    def next_question_id(self) -> str: ...


def claim_task(board: Board, task: Task, agent: str, *, sleep: Callable[[float], None] = time.sleep,
               nonce: str | None = None, wait_s: float = 1.5) -> bool:
    """Optimistic claim: write Running + nonce, wait, re-read, confirm nonce survived."""
    nonce = nonce or uuid.uuid4().hex
    task.status = Status.RUNNING
    task.claim_nonce = nonce
    task.agent = agent
    task.started = utcnow()
    board.update_task(task, ["status", "claim_nonce", "agent", "started"])
    sleep(wait_s)
    fresh = board.get_task(task.id)
    if fresh is None or fresh.claim_nonce != nonce or fresh.status is not Status.RUNNING:
        return False
    return True
```

`swarm/board/memory.py`:
```python
from __future__ import annotations

import copy
from typing import Iterable

from ..models import AgentRow, Question, Status, Task, next_id, utcnow


class InMemoryBoard:
    def __init__(self) -> None:
        self.tasks: dict[str, Task] = {}
        self.questions: dict[str, Question] = {}
        self.agents: dict[str, AgentRow] = {}
        self.reports: dict[str, list[tuple[str, str]]] = {}
        self.status_page = ""

    # tasks
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

    def get_task(self, task_id: str) -> Task | None:
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
        stored = self.tasks[task.id]
        for f in fields:
            setattr(stored, f, copy.deepcopy(getattr(task, f)))
        stored.updated = utcnow()
        return copy.deepcopy(stored)

    def append_task_report(self, task: Task, heading: str, markdown: str) -> None:
        self.reports.setdefault(task.id, []).append((heading, markdown))

    # questions
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

    # agents
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

    def write_status_page(self, text: str) -> None:
        self.status_page = text
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_board_memory.py -v`
Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add swarm/board tests/test_board_memory.py
git commit -m "feat: board protocol, in-memory board, optimistic claim"
```

---

### Task 4: Notion property mapping and database schemas

**Files:**
- Create: `swarm/board/notion_props.py`
- Test: `tests/test_notion_props.py`

**Interfaces:**
- Consumes: `swarm.models`.
- Produces: builders `p_title, p_rich, p_select, p_number, p_url, p_checkbox, p_date, p_relation`; readers `r_title, r_rich, r_select, r_number, r_url, r_checkbox, r_date, r_relation`; `task_to_props(task, fields=None) -> dict`; `page_to_task(page) -> Task`; `question_to_props / page_to_question`; `agent_to_props / page_to_agent`; schemas `TASKS_SCHEMA(agent_names)`, `QUESTIONS_SCHEMA(tasks_ds_id)`, `AGENTS_SCHEMA`; constant `TASK_PROP_NAMES` (attr → Notion property name).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_notion_props.py
from datetime import datetime, timezone
from swarm.board.notion_props import (
    p_rich, r_rich, p_select, r_select, p_date, r_date, task_to_props, page_to_task,
    question_to_props, page_to_question, agent_to_props, page_to_agent, TASKS_SCHEMA,
)
from swarm.models import Task, Question, AgentRow, Status


def test_p_rich_chunks_long_text():
    long = "x" * 4500
    prop = p_rich(long)
    assert [len(c["text"]["content"]) for c in prop["rich_text"]] == [2000, 2000, 500]
    assert r_rich(prop) == long


def test_p_rich_empty_and_select_none():
    assert p_rich("") == {"rich_text": []}
    assert p_select("") == {"select": None}
    assert r_select({"select": None}) == ""


def test_date_roundtrip():
    dt = datetime(2026, 10, 10, 16, 0, tzinfo=timezone.utc)
    assert r_date(p_date(dt)) == dt
    assert p_date(None) == {"date": None} and r_date({"date": None}) is None


def test_task_roundtrip():
    t = Task(id="T-012", title="Degraded state", description="d", acceptance="a", type="frontend",
             importance="high", size="M", milestone="M1", priority=5, agent="claude-a", model="opus",
             effort="high", status=Status.REVIEW, depends_on=["T-003", "T-004"], scope=["frontend/**"],
             feedback="fix x", pr_url="https://github.com/x/y/pull/1", attempts=2, review_rounds=1,
             claim_nonce="abc", last_error="boom", flags=["report_missing"],
             started=datetime(2026, 10, 10, 16, 0, tzinfo=timezone.utc))
    props = task_to_props(t)
    assert props["Name"]["title"][0]["text"]["content"] == "T-012 · Degraded state"
    assert props["Status"]["select"]["name"] == "Review"
    assert props["Depends On"]["rich_text"][0]["text"]["content"] == "T-003, T-004"
    page = {"id": "pg1", "properties": {k: {**v, "type": next(iter(v))} for k, v in props.items()}}
    back = page_to_task(page)
    assert back.page_id == "pg1" and back.id == "T-012" and back.title == "Degraded state"
    assert back.depends_on == ["T-003", "T-004"] and back.flags == ["report_missing"]
    assert back.status is Status.REVIEW and back.attempts == 2 and back.started == t.started


def test_task_partial_fields():
    t = Task(id="T-001", title="x", status=Status.RUNNING, claim_nonce="n")
    props = task_to_props(t, ["status", "claim_nonce"])
    assert set(props) == {"Status", "Claim Nonce"}


def test_question_and_agent_roundtrip():
    q = Question(id="Q-004", text="Tap or rail?", kind="fyi", context="c", options=["tap", "rail"],
                 proceeding_with="tap", impact="high", task_id="T-011", asked_by="claude-a",
                 answer="rail", needs_follow_up=True)
    props = question_to_props(q, task_page_id="pg-t011")
    assert props["Task"]["relation"] == [{"id": "pg-t011"}]
    page = {"id": "pgq", "properties": {k: {**v, "type": next(iter(v))} for k, v in props.items()}}
    back = page_to_question(page)
    assert back.id == "Q-004" and back.options == ["tap", "rail"] and back.needs_follow_up is True
    a = AgentRow(name="claude-a", provider="claude", host="h", status="cooldown", runs=4, cost_usd=1.5,
                 cooldown_until=datetime(2026, 10, 10, 16, 0, tzinfo=timezone.utc))
    aprops = agent_to_props(a)
    apage = {"id": "pga", "properties": {k: {**v, "type": next(iter(v))} for k, v in aprops.items()}}
    aback = page_to_agent(apage)
    assert aback.name == "claude-a" and aback.runs == 4 and aback.cooldown_until == a.cooldown_until


def test_tasks_schema_has_agent_options():
    schema = TASKS_SCHEMA(["claude-a", "codex-a"])
    names = [o["name"] for o in schema["Agent"]["select"]["options"]]
    assert names == ["claude-a", "codex-a"]
    assert "Backlog" in [o["name"] for o in schema["Status"]["select"]["options"]]
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_notion_props.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement notion_props.py**

```python
# swarm/board/notion_props.py
from __future__ import annotations

from datetime import datetime
from typing import Iterable

from ..models import IMPORTANCES, SIZES, STATUS_ORDER, TASK_TYPES, AgentRow, Question, Status, Task

CHUNK = 2000


def _chunks(s: str) -> list[str]:
    return [s[i:i + CHUNK] for i in range(0, len(s), CHUNK)][:100]


# ---------- builders ----------
def p_title(s: str) -> dict:
    return {"title": [{"type": "text", "text": {"content": (s or "")[:CHUNK]}}]}


def p_rich(s: str) -> dict:
    return {"rich_text": [{"type": "text", "text": {"content": c}} for c in _chunks(s or "")]}


def p_select(name: str | None) -> dict:
    return {"select": {"name": name} if name else None}


def p_number(n) -> dict:
    return {"number": n}


def p_url(u: str | None) -> dict:
    return {"url": u or None}


def p_checkbox(b: bool) -> dict:
    return {"checkbox": bool(b)}


def p_date(dt: datetime | None) -> dict:
    return {"date": {"start": dt.isoformat()} if dt else None}


def p_relation(ids: Iterable[str]) -> dict:
    return {"relation": [{"id": i} for i in ids]}


# ---------- readers ----------
def _plain(arr) -> str:
    return "".join(x.get("plain_text") or x.get("text", {}).get("content", "") for x in (arr or []))


def r_title(p: dict) -> str:
    return _plain(p.get("title"))


def r_rich(p: dict) -> str:
    return _plain(p.get("rich_text"))


def r_select(p: dict) -> str:
    sel = p.get("select")
    return sel.get("name", "") if sel else ""


def r_number(p: dict):
    return p.get("number")


def r_url(p: dict) -> str:
    return p.get("url") or ""


def r_checkbox(p: dict) -> bool:
    return bool(p.get("checkbox"))


def r_date(p: dict) -> datetime | None:
    d = p.get("date")
    if not d or not d.get("start"):
        return None
    return datetime.fromisoformat(d["start"].replace("Z", "+00:00"))


def r_relation(p: dict) -> list[str]:
    return [x["id"] for x in (p.get("relation") or [])]


def _csv(items: list[str]) -> str:
    return ", ".join(items)


def _uncsv(s: str) -> list[str]:
    return [x.strip() for x in (s or "").split(",") if x.strip()]


# ---------- tasks ----------
TASK_PROP_NAMES = {
    "title": "Name", "id": "ID", "description": "Description", "acceptance": "Acceptance",
    "type": "Type", "importance": "Importance", "size": "Size", "milestone": "Milestone",
    "priority": "Priority", "agent": "Agent", "model": "Model", "effort": "Effort", "status": "Status",
    "depends_on": "Depends On", "scope": "Scope", "feedback": "Feedback", "pr_url": "PR",
    "attempts": "Attempts", "review_rounds": "Review Rounds", "claim_nonce": "Claim Nonce",
    "last_error": "Last Error", "flags": "Flags", "started": "Started",
}


def task_to_props(t: Task, fields: Iterable[str] | None = None) -> dict:
    fields = list(fields) if fields is not None else list(TASK_PROP_NAMES)
    if "title" in fields or "id" in fields:
        fields = list(dict.fromkeys(fields + ["title", "id"]))
    out = {}
    for f in fields:
        name = TASK_PROP_NAMES[f]
        v = getattr(t, f)
        if f == "title":
            out[name] = p_title(t.title_with_id())
        elif f == "status":
            out[name] = p_select(v.value if isinstance(v, Status) else v)
        elif f in ("type", "importance", "size", "milestone", "agent"):
            out[name] = p_select(v)
        elif f in ("priority", "attempts", "review_rounds"):
            out[name] = p_number(v)
        elif f == "pr_url":
            out[name] = p_url(v)
        elif f in ("depends_on", "scope", "flags"):
            out[name] = p_rich(_csv(v))
        elif f == "started":
            out[name] = p_date(v)
        else:
            out[name] = p_rich(v or "")
    return out


def page_to_task(page: dict) -> Task:
    p = page["properties"]
    g = lambda name: p.get(name, {})  # noqa: E731
    full_title = r_title(g("Name"))
    tid = r_rich(g("ID")) or full_title.split(" · ")[0]
    title = full_title.split(" · ", 1)[1] if " · " in full_title else full_title
    status_name = r_select(g("Status")) or "Backlog"
    return Task(
        id=tid, title=title, description=r_rich(g("Description")), acceptance=r_rich(g("Acceptance")),
        type=r_select(g("Type")) or "backend", importance=r_select(g("Importance")) or "normal",
        size=r_select(g("Size")) or "M", milestone=r_select(g("Milestone")),
        priority=int(r_number(g("Priority")) or 100), agent=r_select(g("Agent")) or None,
        model=r_rich(g("Model")) or None, effort=r_rich(g("Effort")) or None,
        status=Status(status_name) if status_name in STATUS_ORDER else Status.BACKLOG,
        depends_on=_uncsv(r_rich(g("Depends On"))), scope=_uncsv(r_rich(g("Scope"))),
        feedback=r_rich(g("Feedback")), pr_url=r_url(g("PR")), attempts=int(r_number(g("Attempts")) or 0),
        review_rounds=int(r_number(g("Review Rounds")) or 0), claim_nonce=r_rich(g("Claim Nonce")),
        last_error=r_rich(g("Last Error")), flags=_uncsv(r_rich(g("Flags"))), started=r_date(g("Started")),
        updated=datetime.fromisoformat(page["last_edited_time"].replace("Z", "+00:00"))
        if page.get("last_edited_time") else None,
        page_id=page.get("id", ""),
    )


def TASKS_SCHEMA(agent_names: list[str]) -> dict:
    sel = lambda names: {"select": {"options": [{"name": n} for n in names]}}  # noqa: E731
    return {
        "Name": {"title": {}}, "ID": {"rich_text": {}}, "Description": {"rich_text": {}},
        "Acceptance": {"rich_text": {}}, "Type": sel(TASK_TYPES), "Importance": sel(IMPORTANCES),
        "Size": sel(SIZES), "Milestone": sel(["M0", "M1", "M2", "M3", "M4"]),
        "Priority": {"number": {"format": "number"}}, "Agent": sel(agent_names),
        "Model": {"rich_text": {}}, "Effort": {"rich_text": {}}, "Status": sel(STATUS_ORDER),
        "Depends On": {"rich_text": {}}, "Scope": {"rich_text": {}}, "Feedback": {"rich_text": {}},
        "PR": {"url": {}}, "Attempts": {"number": {"format": "number"}},
        "Review Rounds": {"number": {"format": "number"}}, "Claim Nonce": {"rich_text": {}},
        "Last Error": {"rich_text": {}}, "Flags": {"rich_text": {}}, "Started": {"date": {}},
    }


# ---------- questions ----------
def question_to_props(q: Question, *, task_page_id: str | None = None,
                      fields: Iterable[str] | None = None) -> dict:
    all_props = {
        "Question": p_title(f"{q.id} · {q.text}"[:200]), "ID": p_rich(q.id), "Kind": p_select(q.kind),
        "Context": p_rich(q.context), "Options": p_rich("\n".join(q.options)),
        "Proceeding With": p_rich(q.proceeding_with), "Impact": p_select(q.impact),
        "Task ID": p_rich(q.task_id), "Asked By": p_rich(q.asked_by), "Status": p_select(q.status),
        "Answer": p_rich(q.answer), "Needs Follow-up": p_checkbox(q.needs_follow_up),
    }
    if task_page_id:
        all_props["Task"] = p_relation([task_page_id])
    if fields is None:
        return all_props
    names = {"status": "Status", "answer": "Answer", "needs_follow_up": "Needs Follow-up",
             "text": "Question", "context": "Context"}
    return {names[f]: all_props[names[f]] for f in fields}


def page_to_question(page: dict) -> Question:
    p = page["properties"]
    g = lambda name: p.get(name, {})  # noqa: E731
    title = r_title(g("Question"))
    qid = r_rich(g("ID")) or title.split(" · ")[0]
    text = title.split(" · ", 1)[1] if " · " in title else title
    return Question(
        id=qid, text=text, kind=r_select(g("Kind")) or "blocking", context=r_rich(g("Context")),
        options=[o for o in r_rich(g("Options")).split("\n") if o], proceeding_with=r_rich(g("Proceeding With")),
        impact=r_select(g("Impact")) or "medium", task_id=r_rich(g("Task ID")), asked_by=r_rich(g("Asked By")),
        status=r_select(g("Status")) or "Open", answer=r_rich(g("Answer")),
        needs_follow_up=r_checkbox(g("Needs Follow-up")), page_id=page.get("id", ""),
    )


def QUESTIONS_SCHEMA(tasks_ds_id: str) -> dict:
    sel = lambda names: {"select": {"options": [{"name": n} for n in names]}}  # noqa: E731
    return {
        "Question": {"title": {}}, "ID": {"rich_text": {}}, "Kind": sel(["blocking", "fyi"]),
        "Context": {"rich_text": {}}, "Options": {"rich_text": {}}, "Proceeding With": {"rich_text": {}},
        "Impact": sel(["high", "medium", "low"]),
        "Task": {"relation": {"data_source_id": tasks_ds_id, "type": "single_property", "single_property": {}}},
        "Task ID": {"rich_text": {}}, "Asked By": {"rich_text": {}}, "Status": sel(["Open", "Applied"]),
        "Answer": {"rich_text": {}}, "Needs Follow-up": {"checkbox": {}},
    }


# ---------- agents ----------
def agent_to_props(a: AgentRow) -> dict:
    return {
        "Name": p_title(a.name), "Provider": p_select(a.provider), "Host": p_rich(a.host),
        "Status": p_select(a.status), "Current Task": p_rich(a.current_task),
        "Last Heartbeat": p_date(a.last_heartbeat), "Cooldown Until": p_date(a.cooldown_until),
        "Runs": p_number(a.runs), "Tokens In": p_number(a.tokens_in), "Tokens Out": p_number(a.tokens_out),
        "Cost USD": p_number(round(a.cost_usd, 4)), "Cost 5h USD": p_number(round(a.cost_5h_usd, 4)),
        "Note": p_rich(a.note),
    }


def page_to_agent(page: dict) -> AgentRow:
    p = page["properties"]
    g = lambda name: p.get(name, {})  # noqa: E731
    return AgentRow(
        name=r_title(g("Name")), provider=r_select(g("Provider")), host=r_rich(g("Host")),
        status=r_select(g("Status")) or "idle", current_task=r_rich(g("Current Task")),
        last_heartbeat=r_date(g("Last Heartbeat")), cooldown_until=r_date(g("Cooldown Until")),
        runs=int(r_number(g("Runs")) or 0), tokens_in=int(r_number(g("Tokens In")) or 0),
        tokens_out=int(r_number(g("Tokens Out")) or 0), cost_usd=float(r_number(g("Cost USD")) or 0.0),
        cost_5h_usd=float(r_number(g("Cost 5h USD")) or 0.0), note=r_rich(g("Note")), page_id=page.get("id", ""),
    )


AGENTS_SCHEMA = {
    "Name": {"title": {}},
    "Provider": {"select": {"options": [{"name": n} for n in
                                        ["claude", "codex", "antigravity", "gemini", "grok", "generic", "serve"]]}},
    "Host": {"rich_text": {}},
    "Status": {"select": {"options": [{"name": n} for n in ["idle", "running", "cooldown", "offline"]]}},
    "Current Task": {"rich_text": {}}, "Last Heartbeat": {"date": {}}, "Cooldown Until": {"date": {}},
    "Runs": {"number": {"format": "number"}}, "Tokens In": {"number": {"format": "number"}},
    "Tokens Out": {"number": {"format": "number"}}, "Cost USD": {"number": {"format": "number"}},
    "Cost 5h USD": {"number": {"format": "number"}}, "Note": {"rich_text": {}},
}
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_notion_props.py -v`
Expected: 7 passed

- [ ] **Step 5: Commit**

```bash
git add swarm/board/notion_props.py tests/test_notion_props.py
git commit -m "feat: notion property mapping and database schemas"
```

---

### Task 5: Notion client and NotionBoard

**Files:**
- Create: `swarm/board/notion.py`
- Modify: `swarm/board/__init__.py` (export `NotionClient, NotionBoard`)
- Test: `tests/test_notion_client.py`

**Interfaces:**
- Consumes: `notion_props`, `Board` protocol, `NotionIds`.
- Produces:
  - `NotionClient(token, *, version="2026-03-11", transport=None, sleep=time.sleep)` with `request(method, path, json=None) -> dict`, `query(ds_id, filter=None, sorts=None) -> list[dict]`, `create_page(ds_id, props, children=None)`, `update_page(page_id, props)`, `get_page(page_id)`, `append_blocks(block_id, children)`, `update_block(block_id, payload)`, `create_database(parent_page_id, title, properties) -> dict`, `create_view(database_id, ds_id, name, kind, configuration)`, `get_data_source(ds_id)`, `me()`.
  - `NotionError(status, code, message)`.
  - `NotionBoard(client, ids: NotionIds)` implementing `Board`.
  - `NotionBoard.init(client, parent_page_id, agent_names) -> dict` creating the three databases, board views, and a status page with one code block; returns id dict for `save_notion_ids`.
  - `markdown_to_blocks(md) -> list[dict]` (headings `#`/`##`/`###`, fenced code, paragraphs; chunked to 2,000).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_notion_client.py
import json
import httpx
import pytest
from swarm.board.notion import NotionClient, NotionBoard, NotionError, markdown_to_blocks
from swarm.config import NotionIds
from swarm.models import Task, Status, Question, AgentRow


def make_client(handler, sleeps=None):
    transport = httpx.MockTransport(handler)
    sleeps = sleeps if sleeps is not None else []
    return NotionClient("tok", transport=transport, sleep=lambda s: sleeps.append(s)), sleeps


def test_headers_and_me():
    seen = {}

    def handler(req: httpx.Request):
        seen["auth"] = req.headers["Authorization"]
        seen["ver"] = req.headers["Notion-Version"]
        return httpx.Response(200, json={"object": "user", "id": "u1"})

    client, _ = make_client(handler)
    assert client.me()["id"] == "u1"
    assert seen == {"auth": "Bearer tok", "ver": "2026-03-11"}


def test_retries_on_429_with_retry_after():
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(429, headers={"Retry-After": "2"}, json={"code": "rate_limited", "message": "slow"})
        return httpx.Response(200, json={"ok": True})

    client, sleeps = make_client(handler)
    assert client.request("GET", "/users/me") == {"ok": True}
    assert calls["n"] == 3 and sleeps[:2] == [2.0, 2.0]


def test_retry_after_long_wait_is_capped():
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "900"}, json={})
        return httpx.Response(200, json={"ok": True})

    client, sleeps = make_client(handler)
    client.request("GET", "/users/me")
    assert sleeps == [300.0]


def test_gives_up_after_five_retries():
    def handler(req):
        return httpx.Response(429, headers={"Retry-After": "1"}, json={"code": "rate_limited", "message": "no"})

    client, sleeps = make_client(handler)
    with pytest.raises(NotionError) as ei:
        client.request("GET", "/users/me")
    assert ei.value.status == 429 and len(sleeps) == 5


def test_400_raises_without_retry():
    def handler(req):
        return httpx.Response(400, json={"code": "validation_error", "message": "bad"})

    client, sleeps = make_client(handler)
    with pytest.raises(NotionError, match="bad"):
        client.request("POST", "/pages", json={})
    assert sleeps == []


def test_query_paginates():
    pages = {"n": 0}

    def handler(req):
        body = json.loads(req.content)
        pages["n"] += 1
        if body.get("start_cursor") is None:
            return httpx.Response(200, json={"results": [{"id": "a"}], "has_more": True, "next_cursor": "c1"})
        return httpx.Response(200, json={"results": [{"id": "b"}], "has_more": False, "next_cursor": None})

    client, _ = make_client(handler)
    rows = client.query("ds1", filter={"x": 1})
    assert [r["id"] for r in rows] == ["a", "b"] and pages["n"] == 2


def test_markdown_to_blocks():
    blocks = markdown_to_blocks("## Report\n\nline one\nline two\n\n```json\n{\"a\":1}\n```\n")
    assert blocks[0]["type"] == "heading_2"
    assert blocks[1]["type"] == "paragraph" and "line one" in blocks[1]["paragraph"]["rich_text"][0]["text"]["content"]
    assert blocks[2]["type"] == "code" and blocks[2]["code"]["language"] == "json"


def _board_with_fake_notion():
    """A fake Notion that stores pages by data source and supports the calls NotionBoard makes."""
    store = {"ds-tasks": {}, "ds-q": {}, "ds-agents": {}, "blocks": {}}
    counter = {"n": 0}

    def handler(req: httpx.Request):
        path = req.url.path
        body = json.loads(req.content) if req.content else {}
        if req.method == "POST" and path.endswith("/query"):
            ds = path.split("/")[3]
            results = list(store[ds].values())
            flt = body.get("filter")
            if flt:
                def match(page, f):
                    if "and" in f:
                        return all(match(page, x) for x in f["and"])
                    if "or" in f:
                        return any(match(page, x) for x in f["or"])
                    prop = page["properties"][f["property"]]
                    if "select" in f:
                        sel = prop.get("select")
                        return (sel or {}).get("name") == f["select"].get("equals")
                    if "rich_text" in f:
                        text = "".join(x["text"]["content"] for x in prop.get("rich_text", []))
                        if "equals" in f["rich_text"]:
                            return text == f["rich_text"]["equals"]
                        if "is_not_empty" in f["rich_text"]:
                            return bool(text)
                    if "title" in f:
                        text = "".join(x["text"]["content"] for x in prop.get("title", []))
                        return text == f["title"].get("equals")
                    return True
                results = [p for p in results if match(p, flt)]
            return httpx.Response(200, json={"results": results, "has_more": False, "next_cursor": None})
        if req.method == "POST" and path == "/v1/pages":
            counter["n"] += 1
            ds = body["parent"]["data_source_id"]
            page = {"id": f"pg{counter['n']}", "properties": body["properties"], "last_edited_time": "2026-10-10T16:00:00.000Z"}
            store[ds][page["id"]] = page
            return httpx.Response(200, json=page)
        if req.method == "PATCH" and path.startswith("/v1/pages/"):
            pid = path.split("/")[3]
            for ds in ("ds-tasks", "ds-q", "ds-agents"):
                if pid in store[ds]:
                    store[ds][pid]["properties"].update(body["properties"])
                    return httpx.Response(200, json=store[ds][pid])
            return httpx.Response(404, json={"message": "nope"})
        if req.method == "GET" and path.startswith("/v1/pages/"):
            pid = path.split("/")[3]
            for ds in ("ds-tasks", "ds-q", "ds-agents"):
                if pid in store[ds]:
                    return httpx.Response(200, json=store[ds][pid])
            return httpx.Response(404, json={"message": "nope"})
        if req.method == "PATCH" and path.startswith("/v1/blocks/") and path.endswith("/children"):
            bid = path.split("/")[3]
            store["blocks"].setdefault(bid, []).extend(body["children"])
            return httpx.Response(200, json={"results": body["children"]})
        if req.method == "PATCH" and path.startswith("/v1/blocks/"):
            store["blocks"][path.split("/")[3]] = body
            return httpx.Response(200, json=body)
        return httpx.Response(500, json={"message": f"unhandled {req.method} {path}"})

    client = NotionClient("tok", transport=httpx.MockTransport(handler), sleep=lambda s: None)
    ids = NotionIds(tasks_ds="ds-tasks", questions_ds="ds-q", agents_ds="ds-agents",
                    status_page="sp", status_block="sb")
    return NotionBoard(client, ids), store


def test_board_task_crud():
    board, store = _board_with_fake_notion()
    t = board.create_task(Task(id="", title="first", status=Status.READY, agent="claude-a"))
    assert t.id == "T-001" and t.page_id == "pg1"
    t2 = board.create_task(Task(id="", title="second"))
    assert t2.id == "T-002"
    assert [x.id for x in board.list_tasks(status=[Status.READY], agent=["claude-a"])] == ["T-001"]
    t.status = Status.RUNNING
    board.update_task(t, ["status"])
    assert board.get_task("T-001").status is Status.RUNNING
    board.append_task_report(t, "Report — attempt 1", "hello")
    assert store["blocks"]["pg1"][0]["type"] == "heading_2"


def test_board_questions_agents_status():
    board, store = _board_with_fake_notion()
    t = board.create_task(Task(id="", title="first"))
    q = board.create_question(Question(id="", text="why?", task_id=t.id))
    assert q.id == "Q-001"
    assert board.list_questions(status="Open")[0].text == "why?"
    q.answer = "because"
    board.update_question(q, ["answer"])
    assert board.list_questions()[0].answer == "because"
    row = board.upsert_agent(AgentRow(name="claude-a", provider="claude"))
    row.runs = 2
    board.upsert_agent(row)
    assert board.get_agent("claude-a").runs == 2 and len(board.list_agents()) == 1
    board.write_status_page("status text")
    assert store["blocks"]["sb"]["code"]["rich_text"][0]["text"]["content"] == "status text"
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_notion_client.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement notion.py**

```python
# swarm/board/notion.py
from __future__ import annotations

import random
import re
import time
from typing import Callable, Iterable

import httpx

from ..config import NotionIds
from ..models import AgentRow, Question, Status, Task, next_id, utcnow
from . import notion_props as np

API = "https://api.notion.com/v1"
VERSION = "2026-03-11"
MAX_RETRIES = 5
MAX_SLEEP = 300.0


class NotionError(RuntimeError):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(f"Notion {status} {code}: {message}")
        self.status, self.code, self.message = status, code, message


class NotionClient:
    def __init__(self, token: str, *, version: str = VERSION, transport=None,
                 sleep: Callable[[float], None] = time.sleep, timeout: float = 30.0):
        self._sleep = sleep
        self._http = httpx.Client(
            base_url=API, transport=transport, timeout=timeout,
            headers={"Authorization": f"Bearer {token}", "Notion-Version": version,
                     "Content-Type": "application/json"},
        )

    def request(self, method: str, path: str, json: dict | None = None) -> dict:
        attempt = 0
        while True:
            try:
                resp = self._http.request(method, path, json=json)
            except httpx.TransportError as e:
                if attempt >= MAX_RETRIES:
                    raise NotionError(0, "transport", str(e)) from e
                attempt += 1
                self._sleep(min(MAX_SLEEP, 2 ** attempt + random.random()))
                continue
            if resp.status_code < 400:
                return resp.json() if resp.content else {}
            body = {}
            try:
                body = resp.json()
            except ValueError:
                pass
            code = body.get("code", "http_error")
            message = body.get("message", resp.text[:200])
            retryable = resp.status_code in (429, 500, 502, 503, 504, 529)
            if not retryable or attempt >= MAX_RETRIES:
                raise NotionError(resp.status_code, code, message)
            attempt += 1
            ra = resp.headers.get("Retry-After")
            if ra is None:
                ra = (body.get("additional_data") or {}).get("retry_after")
            wait = float(ra) if ra is not None else (2 ** attempt + random.random())
            self._sleep(min(MAX_SLEEP, wait))

    # convenience wrappers
    def me(self) -> dict:
        return self.request("GET", "/users/me")

    def query(self, ds_id: str, filter: dict | None = None, sorts: list | None = None) -> list[dict]:
        out, cursor = [], None
        while True:
            body: dict = {"page_size": 100}
            if filter:
                body["filter"] = filter
            if sorts:
                body["sorts"] = sorts
            if cursor:
                body["start_cursor"] = cursor
            data = self.request("POST", f"/data_sources/{ds_id}/query", json=body)
            out.extend(data.get("results", []))
            if not data.get("has_more"):
                return out
            cursor = data.get("next_cursor")

    def create_page(self, ds_id: str, props: dict, children: list | None = None) -> dict:
        body = {"parent": {"type": "data_source_id", "data_source_id": ds_id}, "properties": props}
        if children:
            body["children"] = children[:100]
        return self.request("POST", "/pages", json=body)

    def create_child_page(self, parent_page_id: str, title: str, children: list | None = None) -> dict:
        body = {"parent": {"type": "page_id", "page_id": parent_page_id},
                "properties": {"title": np.p_title(title)["title"]}}
        if children:
            body["children"] = children[:100]
        return self.request("POST", "/pages", json=body)

    def update_page(self, page_id: str, props: dict) -> dict:
        return self.request("PATCH", f"/pages/{page_id}", json={"properties": props})

    def get_page(self, page_id: str) -> dict:
        return self.request("GET", f"/pages/{page_id}")

    def append_blocks(self, block_id: str, children: list) -> dict:
        out = {}
        for i in range(0, len(children), 100):
            out = self.request("PATCH", f"/blocks/{block_id}/children", json={"children": children[i:i + 100]})
        return out

    def update_block(self, block_id: str, payload: dict) -> dict:
        return self.request("PATCH", f"/blocks/{block_id}", json=payload)

    def list_children(self, block_id: str) -> list[dict]:
        return self.request("GET", f"/blocks/{block_id}/children?page_size=100").get("results", [])

    def create_database(self, parent_page_id: str, title: str, properties: dict) -> dict:
        return self.request("POST", "/databases", json={
            "parent": {"type": "page_id", "page_id": parent_page_id},
            "title": [{"type": "text", "text": {"content": title}}],
            "initial_data_source": {"properties": properties},
        })

    def get_data_source(self, ds_id: str) -> dict:
        return self.request("GET", f"/data_sources/{ds_id}")

    def create_view(self, database_id: str, ds_id: str, name: str, kind: str, configuration: dict) -> dict:
        return self.request("POST", "/views", json={
            "database_id": database_id, "data_source_id": ds_id, "name": name, "type": kind,
            "configuration": configuration,
        })


# ---------- markdown → blocks ----------
def _rt(text: str) -> list[dict]:
    return [{"type": "text", "text": {"content": c}} for c in np._chunks(text)] or \
        [{"type": "text", "text": {"content": ""}}]


def markdown_to_blocks(md: str) -> list[dict]:
    blocks: list[dict] = []
    lines = md.splitlines()
    i = 0
    para: list[str] = []

    def flush():
        if para:
            blocks.append({"object": "block", "type": "paragraph", "paragraph": {"rich_text": _rt("\n".join(para))}})
            para.clear()

    while i < len(lines):
        line = lines[i]
        if line.startswith("```"):
            flush()
            lang = line[3:].strip() or "plain text"
            i += 1
            code: list[str] = []
            while i < len(lines) and not lines[i].startswith("```"):
                code.append(lines[i]); i += 1
            blocks.append({"object": "block", "type": "code",
                           "code": {"language": lang, "rich_text": _rt("\n".join(code))}})
        elif re.match(r"^#{1,3} ", line):
            flush()
            level = len(line) - len(line.lstrip("#"))
            kind = f"heading_{min(level, 3)}"
            blocks.append({"object": "block", "type": kind, kind: {"rich_text": _rt(line[level:].strip())}})
        elif line.strip() == "":
            flush()
        else:
            para.append(line)
        i += 1
    flush()
    return blocks


# ---------- board ----------
def _sel_filter(prop: str, values: Iterable[str]) -> dict:
    vals = list(values)
    if len(vals) == 1:
        return {"property": prop, "select": {"equals": vals[0]}}
    return {"or": [{"property": prop, "select": {"equals": v}} for v in vals]}


class NotionBoard:
    def __init__(self, client: NotionClient, ids: NotionIds):
        self.c = client
        self.ids = ids

    # ----- tasks -----
    def next_task_id(self) -> str:
        rows = self.c.query(self.ids.tasks_ds)
        return next_id("T", (np.r_rich(r["properties"].get("ID", {})) for r in rows))

    def create_task(self, task: Task) -> Task:
        if not task.id:
            task.id = self.next_task_id()
        children = markdown_to_blocks(task.description) if task.description else None
        page = self.c.create_page(self.ids.tasks_ds, np.task_to_props(task), children)
        task.page_id = page["id"]
        return task

    def get_task(self, task_id: str) -> Task | None:
        rows = self.c.query(self.ids.tasks_ds, filter={"property": "ID", "rich_text": {"equals": task_id}})
        return np.page_to_task(rows[0]) if rows else None

    def list_tasks(self, *, status: Iterable[Status] | None = None,
                   agent: Iterable[str] | None = None) -> list[Task]:
        parts = []
        if status is not None:
            parts.append(_sel_filter("Status", [s.value for s in status]))
        if agent is not None:
            parts.append(_sel_filter("Agent", list(agent)))
        flt = None if not parts else (parts[0] if len(parts) == 1 else {"and": parts})
        rows = self.c.query(self.ids.tasks_ds, filter=flt,
                            sorts=[{"property": "Priority", "direction": "ascending"}])
        tasks = [np.page_to_task(r) for r in rows]
        tasks.sort(key=lambda t: (t.priority, t.id))
        return tasks

    def update_task(self, task: Task, fields: Iterable[str]) -> Task:
        self.c.update_page(task.page_id, np.task_to_props(task, fields))
        task.updated = utcnow()
        return task

    def append_task_report(self, task: Task, heading: str, markdown: str) -> None:
        self.c.append_blocks(task.page_id, markdown_to_blocks(f"## {heading}\n\n{markdown}"))

    # ----- questions -----
    def next_question_id(self) -> str:
        rows = self.c.query(self.ids.questions_ds)
        return next_id("Q", (np.r_rich(r["properties"].get("ID", {})) for r in rows))

    def create_question(self, q: Question) -> Question:
        if not q.id:
            q.id = self.next_question_id()
        task_page = None
        if q.task_id:
            t = self.get_task(q.task_id)
            task_page = t.page_id if t else None
        page = self.c.create_page(self.ids.questions_ds, np.question_to_props(q, task_page_id=task_page))
        q.page_id = page["id"]
        return q

    def list_questions(self, *, status: str | None = None) -> list[Question]:
        flt = _sel_filter("Status", [status]) if status else None
        qs = [np.page_to_question(r) for r in self.c.query(self.ids.questions_ds, filter=flt)]
        qs.sort(key=lambda q: q.id)
        return qs

    def update_question(self, q: Question, fields: Iterable[str]) -> Question:
        self.c.update_page(q.page_id, np.question_to_props(q, fields=fields))
        return q

    # ----- agents -----
    def _find_agent_page(self, name: str) -> dict | None:
        rows = self.c.query(self.ids.agents_ds, filter={"property": "Name", "title": {"equals": name}})
        return rows[0] if rows else None

    def upsert_agent(self, row: AgentRow) -> AgentRow:
        page = self._find_agent_page(row.name) if not row.page_id else {"id": row.page_id}
        if page:
            self.c.update_page(page["id"], np.agent_to_props(row))
            row.page_id = page["id"]
        else:
            created = self.c.create_page(self.ids.agents_ds, np.agent_to_props(row))
            row.page_id = created["id"]
        return row

    def get_agent(self, name: str) -> AgentRow | None:
        page = self._find_agent_page(name)
        return np.page_to_agent(page) if page else None

    def list_agents(self) -> list[AgentRow]:
        return [np.page_to_agent(r) for r in self.c.query(self.ids.agents_ds)]

    # ----- status page -----
    def write_status_page(self, text: str) -> None:
        self.c.update_block(self.ids.status_block, {"code": {"language": "plain text", "rich_text": _rt(text)}})

    # ----- init -----
    @staticmethod
    def init(client: NotionClient, parent_page_id: str, agent_names: list[str]) -> dict:
        ids: dict = {"parent_page_id": parent_page_id}
        tasks = client.create_database(parent_page_id, "Swarm Tasks", np.TASKS_SCHEMA(agent_names))
        ids["tasks_db"], ids["tasks_ds"] = tasks["id"], tasks["data_sources"][0]["id"]
        questions = client.create_database(parent_page_id, "Swarm Questions", np.QUESTIONS_SCHEMA(ids["tasks_ds"]))
        ids["questions_db"], ids["questions_ds"] = questions["id"], questions["data_sources"][0]["id"]
        agents = client.create_database(parent_page_id, "Swarm Agents", np.AGENTS_SCHEMA)
        ids["agents_db"], ids["agents_ds"] = agents["id"], agents["data_sources"][0]["id"]
        page = client.create_child_page(parent_page_id, "Swarm Status", [
            {"object": "block", "type": "code", "code": {"language": "plain text", "rich_text": _rt("(no status yet)")}}])
        ids["status_page"] = page["id"]
        children = client.list_children(page["id"])
        ids["status_block"] = children[0]["id"] if children else ""
        ids["views_ok"] = "true"
        try:
            ds = client.get_data_source(ids["tasks_ds"])
            props = ds["properties"]
            for name, prop in (("By Status", "Status"), ("By Agent", "Agent")):
                client.create_view(ids["tasks_db"], ids["tasks_ds"], name, "board", {
                    "type": "board",
                    "group_by": {"type": "select", "property_id": props[prop]["id"], "sort": {"type": "manual"},
                                 "hide_empty_groups": False},
                    "card_layout": "compact"})
            qds = client.get_data_source(ids["questions_ds"])
            client.create_view(ids["questions_db"], ids["questions_ds"], "Open Questions", "board", {
                "type": "board",
                "group_by": {"type": "select", "property_id": qds["properties"]["Status"]["id"],
                             "sort": {"type": "manual"}, "hide_empty_groups": False},
                "card_layout": "compact"})
        except NotionError as e:  # views API is new; fall back to manual instructions
            ids["views_ok"] = f"false: {e}"
        return ids
```

Update `swarm/board/__init__.py`:
```python
from .base import Board, claim_task  # noqa: F401
from .memory import InMemoryBoard  # noqa: F401
from .notion import NotionBoard, NotionClient, NotionError  # noqa: F401
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_notion_client.py -v`
Expected: 9 passed

- [ ] **Step 5: Commit**

```bash
git add swarm/board/notion.py swarm/board/__init__.py tests/test_notion_client.py
git commit -m "feat: notion client with retries and NotionBoard"
```

---

### Task 6: Workspace (git worktrees, push, PRs)

**Files:**
- Create: `swarm/workspace.py`
- Test: `tests/test_workspace.py`

**Interfaces:**
- Produces: `CmdResult(code, out, err)` with `.ok` and `.tail(n)`; `run_cmd(args, cwd, timeout, input=None, env=None) -> CmdResult`; `Workspace(repo_root, worktree_root, main_branch="main", remote="origin", gh=None)` with `fetch()`, `worktree_path(task_id)`, `provision(task_id, *, reuse_branch=False) -> Path`, `dispose(path)`, `commit_all(path, message) -> bool`, `push(path, branch, *, force_with_lease=False) -> CmdResult`, `changed_files(path) -> list[str]`, `diff_stat(path) -> str`, `rebase_onto_main(path) -> tuple[bool, list[str]]`, `run_script(path, script_rel, timeout) -> CmdResult | None`, `pr_create_or_update(branch, title, body) -> str`, `pr_merge(branch) -> CmdResult`, `main_worktree() -> Path`.
- `gh` is an injectable callable `(args: list[str], cwd: Path) -> CmdResult`; default runs the real `gh`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_workspace.py
import subprocess
from pathlib import Path
from swarm.workspace import Workspace, CmdResult, run_cmd


def _git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True).stdout


def test_run_cmd_captures():
    r = run_cmd(["sh", "-c", "echo hi; echo err 1>&2; exit 3"], cwd=Path("."))
    assert r.code == 3 and r.out.strip() == "hi" and r.err.strip() == "err" and not r.ok
    assert "err" in r.tail(100)


def test_provision_commit_push_and_reuse(git_repo, tmp_path):
    ws = Workspace(git_repo, tmp_path / "wt")
    path = ws.provision("T-001")
    assert path.exists() and (path / ".swarm-run").is_dir()
    assert _git(path, "rev-parse", "--abbrev-ref", "HEAD").strip() == "task/T-001"
    (path / "a.txt").write_text("hello\n")
    assert ws.commit_all(path, "T-001: add a") is True
    assert ws.commit_all(path, "nothing") is False
    assert ws.push(path, "task/T-001").ok
    assert ws.changed_files(path) == ["a.txt"]
    assert "a.txt" in ws.diff_stat(path)
    ws.dispose(path)
    assert not path.exists()
    # reuse picks up the pushed commit
    path2 = ws.provision("T-001", reuse_branch=True)
    assert (path2 / "a.txt").read_text() == "hello\n"
    ws.dispose(path2)
    # fresh provision resets to main
    path3 = ws.provision("T-001", reuse_branch=False)
    assert not (path3 / "a.txt").exists()
    ws.dispose(path3)


def test_provision_replaces_stale_worktree(git_repo, tmp_path):
    ws = Workspace(git_repo, tmp_path / "wt")
    p1 = ws.provision("T-002")
    (p1 / "junk.txt").write_text("x")
    p2 = ws.provision("T-002")  # not disposed first
    assert p2 == p1 and not (p2 / "junk.txt").exists()
    ws.dispose(p2)


def test_rebase_conflict_detection(git_repo, tmp_path):
    ws = Workspace(git_repo, tmp_path / "wt")
    path = ws.provision("T-003")
    (path / "README.md").write_text("branch version\n")
    ws.commit_all(path, "branch change")
    ws.push(path, "task/T-003")
    # conflicting change on main
    (git_repo / "README.md").write_text("main version\n")
    _git(git_repo, "commit", "-qam", "main change")
    _git(git_repo, "push", "-q", "origin", "main")
    ok, conflicts = ws.rebase_onto_main(path)
    assert ok is False and conflicts == ["README.md"]
    assert _git(path, "status", "--porcelain").strip() == ""  # aborted cleanly
    ws.dispose(path)


def test_rebase_clean(git_repo, tmp_path):
    ws = Workspace(git_repo, tmp_path / "wt")
    path = ws.provision("T-004")
    (path / "new.txt").write_text("n\n")
    ws.commit_all(path, "new")
    (git_repo / "other.txt").write_text("o\n")
    _git(git_repo, "add", "other.txt")
    _git(git_repo, "commit", "-qm", "main other")
    _git(git_repo, "push", "-q", "origin", "main")
    ok, conflicts = ws.rebase_onto_main(path)
    assert ok and conflicts == [] and (path / "other.txt").exists()
    ws.dispose(path)


def test_run_script_missing_and_present(git_repo, tmp_path):
    ws = Workspace(git_repo, tmp_path / "wt")
    path = ws.provision("T-005")
    assert ws.run_script(path, "scripts/nope.sh", 10) is None
    r = ws.run_script(path, "scripts/verify_fast.sh", 10)
    assert r is not None and r.ok
    ws.dispose(path)


def test_pr_create_then_update_and_merge(git_repo, tmp_path):
    calls = []

    def fake_gh(args, cwd):
        calls.append(args)
        if args[:3] == ["pr", "view", "task/T-006"]:
            return CmdResult(1, "", "no pull requests found") if len(calls) == 1 else CmdResult(0, '{"url":"https://gh/pr/9"}', "")
        if args[:2] == ["pr", "create"]:
            return CmdResult(0, "https://gh/pr/9\n", "")
        if args[:2] == ["pr", "edit"]:
            return CmdResult(0, "", "")
        if args[:2] == ["pr", "merge"]:
            return CmdResult(0, "merged", "")
        return CmdResult(1, "", "unexpected")

    ws = Workspace(git_repo, tmp_path / "wt", gh=fake_gh)
    assert ws.pr_create_or_update("task/T-006", "T-006 · title", "body") == "https://gh/pr/9"
    assert ws.pr_create_or_update("task/T-006", "T-006 · title", "body2") == "https://gh/pr/9"
    assert any(a[:2] == ["pr", "edit"] for a in calls)
    assert ws.pr_merge("task/T-006").ok


def test_main_worktree(git_repo, tmp_path):
    ws = Workspace(git_repo, tmp_path / "wt")
    m = ws.main_worktree()
    assert (m / "README.md").exists()
    assert _git(m, "rev-parse", "--abbrev-ref", "HEAD").strip() == "swarm-main"
    assert ws.main_worktree() == m
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_workspace.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement workspace.py**

```python
# swarm/workspace.py
from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


@dataclass
class CmdResult:
    code: int
    out: str
    err: str

    @property
    def ok(self) -> bool:
        return self.code == 0

    def tail(self, n: int = 2000) -> str:
        text = self.out + ("\n" + self.err if self.err else "")
        return text[-n:]


def run_cmd(args: list[str], cwd: Path, timeout: int = 600, input: bytes | None = None,
            env: dict | None = None) -> CmdResult:
    try:
        p = subprocess.run(args, cwd=str(cwd), input=input, capture_output=True, timeout=timeout,
                           env={**os.environ, **(env or {})})
    except subprocess.TimeoutExpired as e:
        return CmdResult(-1, (e.stdout or b"").decode(errors="replace"),
                         (e.stderr or b"").decode(errors="replace") + f"\n[timeout after {timeout}s]")
    except FileNotFoundError as e:
        return CmdResult(-2, "", f"command not found: {args[0]} ({e})")
    return CmdResult(p.returncode, p.stdout.decode(errors="replace"), p.stderr.decode(errors="replace"))


def _default_gh(args: list[str], cwd: Path) -> CmdResult:
    return run_cmd(["gh", *args], cwd=cwd, timeout=120)


class Workspace:
    def __init__(self, repo_root: Path, worktree_root: Path, main_branch: str = "main",
                 remote: str = "origin", gh: Callable[[list[str], Path], CmdResult] | None = None):
        self.repo_root = Path(repo_root).resolve()
        self.worktree_root = Path(worktree_root).resolve()
        self.main_branch = main_branch
        self.remote = remote
        self.gh = gh or _default_gh

    # ----- git plumbing -----
    def git(self, cwd: Path, *args: str, check: bool = True, timeout: int = 300) -> CmdResult:
        r = run_cmd(["git", "-C", str(cwd), *args], cwd=cwd, timeout=timeout)
        if check and not r.ok:
            raise RuntimeError(f"git {' '.join(args)} failed: {r.err.strip() or r.out.strip()}")
        return r

    def fetch(self) -> None:
        self.git(self.repo_root, "fetch", "-q", self.remote)

    def worktree_path(self, task_id: str) -> Path:
        return self.worktree_root / task_id

    def _main_ref(self) -> str:
        return f"{self.remote}/{self.main_branch}"

    def provision(self, task_id: str, *, reuse_branch: bool = False) -> Path:
        self.fetch()
        path = self.worktree_path(task_id)
        branch = f"task/{task_id}"
        if path.exists():
            self.dispose(path)
        self.git(self.repo_root, "worktree", "prune")
        self.worktree_root.mkdir(parents=True, exist_ok=True)
        remote_branch = f"{self.remote}/{branch}"
        has_remote = self.git(self.repo_root, "rev-parse", "--verify", "--quiet", remote_branch, check=False).ok
        start = remote_branch if (reuse_branch and has_remote) else self._main_ref()
        self.git(self.repo_root, "worktree", "add", "-q", "-B", branch, str(path), start)
        (path / ".swarm-run").mkdir(exist_ok=True)
        return path

    def dispose(self, path: Path) -> None:
        r = self.git(self.repo_root, "worktree", "remove", "--force", str(path), check=False)
        if not r.ok and path.exists():
            shutil.rmtree(path, ignore_errors=True)
            self.git(self.repo_root, "worktree", "prune", check=False)

    def commit_all(self, path: Path, message: str) -> bool:
        self.git(path, "add", "-A")
        if self.git(path, "diff", "--cached", "--quiet", check=False).ok:
            return False
        self.git(path, "-c", "user.email=swarm@local", "-c", "user.name=swarm", "commit", "-q", "-m", message)
        return True

    def push(self, path: Path, branch: str, *, force_with_lease: bool = False) -> CmdResult:
        args = ["push", "-q", "-u", self.remote, f"HEAD:refs/heads/{branch}"]
        if force_with_lease:
            args.insert(1, "--force-with-lease")
        return self.git(path, *args, check=False, timeout=180)

    def changed_files(self, path: Path) -> list[str]:
        committed = self.git(path, "diff", "--name-only", f"{self._main_ref()}...HEAD", check=False).out.split()
        status = self.git(path, "status", "--porcelain", check=False).out.splitlines()
        uncommitted = [line[3:].strip() for line in status if line.strip()]
        return sorted(set(committed) | set(uncommitted))

    def diff_stat(self, path: Path) -> str:
        return self.git(path, "diff", "--stat", self._main_ref(), check=False).out

    def rebase_onto_main(self, path: Path) -> tuple[bool, list[str]]:
        self.fetch()
        r = self.git(path, "-c", "user.email=swarm@local", "-c", "user.name=swarm",
                     "rebase", self._main_ref(), check=False)
        if r.ok:
            return True, []
        conflicts = self.git(path, "diff", "--name-only", "--diff-filter=U", check=False).out.split()
        self.git(path, "rebase", "--abort", check=False)
        return False, sorted(conflicts)

    def run_script(self, path: Path, script_rel: str | None, timeout: int) -> CmdResult | None:
        if not script_rel:
            return None
        script = path / script_rel
        if not script.exists():
            return None
        return run_cmd(["bash", str(script)], cwd=path, timeout=timeout)

    # ----- GitHub -----
    def pr_create_or_update(self, branch: str, title: str, body: str) -> str:
        view = self.gh(["pr", "view", branch, "--json", "url"], self.repo_root)
        if view.ok:
            try:
                url = json.loads(view.out)["url"]
            except (ValueError, KeyError):
                url = ""
            self.gh(["pr", "edit", branch, "--body", body], self.repo_root)
            return url
        created = self.gh(["pr", "create", "--head", branch, "--base", self.main_branch,
                           "--title", title, "--body", body], self.repo_root)
        if not created.ok:
            raise RuntimeError(f"gh pr create failed: {created.err.strip() or created.out.strip()}")
        return created.out.strip().splitlines()[-1] if created.out.strip() else ""

    def pr_merge(self, branch: str) -> CmdResult:
        return self.gh(["pr", "merge", branch, "--squash", "--delete-branch"], self.repo_root)

    # ----- main worktree for serve -----
    def main_worktree(self) -> Path:
        path = self.worktree_root / "_main"
        if path.exists() and (path / ".git").exists():
            self.git(path, "fetch", "-q", self.remote, check=False)
            self.git(path, "reset", "-q", "--hard", self._main_ref(), check=False)
            return path
        self.fetch()
        self.git(self.repo_root, "worktree", "prune")
        self.worktree_root.mkdir(parents=True, exist_ok=True)
        self.git(self.repo_root, "worktree", "add", "-q", "-B", "swarm-main", str(path), self._main_ref())
        return path
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_workspace.py -v`
Expected: 8 passed

- [ ] **Step 5: Commit**

```bash
git add swarm/workspace.py tests/test_workspace.py
git commit -m "feat: git worktree workspace with push, rebase, and gh PR helpers"
```

---

### Task 7: Provider adapters

**Files:**
- Create: `swarm/adapters/__init__.py`, `swarm/adapters/base.py`, `swarm/adapters/claude.py`, `swarm/adapters/codex.py`, `swarm/adapters/antigravity.py`, `swarm/adapters/gemini.py`, `swarm/adapters/grok.py`, `swarm/adapters/generic.py`
- Test: `tests/test_adapters.py`

**Interfaces:**
- Consumes: `AgentConfig`, `RunResult`, `Usage`.
- Produces: `RunSpec(prompt_file, model, effort, max_turns, budget_usd, timeout_s, cwd, schema=None, read_only=False, sandbox=None, extra_args=[])`; `Adapter` base with `build_command(spec) -> (argv, stdin_bytes|None)`, `parse_output(code, out, err) -> RunResult`, `run(spec, runner=subprocess.run) -> RunResult`; `RATE_LIMIT_RE`; `get_adapter(agent_cfg: AgentConfig) -> Adapter`; `parse_reset_at(text) -> datetime | None`.
- Rules text is inlined in the prompt by `prompt.py`; adapters never inject system prompts.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_adapters.py
import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

from swarm.adapters import get_adapter
from swarm.adapters.base import RunSpec, Adapter, RATE_LIMIT_RE, parse_reset_at
from swarm.adapters.claude import ClaudeAdapter
from swarm.adapters.codex import CodexAdapter
from swarm.adapters.antigravity import AntigravityAdapter
from swarm.adapters.gemini import GeminiAdapter
from swarm.adapters.grok import GrokAdapter
from swarm.adapters.generic import GenericAdapter
from swarm.config import AgentConfig


def spec(tmp_path, **kw):
    pf = tmp_path / "prompt.md"
    pf.write_text("do the thing")
    base = dict(prompt_file=pf, model="m", effort="high", max_turns=30, budget_usd=3.0, timeout_s=60,
                cwd=tmp_path, schema={"type": "object"})
    base.update(kw)
    (tmp_path / ".swarm-run").mkdir(exist_ok=True)
    return RunSpec(**base)


def test_claude_command_and_parse(tmp_path):
    a = ClaudeAdapter()
    argv, stdin = a.build_command(spec(tmp_path))
    assert argv[:3] == ["claude", "-p", "--output-format"] and "json" in argv
    assert "--permission-mode" in argv and argv[argv.index("--permission-mode") + 1] == "bypassPermissions"
    assert argv[argv.index("--model") + 1] == "m" and argv[argv.index("--effort") + 1] == "high"
    assert argv[argv.index("--max-turns") + 1] == "30" and "--json-schema" in argv
    assert stdin == b"do the thing"
    ro, _ = a.build_command(spec(tmp_path, read_only=True))
    assert ro[ro.index("--permission-mode") + 1] == "dontAsk" and "--allowedTools" in ro
    out = json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "done",
                      "structured_output": {"status": "done"}, "total_cost_usd": 0.12,
                      "usage": {"input_tokens": 100, "output_tokens": 20}, "session_id": "s1"})
    r = a.parse_output(0, out, "")
    assert r.ok and r.structured_output == {"status": "done"} and r.usage.cost_usd == 0.12
    assert r.usage.input_tokens == 100 and r.session_id == "s1"
    bad = json.dumps({"type": "result", "subtype": "error_max_turns", "is_error": True, "result": "hit max turns"})
    r2 = a.parse_output(1, bad, "")
    assert not r2.ok and "max_turns" in r2.error
    rl = json.dumps({"type": "result", "subtype": "error_during_execution", "is_error": True,
                     "result": "You've hit your session limit · resets 3pm"})
    r3 = a.parse_output(1, rl, "")
    assert r3.rate_limited


def test_codex_command_and_parse(tmp_path):
    a = CodexAdapter(AgentConfig(name="c", provider="codex", host="h", sandbox="workspace-write"))
    argv, stdin = a.build_command(spec(tmp_path))
    assert argv[:3] == ["codex", "exec", "--json"] and argv[-1] == "-" and stdin == b"do the thing"
    assert argv[argv.index("--sandbox") + 1] == "workspace-write"
    assert argv[argv.index("-c") + 1] == 'model_reasoning_effort="high"'
    assert (tmp_path / ".swarm-run" / "schema.json").exists() and "--output-schema" in argv
    ro, _ = a.build_command(spec(tmp_path, read_only=True))
    assert ro[ro.index("--sandbox") + 1] == "read-only"
    lines = [
        {"type": "thread.started", "thread_id": "th1"},
        {"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps({"status": "done", "summary": "ok"})}},
        {"type": "turn.completed", "usage": {"input_tokens": 50, "cached_input_tokens": 10, "output_tokens": 7}},
    ]
    r = a.parse_output(0, "\n".join(json.dumps(l) for l in lines), "")
    assert r.ok and r.structured_output["summary"] == "ok" and r.session_id == "th1"
    assert r.usage.input_tokens == 50 and r.usage.output_tokens == 7 and r.usage.cost_usd is None
    fail = json.dumps({"type": "error", "message": "Rate limit reached for gpt-6-sol"})
    r2 = a.parse_output(1, fail, "")
    assert not r2.ok and r2.rate_limited


def test_antigravity_gemini_grok_generic_commands(tmp_path):
    ag, _ = AntigravityAdapter().build_command(spec(tmp_path))
    assert ag[0] == "agy" and "--model=m" in ag and "--approval-mode" in ag
    r = AntigravityAdapter().parse_output(0, json.dumps({"response": "hi", "stats": {}}), "")
    assert r.ok and r.structured_output is None
    ge, _ = GeminiAdapter().build_command(spec(tmp_path))
    assert ge[0] == "gemini" and ge[ge.index("-m") + 1] == "m"
    gr, _ = GrokAdapter().build_command(spec(tmp_path))
    assert gr[0] == "grok" and gr[gr.index("--prompt-file") + 1].endswith("prompt.md") and "--cwd" in gr
    rg = GrokAdapter().parse_output(0, json.dumps({"text": "t", "usage": {"input_tokens": 3, "output_tokens": 4}, "total_cost_usd": 0.01}), "")
    assert rg.ok and rg.usage.cost_usd == 0.01 and rg.usage.output_tokens == 4
    gen = GenericAdapter(AgentConfig(name="g", provider="generic", host="h",
                                     command_template="bash run.sh {prompt_file} {model} {cwd}"))
    argv, stdin = gen.build_command(spec(tmp_path))
    assert argv[:2] == ["bash", "run.sh"] and argv[3] == "m" and stdin is None


def test_get_adapter_dispatch():
    for provider, cls in [("claude", ClaudeAdapter), ("codex", CodexAdapter), ("antigravity", AntigravityAdapter),
                          ("gemini", GeminiAdapter), ("grok", GrokAdapter)]:
        assert isinstance(get_adapter(AgentConfig(name="x", provider=provider, host="h")), cls)
    assert isinstance(get_adapter(AgentConfig(name="x", provider="generic", host="h", command_template="x {prompt_file}")), GenericAdapter)


def test_run_with_fake_cli_and_timeout(tmp_path):
    script = tmp_path / "fake.sh"
    script.write_text('#!/bin/bash\ncat > /dev/null\necho \'{"type":"result","subtype":"success","is_error":false,"result":"ok","structured_output":{"status":"done"},"usage":{"input_tokens":1,"output_tokens":1},"total_cost_usd":0.0}\'\n')
    script.chmod(script.stat().st_mode | stat.S_IEXEC)

    class FakeClaude(ClaudeAdapter):
        def build_command(self, spec):
            argv, stdin = super().build_command(spec)
            return ["bash", str(script)], stdin

    r = FakeClaude().run(spec(tmp_path))
    assert r.ok and r.structured_output == {"status": "done"}

    slow = tmp_path / "slow.sh"
    slow.write_text("#!/bin/bash\nsleep 5\n")
    gen = GenericAdapter(AgentConfig(name="g", provider="generic", host="h", command_template=f"bash {slow} {{prompt_file}}"))
    r2 = gen.run(spec(tmp_path, timeout_s=1))
    assert r2.timed_out and not r2.ok


def test_missing_cli_is_reported(tmp_path):
    gen = GenericAdapter(AgentConfig(name="g", provider="generic", host="h", command_template="definitely-not-a-cli-xyz {prompt_file}"))
    r = gen.run(spec(tmp_path))
    assert not r.ok and "not found" in r.error


def test_rate_limit_regex_and_reset():
    assert RATE_LIMIT_RE.search("Error: 429 Too Many Requests")
    assert RATE_LIMIT_RE.search("You've hit your weekly limit")
    assert not RATE_LIMIT_RE.search("all good")
    assert parse_reset_at("resets at 2026-10-10T18:00:00Z").isoformat() == "2026-10-10T18:00:00+00:00"
    assert parse_reset_at("nothing here") is None
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_adapters.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement adapters**

`swarm/adapters/base.py`:
```python
from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from ..config import AgentConfig
from ..models import RunResult, Usage

RATE_LIMIT_RE = re.compile(
    r"rate.?limit|too many requests|\b429\b|usage limit|hit your .{0,40}limit|quota exceeded|"
    r"resource.?exhausted|overloaded|capacity", re.I)
_RESET_RE = re.compile(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2}))")


def parse_reset_at(text: str) -> datetime | None:
    m = _RESET_RE.search(text or "")
    if not m:
        return None
    dt = datetime.fromisoformat(m.group(1).replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def last_json_object(text: str) -> dict | None:
    text = (text or "").strip()
    if not text:
        return None
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else None
    except ValueError:
        pass
    for line in reversed(text.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                data = json.loads(line)
                if isinstance(data, dict):
                    return data
            except ValueError:
                continue
    return None


@dataclass
class RunSpec:
    prompt_file: Path
    model: str
    effort: str | None
    max_turns: int
    budget_usd: float | None
    timeout_s: int
    cwd: Path
    schema: dict | None = None
    read_only: bool = False
    sandbox: str | None = None
    extra_args: list[str] = field(default_factory=list)


class Adapter:
    name = "base"

    def __init__(self, agent_cfg: AgentConfig | None = None):
        self.cfg = agent_cfg

    def build_command(self, spec: RunSpec) -> tuple[list[str], bytes | None]:
        raise NotImplementedError

    def parse_output(self, code: int, out: str, err: str) -> RunResult:
        return RunResult(ok=code == 0, exit_code=code, stdout=out, stderr=err,
                         error="" if code == 0 else f"exit {code}")

    def run(self, spec: RunSpec, runner=subprocess.run) -> RunResult:
        argv, stdin = self.build_command(spec)
        (spec.cwd / ".swarm-run").mkdir(exist_ok=True)
        try:
            proc = runner(argv, cwd=str(spec.cwd), input=stdin, capture_output=True, timeout=spec.timeout_s)
        except subprocess.TimeoutExpired as e:
            return RunResult(ok=False, exit_code=-1, stdout=(e.stdout or b"").decode(errors="replace"),
                             stderr=(e.stderr or b"").decode(errors="replace"), timed_out=True,
                             error=f"timeout after {spec.timeout_s}s")
        except FileNotFoundError as e:
            return RunResult(ok=False, exit_code=-2, stdout="", stderr=str(e), error=f"cli not found: {argv[0]}")
        out = proc.stdout.decode(errors="replace")
        err = proc.stderr.decode(errors="replace")
        result = self.parse_output(proc.returncode, out, err)
        if not result.ok and not result.rate_limited and RATE_LIMIT_RE.search(err[-4000:] + "\n" + result.error):
            result.rate_limited = True
        if result.rate_limited and result.reset_at is None:
            result.reset_at = parse_reset_at(err + "\n" + result.error + "\n" + out[-2000:])
        return result
```

`swarm/adapters/claude.py`:
```python
from __future__ import annotations

import json

from ..models import RunResult, Usage
from .base import Adapter, RATE_LIMIT_RE, RunSpec, last_json_object, parse_reset_at

READ_ONLY_TOOLS = "Read,Grep,Glob,Bash(git diff:*),Bash(git log:*),Bash(git show:*),Bash(ls:*),Bash(cat:*)"


class ClaudeAdapter(Adapter):
    name = "claude"

    def build_command(self, spec: RunSpec) -> tuple[list[str], bytes | None]:
        argv = ["claude", "-p", "--output-format", "json", "--model", spec.model,
                "--max-turns", str(spec.max_turns)]
        if spec.read_only:
            argv += ["--permission-mode", "dontAsk", "--allowedTools", READ_ONLY_TOOLS]
        else:
            argv += ["--permission-mode", "bypassPermissions"]
        if spec.effort:
            argv += ["--effort", spec.effort]
        if spec.budget_usd:
            argv += ["--max-budget-usd", str(spec.budget_usd)]
        if spec.schema:
            argv += ["--json-schema", json.dumps(spec.schema)]
        argv += list(spec.extra_args)
        return argv, spec.prompt_file.read_bytes()

    def parse_output(self, code: int, out: str, err: str) -> RunResult:
        data = last_json_object(out)
        if not data:
            return RunResult(ok=False, exit_code=code, stdout=out, stderr=err,
                             error=f"no result json (exit {code})")
        subtype = data.get("subtype", "")
        is_error = bool(data.get("is_error")) or subtype != "success"
        u = data.get("usage") or {}
        usage = Usage(input_tokens=int(u.get("input_tokens", 0) or 0),
                      output_tokens=int(u.get("output_tokens", 0) or 0),
                      cost_usd=data.get("total_cost_usd"))
        text = str(data.get("result") or "")
        rate_limited = is_error and bool(RATE_LIMIT_RE.search(text + "\n" + err))
        return RunResult(ok=not is_error, exit_code=code, stdout=out, stderr=err,
                         structured_output=data.get("structured_output"), usage=usage,
                         session_id=data.get("session_id"), rate_limited=rate_limited,
                         reset_at=parse_reset_at(text) if rate_limited else None,
                         error="" if not is_error else f"{subtype}: {text[:300]}")
```

`swarm/adapters/codex.py`:
```python
from __future__ import annotations

import json

from ..models import RunResult, Usage
from .base import Adapter, RATE_LIMIT_RE, RunSpec, parse_reset_at


class CodexAdapter(Adapter):
    name = "codex"

    def build_command(self, spec: RunSpec) -> tuple[list[str], bytes | None]:
        argv = ["codex", "exec", "--json", "-C", str(spec.cwd), "-m", spec.model]
        if spec.effort:
            argv += ["-c", f'model_reasoning_effort="{spec.effort}"']
        sandbox = "read-only" if spec.read_only else (spec.sandbox or (self.cfg.sandbox if self.cfg else None)
                                                      or "workspace-write")
        argv += ["--sandbox", sandbox]
        if spec.schema:
            schema_path = spec.cwd / ".swarm-run" / "schema.json"
            schema_path.parent.mkdir(exist_ok=True)
            schema_path.write_text(json.dumps(spec.schema))
            argv += ["--output-schema", str(schema_path)]
        argv += list(spec.extra_args) + ["-"]
        return argv, spec.prompt_file.read_bytes()

    def parse_output(self, code: int, out: str, err: str) -> RunResult:
        session, last_text, failed, error = None, None, False, ""
        usage = Usage()
        for line in out.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            t = ev.get("type", "")
            if t == "thread.started":
                session = ev.get("thread_id")
            elif t == "item.completed" and (ev.get("item") or {}).get("type") == "agent_message":
                last_text = ev["item"].get("text")
            elif t == "turn.completed":
                u = ev.get("usage") or {}
                usage.input_tokens += int(u.get("input_tokens", 0) or 0)
                usage.output_tokens += int(u.get("output_tokens", 0) or 0)
            elif t in ("turn.failed", "error"):
                failed = True
                error = str((ev.get("error") or {}).get("message") or ev.get("message") or t)
        structured = None
        if last_text and last_text.strip().startswith("{"):
            try:
                structured = json.loads(last_text)
            except ValueError:
                structured = None
        ok = code == 0 and not failed
        if not ok and not error:
            error = f"exit {code}"
        rate_limited = (not ok) and bool(RATE_LIMIT_RE.search(error + "\n" + err))
        return RunResult(ok=ok, exit_code=code, stdout=out, stderr=err, structured_output=structured,
                         usage=usage, session_id=session, rate_limited=rate_limited,
                         reset_at=parse_reset_at(error + err) if rate_limited else None, error=error)
```

`swarm/adapters/antigravity.py`:
```python
from __future__ import annotations

from ..models import RunResult, Usage
from .base import Adapter, RunSpec, last_json_object


class AntigravityAdapter(Adapter):
    """Google Antigravity CLI (`agy`). Flags are third-party sourced; verify with `swarm doctor`."""
    name = "antigravity"

    def build_command(self, spec: RunSpec) -> tuple[list[str], bytes | None]:
        argv = ["agy", "-p", spec.prompt_file.read_text(), f"--model={spec.model}", "--output-format", "json"]
        if not spec.read_only:
            argv += ["--approval-mode", "yolo"]
        argv += list(spec.extra_args)
        return argv, None

    def parse_output(self, code: int, out: str, err: str) -> RunResult:
        data = last_json_object(out) or {}
        error = str(data.get("error") or ("" if code == 0 else f"exit {code}"))
        return RunResult(ok=code == 0 and not data.get("error"), exit_code=code, stdout=out, stderr=err,
                         structured_output=None, usage=Usage(), error=error)
```

`swarm/adapters/gemini.py`:
```python
from __future__ import annotations

from ..models import RunResult, Usage
from .base import Adapter, RunSpec, last_json_object


class GeminiAdapter(Adapter):
    """Legacy Gemini CLI with an API key."""
    name = "gemini"

    def build_command(self, spec: RunSpec) -> tuple[list[str], bytes | None]:
        argv = ["gemini", "-p", spec.prompt_file.read_text(), "--output-format", "json", "-m", spec.model]
        if not spec.read_only:
            argv += ["--approval-mode", "yolo"]
        argv += list(spec.extra_args)
        return argv, None

    def parse_output(self, code: int, out: str, err: str) -> RunResult:
        data = last_json_object(out) or {}
        error = str(data.get("error") or ("" if code == 0 else f"exit {code}"))
        return RunResult(ok=code == 0 and not data.get("error"), exit_code=code, stdout=out, stderr=err,
                         usage=Usage(), error=error)
```

`swarm/adapters/grok.py`:
```python
from __future__ import annotations

from ..models import RunResult, Usage
from .base import Adapter, RunSpec, last_json_object


class GrokAdapter(Adapter):
    name = "grok"

    def build_command(self, spec: RunSpec) -> tuple[list[str], bytes | None]:
        argv = ["grok", "-p", "--prompt-file", str(spec.prompt_file), "--output-format", "json",
                "--max-turns", str(spec.max_turns), "--cwd", str(spec.cwd), "--model", spec.model]
        if not spec.read_only:
            argv += ["--always-approve"]
        argv += list(spec.extra_args)
        return argv, None

    def parse_output(self, code: int, out: str, err: str) -> RunResult:
        data = last_json_object(out) or {}
        u = data.get("usage") or {}
        usage = Usage(input_tokens=int(u.get("input_tokens", 0) or 0), output_tokens=int(u.get("output_tokens", 0) or 0),
                      cost_usd=data.get("total_cost_usd"))
        return RunResult(ok=code == 0, exit_code=code, stdout=out, stderr=err, usage=usage,
                         session_id=data.get("sessionId"), error="" if code == 0 else f"exit {code}")
```

`swarm/adapters/generic.py`:
```python
from __future__ import annotations

import shlex

from .base import Adapter, RunSpec


class GenericAdapter(Adapter):
    """Any CLI: command_template with {prompt_file} {model} {cwd} placeholders. Report via .swarm-run/report.json."""
    name = "generic"

    def build_command(self, spec: RunSpec) -> tuple[list[str], bytes | None]:
        template = (self.cfg.command_template if self.cfg else None) or "cat {prompt_file}"
        cmd = template.format(prompt_file=str(spec.prompt_file), model=spec.model, cwd=str(spec.cwd))
        return shlex.split(cmd) + list(spec.extra_args), None
```

`swarm/adapters/__init__.py`:
```python
from ..config import AgentConfig
from .antigravity import AntigravityAdapter
from .base import Adapter, RunSpec, RATE_LIMIT_RE  # noqa: F401
from .claude import ClaudeAdapter
from .codex import CodexAdapter
from .gemini import GeminiAdapter
from .generic import GenericAdapter
from .grok import GrokAdapter

_REGISTRY = {"claude": ClaudeAdapter, "codex": CodexAdapter, "antigravity": AntigravityAdapter,
             "gemini": GeminiAdapter, "grok": GrokAdapter, "generic": GenericAdapter}


def get_adapter(agent_cfg: AgentConfig) -> Adapter:
    cls = _REGISTRY.get(agent_cfg.provider)
    if cls is None:
        raise ValueError(f"unknown provider {agent_cfg.provider}")
    return cls(agent_cfg)
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_adapters.py -v`
Expected: 7 passed

- [ ] **Step 5: Commit**

```bash
git add swarm/adapters tests/test_adapters.py
git commit -m "feat: provider adapters for claude, codex, antigravity, gemini, grok, generic"
```

---

### Task 8: Report schema, parsing, and markdown

**Files:**
- Create: `swarm/report.py`
- Test: `tests/test_report.py`

**Interfaces:**
- Produces: `REPORT_SCHEMA: dict`; `REVIEW_SCHEMA: dict`; `parse_report(structured, worktree, *, changed_files) -> Report`; `report_to_markdown(report, *, attempt, verify_ok, verify_tail, pr_url, flags) -> str`; `decisions_markdown(task, report) -> str`; `debts_markdown(task, report) -> str`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_report.py
import json
from swarm.report import REPORT_SCHEMA, REVIEW_SCHEMA, parse_report, report_to_markdown, decisions_markdown, debts_markdown
from swarm.models import Task


def test_schema_shape():
    assert REPORT_SCHEMA["type"] == "object" and "status" in REPORT_SCHEMA["required"]
    assert REPORT_SCHEMA["properties"]["status"]["enum"] == ["done", "blocked", "failed"]
    assert REVIEW_SCHEMA["properties"]["verdict"]["enum"] == ["approve", "request_changes", "escalate"]


def test_parse_structured(tmp_path):
    r = parse_report({"status": "done", "summary": "did it", "files_changed": ["a.py"],
                      "tests": {"command": "pytest", "passed": True}, "debts": [{"kind": "todo", "location": "a.py:1", "reason": "r", "fix": "f"}],
                      "decisions": [{"decision": "x", "why": "y", "impact": "high"}],
                      "question": {"kind": "fyi", "text": "ok?", "options": [], "proceeding_with": "x"}},
                     tmp_path, changed_files=["a.py"])
    assert r.status == "done" and r.debts[0]["kind"] == "todo" and r.question["kind"] == "fyi" and not r.synthesized


def test_parse_from_file(tmp_path):
    (tmp_path / ".swarm-run").mkdir()
    (tmp_path / ".swarm-run" / "report.json").write_text(json.dumps({"status": "blocked", "summary": "need input",
                                                                    "question": {"kind": "blocking", "text": "which db?"}}))
    r = parse_report(None, tmp_path, changed_files=[])
    assert r.status == "blocked" and r.question["text"] == "which db?"


def test_parse_missing_synthesizes(tmp_path):
    r = parse_report(None, tmp_path, changed_files=["x.py", "y.py"])
    assert r.synthesized and r.status == "done" and r.files_changed == ["x.py", "y.py"]
    r2 = parse_report({"garbage": True}, tmp_path, changed_files=[])
    assert r2.synthesized and r2.status == "failed"


def test_parse_bad_status_and_empty_question(tmp_path):
    r = parse_report({"status": "weird", "summary": "s", "question": {"text": ""}}, tmp_path, changed_files=["a"])
    assert r.status == "done" and r.question is None


def test_markdown_outputs(tmp_path):
    r = parse_report({"status": "done", "summary": "did it", "decisions": [{"decision": "use tap", "why": "simpler", "impact": "high"}],
                      "debts": [{"kind": "mock", "location": "b.py:3", "reason": "no api", "fix": "wire it"}]},
                     tmp_path, changed_files=["b.py"])
    md = report_to_markdown(r, attempt=2, verify_ok=False, verify_tail="FAIL 1/3", pr_url="https://gh/1", flags=["out_of_scope"])
    assert "attempt 2" in md.lower() and "FAIL 1/3" in md and "https://gh/1" in md and "out_of_scope" in md and "use tap" in md
    t = Task(id="T-009", title="Cards")
    assert "T-009" in decisions_markdown(t, r) and "use tap" in decisions_markdown(t, r)
    assert "b.py:3" in debts_markdown(t, r)
    assert decisions_markdown(t, parse_report({"status": "done"}, tmp_path, changed_files=["a"])) == ""
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_report.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement report.py**

```python
# swarm/report.py
from __future__ import annotations

import json
from pathlib import Path

from .models import Report, Task, utcnow

REPORT_SCHEMA = {
    "type": "object",
    "required": ["status", "summary"],
    "additionalProperties": False,
    "properties": {
        "status": {"type": "string", "enum": ["done", "blocked", "failed"]},
        "summary": {"type": "string", "description": "2-4 sentences: what changed and why"},
        "files_changed": {"type": "array", "items": {"type": "string"}},
        "tests": {"type": "object", "properties": {
            "command": {"type": "string"}, "passed": {"type": "boolean"}, "output_tail": {"type": "string"}}},
        "debts": {"type": "array", "items": {"type": "object", "properties": {
            "kind": {"type": "string", "enum": ["mock", "hardcode", "todo", "skipped_test", "assumption", "fallback"]},
            "location": {"type": "string"}, "reason": {"type": "string"}, "fix": {"type": "string"}},
            "required": ["kind", "location", "reason"]}},
        "decisions": {"type": "array", "items": {"type": "object", "properties": {
            "decision": {"type": "string"}, "why": {"type": "string"},
            "impact": {"type": "string", "enum": ["high", "medium", "low"]}}, "required": ["decision", "impact"]}},
        "question": {"type": "object", "properties": {
            "kind": {"type": "string", "enum": ["blocking", "fyi"]}, "text": {"type": "string"},
            "options": {"type": "array", "items": {"type": "string"}}, "proceeding_with": {"type": "string"}},
            "required": ["kind", "text"]},
        "notes_for_reviewer": {"type": "string"},
    },
}

REVIEW_SCHEMA = {
    "type": "object",
    "required": ["verdict", "summary"],
    "additionalProperties": False,
    "properties": {
        "verdict": {"type": "string", "enum": ["approve", "request_changes", "escalate"]},
        "summary": {"type": "string"},
        "findings": {"type": "array", "items": {"type": "object", "properties": {
            "severity": {"type": "string", "enum": ["high", "medium", "low"]},
            "file": {"type": "string"}, "line": {"type": "integer"}, "issue": {"type": "string"},
            "fix": {"type": "string"}}, "required": ["severity", "issue"]}},
    },
}


def parse_report(structured: dict | None, worktree: Path, *, changed_files: list[str]) -> Report:
    data = structured if isinstance(structured, dict) and "status" in structured else None
    if data is None:
        f = Path(worktree) / ".swarm-run" / "report.json"
        if f.exists():
            try:
                loaded = json.loads(f.read_text())
                data = loaded if isinstance(loaded, dict) and "status" in loaded else None
            except ValueError:
                data = None
    if data is None:
        return Report(status="done" if changed_files else "failed",
                      summary="Report missing; synthesized from the diff." if changed_files
                      else "Report missing and no files changed.",
                      files_changed=list(changed_files), synthesized=True)
    status = data.get("status")
    if status not in ("done", "blocked", "failed"):
        status = "done"
    q = data.get("question")
    if not isinstance(q, dict) or not str(q.get("text", "")).strip():
        q = None
    else:
        q = {"kind": q.get("kind") if q.get("kind") in ("blocking", "fyi") else "blocking",
             "text": str(q.get("text")).strip(), "options": [str(o) for o in (q.get("options") or [])],
             "proceeding_with": str(q.get("proceeding_with") or "")}
    return Report(
        status=status, summary=str(data.get("summary") or ""),
        files_changed=[str(x) for x in (data.get("files_changed") or changed_files)],
        tests=dict(data.get("tests") or {}), debts=[d for d in (data.get("debts") or []) if isinstance(d, dict)],
        decisions=[d for d in (data.get("decisions") or []) if isinstance(d, dict)], question=q,
        notes_for_reviewer=str(data.get("notes_for_reviewer") or ""),
    )


def report_to_markdown(r: Report, *, attempt: int, verify_ok: bool | None, verify_tail: str,
                       pr_url: str, flags: list[str]) -> str:
    lines = [f"**Attempt {attempt}** · status `{r.status}`" + (" · *synthesized*" if r.synthesized else "")]
    if pr_url:
        lines.append(f"PR: {pr_url}")
    if flags:
        lines.append("Flags: " + ", ".join(f"`{f}`" for f in flags))
    lines += ["", r.summary or "(no summary)", ""]
    if r.files_changed:
        lines.append("Files: " + ", ".join(f"`{f}`" for f in r.files_changed[:40]))
    if r.tests:
        lines.append(f"Model-reported tests: `{r.tests.get('command', '?')}` passed={r.tests.get('passed')}")
    if verify_ok is not None:
        lines += ["", f"Verify (tool-run): {'PASS' if verify_ok else 'FAIL'}"]
        if verify_tail.strip():
            lines += ["```", verify_tail.strip()[-1500:], "```"]
    if r.decisions:
        lines += ["", "Decisions:"]
        lines += [f"- [{d.get('impact', '?')}] {d.get('decision', '')} — {d.get('why', '')}" for d in r.decisions]
    if r.debts:
        lines += ["", "Debts:"]
        lines += [f"- {d.get('kind', '?')} at `{d.get('location', '?')}`: {d.get('reason', '')} → {d.get('fix', '')}"
                  for d in r.debts]
    if r.question:
        lines += ["", f"Question ({r.question['kind']}): {r.question['text']}"]
    if r.notes_for_reviewer:
        lines += ["", "Notes for reviewer: " + r.notes_for_reviewer]
    return "\n".join(lines)


def decisions_markdown(task: Task, r: Report) -> str:
    if not r.decisions:
        return ""
    stamp = utcnow().strftime("%Y-%m-%d %H:%M UTC")
    out = [f"## {task.id} · {task.title} ({stamp})"]
    out += [f"- **[{d.get('impact', '?')}]** {d.get('decision', '')} — {d.get('why', '')}" for d in r.decisions]
    return "\n".join(out) + "\n\n"


def debts_markdown(task: Task, r: Report) -> str:
    if not r.debts:
        return ""
    stamp = utcnow().strftime("%Y-%m-%d %H:%M UTC")
    out = [f"## {task.id} · {task.title} ({stamp})"]
    out += [f"- `{d.get('kind', '?')}` `{d.get('location', '?')}` — {d.get('reason', '')} → fix: {d.get('fix', '')}"
            for d in r.debts]
    return "\n".join(out) + "\n\n"
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_report.py -v`
Expected: 6 passed

- [ ] **Step 5: Commit**

```bash
git add swarm/report.py tests/test_report.py
git commit -m "feat: report schema, parsing with fallbacks, markdown rendering"
```

---

### Task 9: Prompt compiler and worker rules

**Files:**
- Create: `swarm/prompt.py`, `prompts/rules.md`
- Test: `tests/test_prompt.py`

**Interfaces:**
- Consumes: `Config`, `Task`, `REPORT_SCHEMA`.
- Produces: `read_doc(repo_root, ref) -> str | None` (`path` or `path#section`); `select_docs(cfg, task_type) -> list[str]`; `compile_prompt(task, cfg, *, rules_text, deps_summaries, structured_output_supported) -> str`; `load_rules() -> str` (reads `prompts/rules.md` from the package's repo).
- Size policy: each inlined doc capped at 12,000 characters with a `[truncated]` marker.

- [ ] **Step 1: Write prompts/rules.md**

```markdown
# Worker rules

You are one autonomous worker in a swarm building a hackathon project. Other agents are working on other tasks in parallel. You are inside a dedicated git worktree on your own branch.

1. Work only on the task below. Edit only files inside the task's Scope. If you must touch something outside it, say so in the report and keep the change minimal.
2. Never ask a human in chat; nobody is reading. If you genuinely cannot proceed without a decision, put a `question` with kind `blocking` in your report and set status `blocked`. If you made a high-impact choice yourself, put a `question` with kind `fyi`, fill `proceeding_with`, and keep going.
3. Commit as you go with `git add -A && git commit -m "<task id>: <what>"`. Do not push. Do not open pull requests. Do not switch branches. Do not run destructive git commands.
4. Before you finish, run `bash scripts/verify_fast.sh` if it exists and fix what it reports. The harness runs it again after you; a failure sends the task back to you.
5. Every mock, hardcoded value, placeholder, skipped test, fallback, or assumption must be listed under `debts` in the report. Nothing temporary may be invisible.
6. Every decision that changes the product, an interface, or a contract goes under `decisions` with its impact.
7. Do not rewrite `PLAN.md`. Edit `docs/DESIGN.md` only for frontend tasks and `docs/CONTRACTS.md` only for backend tasks, and mention affected consumers.
8. Prefer small, working, tested changes over ambitious half-finished ones. The demo must work.
9. Finish by producing the report in the exact JSON shape given at the end of this prompt. If your CLI cannot return structured output, write the same JSON to `.swarm-run/report.json` in the worktree root.
```

- [ ] **Step 2: Write the failing tests**

```python
# tests/test_prompt.py
from swarm.prompt import read_doc, select_docs, compile_prompt, load_rules
from swarm.models import Task


def test_read_doc_whole_and_section(project_dir):
    assert "Blue buttons" in read_doc(project_dir, "docs/DESIGN.md")
    assert read_doc(project_dir, "docs/CONTRACTS.md#events").strip() == "## events\n{\"a\":1}"
    assert read_doc(project_dir, "docs/CONTRACTS.md#nope") is None
    assert read_doc(project_dir, "docs/missing.md") is None


def test_read_doc_truncates(project_dir):
    (project_dir / "big.md").write_text("x" * 20000)
    text = read_doc(project_dir, "big.md")
    assert len(text) < 12200 and text.endswith("[truncated]")


def test_select_docs(cfg):
    assert select_docs(cfg, "frontend") == ["PLAN.md#summary", "AGENTS.md", "docs/DESIGN.md", "docs/CONTRACTS.md"]
    assert select_docs(cfg, "ml_audio") == ["PLAN.md#summary", "AGENTS.md"]


def test_compile_prompt_contents(cfg):
    t = Task(id="T-016", title="Speaker card", description="Build it", acceptance="- renders\n- 60fps", type="frontend",
             importance="critical", size="M", scope=["frontend/src/**"], depends_on=["T-003"], feedback="Reviewer: fix contrast")
    p = compile_prompt(t, cfg, rules_text="RULES HERE", deps_summaries={"T-003": "Events API"},
                       structured_output_supported=True)
    assert "RULES HERE" in p and "T-016" in p and "Speaker card" in p and "Build it" in p
    assert "- renders" in p and "frontend/src/**" in p and "T-003: Events API" in p
    assert "Reviewer: fix contrast" in p
    assert "Blue buttons" in p and "A demo product." in p and "ml section" not in p
    assert '"status"' in p and ".swarm-run/report.json" not in p
    p2 = compile_prompt(t, cfg, rules_text="R", deps_summaries={}, structured_output_supported=False)
    assert ".swarm-run/report.json" in p2


def test_load_rules():
    assert "Worker rules" in load_rules()
```

- [ ] **Step 3: Run to verify it fails**

Run: `pytest tests/test_prompt.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 4: Implement prompt.py**

```python
# swarm/prompt.py
from __future__ import annotations

import json
import re
from pathlib import Path

from .config import Config
from .models import Task
from .report import REPORT_SCHEMA

DOC_CHAR_CAP = 12000
PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"


def load_rules() -> str:
    return (PROMPTS_DIR / "rules.md").read_text()


def _section(text: str, name: str) -> str | None:
    lines = text.splitlines()
    start, level = None, 0
    for i, line in enumerate(lines):
        m = re.match(r"^(#{1,6})\s+(.*)$", line)
        if m and m.group(2).strip().lower() == name.lower():
            start, level = i, len(m.group(1))
            break
    if start is None:
        return None
    out = [lines[start]]
    for line in lines[start + 1:]:
        m = re.match(r"^(#{1,6})\s+", line)
        if m and len(m.group(1)) <= level:
            break
        out.append(line)
    return "\n".join(out)


def read_doc(repo_root: Path, ref: str) -> str | None:
    path, _, section = ref.partition("#")
    file = Path(repo_root) / path
    if not file.exists():
        return None
    text = file.read_text(errors="replace")
    if section:
        text = _section(text, section)
        if text is None:
            return None
    if len(text) > DOC_CHAR_CAP:
        text = text[:DOC_CHAR_CAP] + "\n[truncated]"
    return text


def select_docs(cfg: Config, task_type: str) -> list[str]:
    refs = list(cfg.docs_by_type.get("_all", [])) + list(cfg.docs_by_type.get(task_type, []))
    return list(dict.fromkeys(refs))


def compile_prompt(task: Task, cfg: Config, *, rules_text: str, deps_summaries: dict[str, str],
                   structured_output_supported: bool) -> str:
    parts = [f"# Worker task {task.id}", "", "## Rules", "", rules_text.strip(), "", "## Task", "",
             f"- ID: {task.id}", f"- Title: {task.title}", f"- Type: {task.type}",
             f"- Importance: {task.importance}", f"- Size: {task.size}",
             f"- Milestone: {task.milestone or '-'}",
             f"- Scope (files you may edit): {', '.join(task.scope) if task.scope else 'whole repo, keep it minimal'}"]
    if task.depends_on:
        parts.append("- Depends on: " + "; ".join(f"{d}: {deps_summaries.get(d, '(see repo)')}" for d in task.depends_on))
    parts += ["", "### Description", "", task.description.strip() or "(none)", "", "### Acceptance criteria", "",
              task.acceptance.strip() or "(none given; make it work and test it)"]
    if task.feedback.strip():
        parts += ["", "## Feedback from the previous attempt (address every item)", "", task.feedback.strip()]
    parts += ["", "## Project context", ""]
    for ref in select_docs(cfg, task.type):
        text = read_doc(cfg.repo_root, ref)
        if text:
            parts += [f"### {ref}", "", text.strip(), ""]
    parts += ["Other docs are in the repo; read them when you need them: PLAN.md, docs/ARCHITECTURE.md, "
              "docs/DESIGN.md, docs/CONTRACTS.md.", "", "## Report contract", ""]
    if structured_output_supported:
        parts.append("Your final answer MUST be a JSON object matching this schema (the harness validates it):")
    else:
        parts.append("Before you finish, write a JSON object matching this schema to `.swarm-run/report.json` "
                     "in the worktree root (create the directory if needed):")
    parts += ["", "```json", json.dumps(REPORT_SCHEMA, indent=1), "```", ""]
    return "\n".join(parts)
```

- [ ] **Step 5: Run tests**

Run: `pytest tests/test_prompt.py -v`
Expected: 5 passed

- [ ] **Step 6: Commit**

```bash
git add swarm/prompt.py prompts/rules.md tests/test_prompt.py
git commit -m "feat: prompt compiler with per-type doc selection and worker rules"
```

---

### Task 10: Router

**Files:**
- Create: `swarm/router.py`
- Test: `tests/test_router.py`

**Interfaces:**
- Consumes: `Config`, `AgentConfig`, `AgentRow`, `Task`.
- Produces: `RouteContext(rows: dict[str, AgentRow], queue_depth: dict[str, int], scopes_by_agent: dict[str, list[str]], now: datetime)`; `route(task, cfg, ctx) -> tuple[str, str, str | None]` (agent, model, effort); `tier_for(task, cfg) -> str`; `model_for(agent_cfg, tier, task_type, cfg) -> tuple[str, str | None]`; `is_available(agent_cfg, row, *, importance, now) -> bool`; `escalate_importance(importance) -> str`; `scopes_overlap(a: list[str], b: list[str]) -> bool`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_router.py
from datetime import timedelta
from swarm.router import RouteContext, route, tier_for, model_for, is_available, escalate_importance, scopes_overlap
from swarm.models import Task, AgentRow, utcnow


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
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_router.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement router.py**

```python
# swarm/router.py
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .config import AgentConfig, Config
from .models import IMPORTANCES, AgentRow, Task, utcnow

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
        return True
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
    candidates = [a for a in agents if is_available(a, ctx.rows.get(a.name), importance=task.importance, now=ctx.now)]
    if not candidates:
        candidates = agents

    def score(a: AgentConfig) -> int:
        return a.strengths.get(task.type, 3)

    def sort_key(a: AgentConfig):
        return (-score(a), ctx.queue_depth.get(a.name, 0), PROVIDER_COST_RANK.get(a.provider, 5), a.name)

    ranked = sorted(candidates, key=sort_key)
    best = ranked[0]
    chosen = best
    # scope affinity: someone already working in these files, within one point
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
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_router.py -v`
Expected: 9 passed

- [ ] **Step 5: Commit**

```bash
git add swarm/router.py tests/test_router.py
git commit -m "feat: deterministic router with tiers, spill, cooldown, soft caps, scope affinity"
```

---

### Task 11: Usage ledger

**Files:**
- Create: `swarm/usage.py`
- Test: `tests/test_usage.py`

**Interfaces:**
- Produces: `Ledger(path: Path)` with `append(*, agent, model, task_id, usage: Usage, duration_s: float, ok: bool, now=None)`, `window(agent, hours=5, now=None) -> Usage`, `totals(agent) -> Usage`, `all_agents() -> list[str]`; `default_ledger_path(project) -> Path` (`~/.swarm/<project>/usage.jsonl`).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_usage.py
from datetime import timedelta
from swarm.usage import Ledger, default_ledger_path
from swarm.models import Usage, utcnow


def test_ledger_append_window_totals(tmp_path):
    led = Ledger(tmp_path / "usage.jsonl")
    now = utcnow()
    led.append(agent="a", model="m", task_id="T-1", usage=Usage(10, 5, 0.5), duration_s=1.0, ok=True, now=now - timedelta(hours=6))
    led.append(agent="a", model="m", task_id="T-2", usage=Usage(20, 5, 1.0), duration_s=1.0, ok=True, now=now - timedelta(hours=1))
    led.append(agent="b", model="m", task_id="T-3", usage=Usage(1, 1, None), duration_s=1.0, ok=False, now=now)
    w = led.window("a", hours=5, now=now)
    assert (w.input_tokens, w.output_tokens, w.cost_usd) == (20, 5, 1.0)
    t = led.totals("a")
    assert (t.input_tokens, t.cost_usd) == (30, 1.5)
    assert led.totals("b").cost_usd == 0.0 and led.all_agents() == ["a", "b"]


def test_ledger_missing_file_is_empty(tmp_path):
    led = Ledger(tmp_path / "nope" / "usage.jsonl")
    assert led.window("x").cost_usd == 0.0
    led.append(agent="x", model="m", task_id="T", usage=Usage(), duration_s=0, ok=True)
    assert (tmp_path / "nope" / "usage.jsonl").exists()


def test_default_path():
    assert str(default_ledger_path("demo")).endswith("/.swarm/demo/usage.jsonl")
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_usage.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement usage.py**

```python
# swarm/usage.py
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

    def _sum(self, rows) -> Usage:
        u = Usage(cost_usd=0.0)
        for r in rows:
            u.input_tokens += int(r.get("in") or 0)
            u.output_tokens += int(r.get("out") or 0)
            u.cost_usd += float(r.get("cost") or 0.0)
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
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_usage.py -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add swarm/usage.py tests/test_usage.py
git commit -m "feat: usage ledger with 5-hour windows"
```

---

### Task 12: Review policy and the Runner (worker loop)

**Files:**
- Create: `swarm/policy.py`, `swarm/runner.py`
- Test: `tests/test_runner.py`

**Interfaces:**
- Consumes: `Board`, `claim_task`, `Workspace`, adapters, `compile_prompt`, `parse_report`, `report_to_markdown`, `decisions_markdown`, `debts_markdown`, `Ledger`.
- Produces:
  - `policy.needs_review(task, cfg) -> bool`; `policy.glob_match(path, glob) -> bool`; `policy.in_scope(path, scope) -> bool`.
  - `runner.SyncExecutor` (test double with `submit(fn, *a) -> future` and `shutdown()`).
  - `runner.Outcome(task, report, result, verify_ok, status)`.
  - `runner.Runner(cfg, board, host, ws, *, ledger, adapter_factory=get_adapter, sleep=time.sleep, now=utcnow, log=print, executor=None, rules_text=None)` with `free_slots(agent)`, `heartbeat(force=False)`, `pending_tasks()`, `tick() -> int`, `run_task(task) -> Outcome`, `loop(once=False, stop=lambda: False)`.
  - Constants: `STRUCTURED_PROVIDERS = {"claude", "codex"}`, `ALWAYS_REVIEWED_DOCS = ("docs/CONTRACTS.md", "docs/DESIGN.md")`, `RATE_LIMIT_COOLDOWN_MIN = 15`.
  - Decision and debt logs are written per task into the branch as `docs/decisions/<task id>.md` and `docs/debt/<task id>.md` (no shared-file conflicts).
  - Flag `resume` on a task means "reuse the pushed branch"; set by reaper, relay, reviewer, and the runner itself when a CLI ended abnormally with changes.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_runner.py
import json
from datetime import timedelta
from pathlib import Path

import pytest

from swarm.board.memory import InMemoryBoard
from swarm.models import Task, Status, AgentRow, RunResult, Usage, utcnow
from swarm.policy import needs_review, glob_match, in_scope
from swarm.runner import Runner, SyncExecutor
from swarm.usage import Ledger
from swarm.workspace import Workspace, CmdResult


class FakeAdapter:
    """Writes files into cwd and returns a canned RunResult."""

    def __init__(self, files=None, structured=None, ok=True, timed_out=False, rate_limited=False, report_file=None):
        self.files = files or {}
        self.structured = structured
        self.ok = ok
        self.timed_out = timed_out
        self.rate_limited = rate_limited
        self.report_file = report_file
        self.specs = []

    def run(self, spec):
        self.specs.append(spec)
        for rel, content in self.files.items():
            p = spec.cwd / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content)
        if self.report_file is not None:
            (spec.cwd / ".swarm-run" / "report.json").write_text(json.dumps(self.report_file))
        return RunResult(ok=self.ok, exit_code=0 if self.ok else 1, stdout="", stderr="",
                         structured_output=self.structured, usage=Usage(10, 5, 0.25),
                         timed_out=self.timed_out, rate_limited=self.rate_limited,
                         error="" if self.ok else "boom")


def fake_gh(args, cwd):
    if args[:2] == ["pr", "view"]:
        return CmdResult(1, "", "none")
    if args[:2] == ["pr", "create"]:
        return CmdResult(0, "https://gh/pr/1\n", "")
    return CmdResult(0, "", "")


def make_runner(cfg, git_repo, tmp_path, adapter, host="host-a"):
    board = InMemoryBoard()
    ws = Workspace(git_repo, tmp_path / "wt", gh=fake_gh)
    ledger = Ledger(tmp_path / "usage.jsonl")
    r = Runner(cfg, board, host, ws, ledger=ledger, adapter_factory=lambda a: adapter,
               sleep=lambda s: None, executor=SyncExecutor(), rules_text="RULES", log=lambda *a: None)
    return r, board


def ready_task(board, **kw):
    base = dict(id="", title="Thing", type="backend", importance="normal", size="S", status=Status.READY,
                agent="codex-a", model="gpt-6-sol", effort="medium", scope=["src/**"])
    base.update(kw)
    return board.create_task(Task(**base))


def test_policy_and_globs(cfg):
    assert glob_match("src/a/b.py", "src/**") and glob_match("src/x.py", "src/*.py")
    assert not glob_match("docs/a.md", "src/**") and not glob_match("src/a/b.py", "src/*.py")
    assert in_scope("src/a.py", ["src/**"]) and in_scope("anything", [])
    t = Task(id="T", title="", importance="normal")
    assert needs_review(t, cfg) is False
    assert needs_review(Task(id="T", title="", importance="high"), cfg) is True
    assert needs_review(Task(id="T", title="", importance="low", flags=["out_of_scope"]), cfg) is True
    cfg.review_policy = "all"
    assert needs_review(t, cfg) is True
    cfg.review_policy = "none"
    assert needs_review(Task(id="T", title="", importance="critical"), cfg) is False


def test_done_normal_goes_merge_ready(cfg, git_repo, tmp_path):
    adapter = FakeAdapter(files={"src/a.py": "print(1)\n"},
                          structured={"status": "done", "summary": "added a", "files_changed": ["src/a.py"],
                                      "decisions": [{"decision": "use print", "why": "simple", "impact": "low"}]})
    r, board = make_runner(cfg, git_repo, tmp_path, adapter)
    t = ready_task(board)
    out = r.run_task(t)
    stored = board.get_task(t.id)
    assert out.status is Status.MERGE_READY and stored.status is Status.MERGE_READY
    assert stored.pr_url == "https://gh/pr/1" and stored.attempts == 1 and stored.claim_nonce == ""
    assert board.reports[t.id][0][0] == "Report — attempt 1"
    assert adapter.specs[0].model == "gpt-6-sol" and adapter.specs[0].max_turns == 30
    assert "RULES" in adapter.specs[0].prompt_file.read_text()
    assert not r.ws.worktree_path(t.id).exists()
    assert r.ledger.totals("codex-a").cost_usd == 0.25
    # decisions log landed on the branch
    wt = r.ws.provision(t.id, reuse_branch=True)
    assert (wt / "docs" / "decisions" / f"{t.id}.md").exists()
    r.ws.dispose(wt)


def test_high_importance_goes_review(cfg, git_repo, tmp_path):
    adapter = FakeAdapter(files={"src/a.py": "x"}, structured={"status": "done", "summary": "s"})
    r, board = make_runner(cfg, git_repo, tmp_path, adapter)
    t = ready_task(board, importance="high")
    assert r.run_task(t).status is Status.REVIEW


def test_out_of_scope_forces_review(cfg, git_repo, tmp_path):
    adapter = FakeAdapter(files={"other/a.py": "x"}, structured={"status": "done", "summary": "s"})
    r, board = make_runner(cfg, git_repo, tmp_path, adapter)
    t = ready_task(board)
    out = r.run_task(t)
    assert out.status is Status.REVIEW and "out_of_scope" in board.get_task(t.id).flags


def test_blocked_creates_question(cfg, git_repo, tmp_path):
    adapter = FakeAdapter(files={"src/a.py": "x"},
                          structured={"status": "blocked", "summary": "need db choice",
                                      "question": {"kind": "blocking", "text": "sqlite or pg?", "options": ["sqlite", "pg"]}})
    r, board = make_runner(cfg, git_repo, tmp_path, adapter)
    t = ready_task(board)
    assert r.run_task(t).status is Status.BLOCKED
    q = board.list_questions(status="Open")[0]
    assert q.text == "sqlite or pg?" and q.task_id == t.id and q.asked_by == "codex-a" and q.kind == "blocking"


def test_fyi_question_keeps_going(cfg, git_repo, tmp_path):
    adapter = FakeAdapter(files={"src/a.py": "x"},
                          structured={"status": "done", "summary": "s",
                                      "question": {"kind": "fyi", "text": "chose 3s", "proceeding_with": "3s"}})
    r, board = make_runner(cfg, git_repo, tmp_path, adapter)
    t = ready_task(board)
    assert r.run_task(t).status is Status.MERGE_READY
    assert board.list_questions()[0].kind == "fyi"


def test_done_without_changes_is_failed(cfg, git_repo, tmp_path):
    adapter = FakeAdapter(files={}, structured={"status": "done", "summary": "claims done"})
    r, board = make_runner(cfg, git_repo, tmp_path, adapter)
    t = ready_task(board)
    assert r.run_task(t).status is Status.FAILED
    assert board.get_task(t.id).last_error == "no changes"


def test_verify_failure_goes_changes_requested(cfg, git_repo, tmp_path):
    (git_repo / "scripts" / "verify_fast.sh").write_text("#!/bin/sh\necho 'FAIL 1/3'; exit 1\n")
    import subprocess
    subprocess.run(["git", "-C", str(git_repo), "commit", "-qam", "failing verify"], check=True)
    subprocess.run(["git", "-C", str(git_repo), "push", "-q", "origin", "main"], check=True)
    adapter = FakeAdapter(files={"src/a.py": "x"}, structured={"status": "done", "summary": "s"})
    r, board = make_runner(cfg, git_repo, tmp_path, adapter)
    t = ready_task(board)
    out = r.run_task(t)
    stored = board.get_task(t.id)
    assert out.status is Status.CHANGES_REQUESTED and "FAIL 1/3" in stored.feedback
    assert stored.review_rounds == 1 and "resume" in stored.flags


def test_verify_failure_beyond_rounds_blocks(cfg, git_repo, tmp_path):
    (git_repo / "scripts" / "verify_fast.sh").write_text("#!/bin/sh\nexit 1\n")
    import subprocess
    subprocess.run(["git", "-C", str(git_repo), "commit", "-qam", "failing verify"], check=True)
    subprocess.run(["git", "-C", str(git_repo), "push", "-q", "origin", "main"], check=True)
    adapter = FakeAdapter(files={"src/a.py": "x"}, structured={"status": "done", "summary": "s"})
    r, board = make_runner(cfg, git_repo, tmp_path, adapter)
    t = ready_task(board, review_rounds=2, status=Status.CHANGES_REQUESTED)
    assert r.run_task(t).status is Status.BLOCKED
    assert board.list_questions(status="Open")[0].kind == "blocking"


def test_rate_limit_returns_to_ready_and_cools_down(cfg, git_repo, tmp_path):
    adapter = FakeAdapter(files={}, ok=False, rate_limited=True)
    r, board = make_runner(cfg, git_repo, tmp_path, adapter)
    t = ready_task(board)
    out = r.run_task(t)
    stored = board.get_task(t.id)
    assert out.status is Status.READY and stored.attempts == 0 and stored.claim_nonce == ""
    row = board.get_agent("codex-a")
    assert row.status == "cooldown" and row.cooldown_until > utcnow() + timedelta(minutes=10)


def test_timeout_without_changes_is_failed(cfg, git_repo, tmp_path):
    adapter = FakeAdapter(files={}, ok=False, timed_out=True)
    r, board = make_runner(cfg, git_repo, tmp_path, adapter)
    t = ready_task(board)
    assert r.run_task(t).status is Status.FAILED
    assert "timeout" in board.get_task(t.id).flags


def test_abnormal_end_with_changes_resumes(cfg, git_repo, tmp_path):
    adapter = FakeAdapter(files={"src/a.py": "half"}, ok=False, structured=None)
    r, board = make_runner(cfg, git_repo, tmp_path, adapter)
    t = ready_task(board)
    out = r.run_task(t)
    stored = board.get_task(t.id)
    assert out.status is Status.CHANGES_REQUESTED and "resume" in stored.flags and "report_missing" in stored.flags
    assert "ended with" in stored.feedback


def test_report_from_file_for_generic(cfg, git_repo, tmp_path):
    adapter = FakeAdapter(files={"src/a.py": "x"}, report_file={"status": "done", "summary": "via file"})
    r, board = make_runner(cfg, git_repo, tmp_path, adapter, host="host-b")
    t = ready_task(board, agent="fake-b", model="x", effort=None)
    assert r.run_task(t).status is Status.MERGE_READY
    assert adapter.specs[0].schema is None
    assert ".swarm-run/report.json" in adapter.specs[0].prompt_file.read_text()


def test_tick_respects_slots_and_skips_unknown_agent(cfg, git_repo, tmp_path):
    adapter = FakeAdapter(files={"src/a.py": "x"}, structured={"status": "done", "summary": "s"})
    r, board = make_runner(cfg, git_repo, tmp_path, adapter)
    for i in range(4):
        ready_task(board, title=f"t{i}", agent="codex-a")
    ready_task(board, title="ghost", agent="not-configured")
    ready_task(board, title="other host", agent="fake-b")
    n = r.tick()
    assert n == 4  # SyncExecutor runs each immediately; codex-a parallel=1 but slot frees after each run
    assert board.get_task("T-005").status is Status.READY and board.get_task("T-006").status is Status.READY
    rows = {a.name for a in board.list_agents()}
    assert {"claude-a", "codex-a"} <= rows


def test_heartbeat_writes_rows(cfg, git_repo, tmp_path):
    adapter = FakeAdapter()
    r, board = make_runner(cfg, git_repo, tmp_path, adapter)
    r.heartbeat(force=True)
    row = board.get_agent("claude-a")
    assert row.host == "host-a" and row.status == "idle" and row.last_heartbeat is not None


def test_cooldown_agent_not_dispatched_unless_critical(cfg, git_repo, tmp_path):
    adapter = FakeAdapter(files={"src/a.py": "x"}, structured={"status": "done", "summary": "s"})
    r, board = make_runner(cfg, git_repo, tmp_path, adapter)
    board.upsert_agent(AgentRow(name="codex-a", status="cooldown", cooldown_until=utcnow() + timedelta(minutes=5)))
    ready_task(board, title="n", importance="normal")
    ready_task(board, title="c", importance="critical")
    assert r.tick() == 1
    assert board.get_task("T-001").status is Status.READY and board.get_task("T-002").status is Status.MERGE_READY
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_runner.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement policy.py**

```python
# swarm/policy.py
from __future__ import annotations

import re

from .config import Config
from .models import Task

FORCE_REVIEW_FLAGS = ("report_missing", "out_of_scope", "docs_touched")


def glob_match(path: str, glob: str) -> bool:
    pattern = re.escape(glob).replace(r"\*\*/", "(?:.*/)?").replace(r"\*\*", ".*").replace(r"\*", "[^/]*")
    return re.fullmatch(pattern, path) is not None


def in_scope(path: str, scope: list[str]) -> bool:
    if not scope:
        return True
    return any(glob_match(path, g) for g in scope)


def needs_review(task: Task, cfg: Config) -> bool:
    if any(f in task.flags for f in FORCE_REVIEW_FLAGS):
        return True
    policy = cfg.review_policy
    if policy == "all":
        return True
    if policy == "none":
        return False
    if policy == "critical_only":
        return task.importance == "critical"
    return task.importance in ("critical", "high")
```

- [ ] **Step 4: Implement runner.py**

```python
# swarm/runner.py
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Callable

from .adapters import get_adapter
from .adapters.base import RunSpec
from .board.base import Board, claim_task
from .config import Config
from .models import AgentRow, Question, Report, RunResult, Status, Task, utcnow
from .policy import in_scope, needs_review
from .prompt import compile_prompt, load_rules
from .report import REPORT_SCHEMA, debts_markdown, decisions_markdown, parse_report, report_to_markdown
from .usage import Ledger
from .workspace import Workspace

STRUCTURED_PROVIDERS = {"claude", "codex"}
ALWAYS_REVIEWED_DOCS = ("docs/CONTRACTS.md", "docs/DESIGN.md")
RATE_LIMIT_COOLDOWN_MIN = 15
IDLE_AFTER_S = 300
PUBLISH_FIELDS = ["status", "attempts", "flags", "pr_url", "claim_nonce", "feedback", "last_error", "review_rounds"]


class _Done:
    def __init__(self, value):
        self._v = value

    def result(self):
        return self._v


class SyncExecutor:
    """Runs submitted callables immediately; used in tests and --once."""

    def submit(self, fn, *args, **kwargs):
        return _Done(fn(*args, **kwargs))

    def shutdown(self, wait: bool = True) -> None:
        pass


@dataclass
class Outcome:
    task: Task
    report: Report | None
    result: RunResult | None
    verify_ok: bool | None
    status: Status


class Runner:
    def __init__(self, cfg: Config, board: Board, host: str, ws: Workspace, *, ledger: Ledger,
                 adapter_factory=get_adapter, sleep: Callable[[float], None] = time.sleep, now=utcnow,
                 log=print, executor=None, rules_text: str | None = None):
        if host not in cfg.hosts:
            raise ValueError(f"host '{host}' is not in config.hosts")
        self.cfg, self.board, self.host, self.ws, self.ledger = cfg, board, host, ws, ledger
        self.adapter_factory, self.sleep, self.now, self.log = adapter_factory, sleep, now, log
        self.agents = {a.name: a for a in cfg.agents_on_host(host)}
        self.rules_text = rules_text if rules_text is not None else load_rules()
        self.lock = threading.Lock()
        self.active: dict[str, set[str]] = {name: set() for name in self.agents}
        workers = max(1, sum(a.parallel for a in self.agents.values()))
        self.executor = executor or ThreadPoolExecutor(max_workers=workers)
        self._last_heartbeat = None
        self._idle_since = None

    # ----- capacity -----
    def free_slots(self, agent_name: str) -> int:
        a = self.agents[agent_name]
        host = self.cfg.hosts[self.host]
        with self.lock:
            used_agent = len(self.active[agent_name])
            used_provider = sum(len(self.active[n]) for n, c in self.agents.items() if c.provider == a.provider)
        cap_provider = host.max_parallel.get(a.provider, 1)
        return max(0, min(a.parallel - used_agent, cap_provider - used_provider))

    # ----- heartbeat -----
    def heartbeat(self, force: bool = False) -> None:
        now = self.now()
        if not force and self._last_heartbeat and (now - self._last_heartbeat).total_seconds() < self.cfg.heartbeat_seconds:
            return
        self._last_heartbeat = now
        for name, a in self.agents.items():
            row = self.board.get_agent(name) or AgentRow(name=name)
            row.provider, row.host, row.last_heartbeat = a.provider, self.host, now
            with self.lock:
                current = sorted(self.active[name])
            if row.cooldown_until and row.cooldown_until > now:
                row.status = "cooldown"
            else:
                row.status = "running" if current else "idle"
                row.cooldown_until = None
            row.current_task = ", ".join(current)
            row.cost_5h_usd = self.ledger.window(name, 5, now).cost_usd or 0.0
            self.board.upsert_agent(row)

    # ----- polling -----
    def pending_tasks(self) -> list[Task]:
        tasks = self.board.list_tasks(status=[Status.READY, Status.CHANGES_REQUESTED], agent=list(self.agents))
        return [t for t in tasks if t.agent in self.agents]

    def tick(self) -> int:
        self.heartbeat()
        dispatched = 0
        now = self.now()
        for task in self.pending_tasks():
            row = self.board.get_agent(task.agent)
            if (row and row.cooldown_until and row.cooldown_until > now and task.importance != "critical"):
                continue
            if self.free_slots(task.agent) <= 0:
                continue
            with self.lock:
                if task.id in self.active[task.agent]:
                    continue
                self.active[task.agent].add(task.id)
            self.executor.submit(self._guarded_run, task)
            dispatched += 1
        return dispatched

    def _guarded_run(self, task: Task) -> None:
        try:
            self.run_task(task)
        except Exception as e:  # noqa: BLE001 - a worker crash must never kill the loop
            self.log(f"[{task.id}] runner crashed: {e!r}")
            try:
                fresh = self.board.get_task(task.id)
                if fresh and fresh.status is Status.RUNNING:
                    fresh.status, fresh.last_error, fresh.claim_nonce = Status.FAILED, f"runner crash: {e!r}"[:1900], ""
                    fresh.attempts += 1
                    self.board.update_task(fresh, ["status", "last_error", "claim_nonce", "attempts"])
            except Exception as e2:  # noqa: BLE001
                self.log(f"[{task.id}] could not record crash: {e2!r}")
        finally:
            with self.lock:
                self.active.get(task.agent, set()).discard(task.id)

    def loop(self, *, once: bool = False, stop: Callable[[], bool] = lambda: False) -> None:
        self.log(f"swarm run · host={self.host} · agents={', '.join(self.agents)}")
        while not stop():
            n = self.tick()
            if once:
                break
            now = self.now()
            if n == 0:
                self._idle_since = self._idle_since or now
                idle_for = (now - self._idle_since).total_seconds()
                self.sleep(self.cfg.idle_poll_seconds if idle_for > IDLE_AFTER_S else self.cfg.poll_seconds)
            else:
                self._idle_since = None
                self.sleep(self.cfg.poll_seconds)
        self.executor.shutdown(wait=True)

    # ----- one task -----
    def run_task(self, task: Task) -> Outcome:
        agent_cfg = self.agents[task.agent]
        prev_status = task.status
        if not claim_task(self.board, task, task.agent, sleep=self.sleep):
            self.log(f"[{task.id}] claim lost")
            return Outcome(task, None, None, None, task.status)
        attempt = task.attempts + 1
        reuse = prev_status is Status.CHANGES_REQUESTED or "resume" in task.flags
        self.log(f"[{task.id}] {task.agent} attempt {attempt} model={task.model} reuse={reuse}")
        started = self.now()
        wt = self.ws.provision(task.id, reuse_branch=reuse)
        try:
            self.ws.run_script(wt, self.cfg.verify.setup_worktree, 600)
            deps = {}
            for d in task.depends_on:
                dep = self.board.get_task(d)
                deps[d] = dep.title if dep else ""
            structured = agent_cfg.provider in STRUCTURED_PROVIDERS
            prompt = compile_prompt(task, self.cfg, rules_text=self.rules_text, deps_summaries=deps,
                                    structured_output_supported=structured)
            pf = wt / ".swarm-run" / "prompt.md"
            pf.write_text(prompt)
            limit = self.cfg.limit_for(task.size)
            model = task.model or agent_cfg.models["mid"]
            effort = task.effort or agent_cfg.effort.get("mid")
            spec = RunSpec(prompt_file=pf, model=model, effort=effort, max_turns=limit.turns,
                           budget_usd=limit.budget_usd, timeout_s=limit.minutes * 60, cwd=wt,
                           schema=REPORT_SCHEMA if structured else None, sandbox=agent_cfg.sandbox,
                           extra_args=list(agent_cfg.extra_args))
            result = self.adapter_factory(agent_cfg).run(spec)
            duration = (self.now() - started).total_seconds()
            self.ledger.append(agent=task.agent, model=model, task_id=task.id, usage=result.usage,
                               duration_s=duration, ok=result.ok)
            self._save_logs(wt, attempt, result)
            if result.rate_limited:
                return self._rate_limited(task, result)
            return self._publish(task, wt, attempt, result, prev_status)
        finally:
            self.ws.dispose(wt)

    def _save_logs(self, wt: Path, attempt: int, result: RunResult) -> None:
        logdir = Path.home() / ".swarm" / self.cfg.project / "runs" / wt.name / f"attempt-{attempt}"
        try:
            logdir.mkdir(parents=True, exist_ok=True)
            (logdir / "stdout.txt").write_text(result.stdout)
            (logdir / "stderr.txt").write_text(result.stderr)
            prompt = wt / ".swarm-run" / "prompt.md"
            if prompt.exists():
                (logdir / "prompt.md").write_text(prompt.read_text())
        except OSError as e:
            self.log(f"could not save logs: {e}")

    def _rate_limited(self, task: Task, result: RunResult) -> Outcome:
        now = self.now()
        task.status, task.claim_nonce = Status.READY, ""
        task.last_error = f"rate limited: {result.error[:300]}"
        self.board.update_task(task, ["status", "claim_nonce", "last_error"])
        row = self.board.get_agent(task.agent) or AgentRow(name=task.agent, provider=self.agents[task.agent].provider,
                                                            host=self.host)
        row.status = "cooldown"
        row.cooldown_until = result.reset_at or (now + timedelta(minutes=RATE_LIMIT_COOLDOWN_MIN))
        row.note = f"rate limited at {now.isoformat(timespec='minutes')}"
        self.board.upsert_agent(row)
        self.log(f"[{task.id}] rate limited; {task.agent} cooling down until {row.cooldown_until}")
        return Outcome(task, None, result, None, Status.READY)

    def _publish(self, task: Task, wt: Path, attempt: int, result: RunResult, prev_status: Status) -> Outcome:
        changed = self.ws.changed_files(wt)
        report = parse_report(result.structured_output, wt, changed_files=changed)
        flags = [f for f in task.flags if f not in ("resume", "report_missing", "out_of_scope", "docs_touched", "timeout")]
        if report.synthesized:
            flags.append("report_missing")
        if any(not in_scope(f, task.scope) for f in changed):
            flags.append("out_of_scope")
        if any(f in ALWAYS_REVIEWED_DOCS for f in changed):
            flags.append("docs_touched")
        if result.timed_out:
            flags.append("timeout")
        task.flags, task.attempts, task.claim_nonce = flags, attempt, ""

        # decision / debt logs live on the branch, one file per task
        dec, debt = decisions_markdown(task, report), debts_markdown(task, report)
        if dec:
            (wt / "docs" / "decisions").mkdir(parents=True, exist_ok=True)
            (wt / "docs" / "decisions" / f"{task.id}.md").write_text(dec)
        if debt:
            (wt / "docs" / "debt").mkdir(parents=True, exist_ok=True)
            (wt / "docs" / "debt" / f"{task.id}.md").write_text(debt)
        if dec or debt:
            changed = self.ws.changed_files(wt)

        verify = self.ws.run_script(wt, self.cfg.verify.fast, 900) if changed else None
        verify_ok = verify.ok if verify is not None else None
        verify_tail = verify.tail(1500) if verify is not None else ""

        pr_url = task.pr_url
        if changed:
            self.ws.commit_all(wt, f"{task.id}: {(report.summary or 'work in progress')[:60]}")
            push = self.ws.push(wt, task.branch)
            if push.ok:
                body = report_to_markdown(report, attempt=attempt, verify_ok=verify_ok, verify_tail=verify_tail,
                                          pr_url="", flags=flags)
                try:
                    pr_url = self.ws.pr_create_or_update(task.branch, task.title_with_id(), body) or pr_url
                except RuntimeError as e:
                    self.log(f"[{task.id}] PR failed: {e}")
            else:
                self.log(f"[{task.id}] push failed: {push.err.strip()[:200]}")
        task.pr_url = pr_url

        md = report_to_markdown(report, attempt=attempt, verify_ok=verify_ok, verify_tail=verify_tail,
                                pr_url=pr_url, flags=flags)
        self.board.append_task_report(task, f"Report — attempt {attempt}", md)
        if report.question:
            self._file_question(task, report, report.question)

        status = self._decide(task, report, result, changed, verify_ok, verify_tail)
        task.status = status
        self.board.update_task(task, PUBLISH_FIELDS)
        self.log(f"[{task.id}] → {status.value}")
        return Outcome(task, report, result, verify_ok, status)

    def _decide(self, task: Task, report: Report, result: RunResult, changed: list[str],
                verify_ok: bool | None, verify_tail: str) -> Status:
        if report.status == "blocked":
            return Status.BLOCKED
        if report.status == "failed":
            task.last_error = (report.summary or "model reported failure")[:1900]
            return Status.FAILED
        if not changed:
            task.last_error = "timeout" if result.timed_out else ("no changes" if result.ok else result.error[:1900])
            return Status.FAILED
        if not result.ok and report.synthesized:
            # the CLI ended abnormally (max turns, timeout, crash) but left work behind: continue on the branch
            if task.attempts >= self.cfg.max_attempts:
                task.last_error = result.error[:1900] or "abnormal end"
                return Status.FAILED
            task.feedback = (f"The previous attempt ended with: {result.error or 'unknown error'}. "
                             "Continue from the current branch state, finish the task, and produce the report.")
            task.flags = list(dict.fromkeys(task.flags + ["resume"]))
            return Status.CHANGES_REQUESTED
        if verify_ok is False:
            task.review_rounds += 1
            if task.review_rounds > self.cfg.max_review_rounds:
                self._file_question(task, report, {"kind": "blocking",
                                                   "text": f"{task.id} keeps failing verify after {task.review_rounds} rounds. Cut, split, or fix by hand?",
                                                   "options": ["cut", "split", "human fix"], "proceeding_with": ""},
                                    context=verify_tail)
                return Status.BLOCKED
            task.feedback = "scripts/verify_fast.sh failed. Fix it:\n" + verify_tail[-1800:]
            task.flags = list(dict.fromkeys(task.flags + ["resume"]))
            return Status.CHANGES_REQUESTED
        task.feedback = ""
        return Status.REVIEW if needs_review(task, self.cfg) else Status.MERGE_READY

    def _file_question(self, task: Task, report: Report, q: dict, *, context: str = "") -> Question:
        question = Question(
            id="", text=q["text"][:190], kind=q.get("kind", "blocking"),
            context=(context or report.summary)[:1900], options=list(q.get("options") or []),
            proceeding_with=q.get("proceeding_with", ""), impact="high" if q.get("kind") == "blocking" else "medium",
            task_id=task.id, asked_by=task.agent or "", status="Open",
        )
        return self.board.create_question(question)
```

- [ ] **Step 5: Run tests**

Run: `pytest tests/test_runner.py -v`
Expected: 16 passed

- [ ] **Step 6: Commit**

```bash
git add swarm/policy.py swarm/runner.py tests/test_runner.py
git commit -m "feat: worker runner with claim, worktree, adapter run, verify, publish, and review policy"
```

---

### Task 13: Reviewer

**Files:**
- Create: `swarm/reviewer.py`, `prompts/reviewer.md`
- Test: `tests/test_reviewer.py`

**Interfaces:**
- Consumes: `Workspace`, adapters, `REVIEW_SCHEMA`, `Board`.
- Produces: `Verdict(verdict: str, summary: str, findings: list[dict])`; `Reviewer(cfg, board, ws, *, adapter_factory=get_adapter, log=print, prompt_text=None)` with `review(task) -> Verdict`, `apply(task, verdict) -> Task`, `process(task) -> Task`; `build_review_prompt(task, diff, verify_tail, instructions) -> str`; `parse_verdict(structured, worktree) -> Verdict`.

- [ ] **Step 1: Write prompts/reviewer.md**

```markdown
# Reviewer instructions

You are an independent reviewer. You did not write this change. Judge it only against the task's acceptance criteria, the diff, and the verify output. Be concrete: every finding names a file and what to change.

- `approve` when the acceptance criteria are met, the tests exercise the change, nothing outside scope was touched without reason, and no temporary hack is unlisted.
- `request_changes` when specific, fixable problems exist. List them with severity, file, line if known, issue, fix.
- `escalate` only when the task itself is wrong, the change conflicts with the contracts or design docs in a way a worker cannot resolve, or you cannot evaluate it.

Do not modify files. Do not run anything that writes. Your final answer is the JSON verdict.
```

- [ ] **Step 2: Write the failing tests**

```python
# tests/test_reviewer.py
import json
from swarm.board.memory import InMemoryBoard
from swarm.models import Task, Status, RunResult, Usage
from swarm.reviewer import Reviewer, Verdict, parse_verdict, build_review_prompt
from swarm.workspace import Workspace, CmdResult


class FakeReviewAdapter:
    def __init__(self, structured):
        self.structured = structured
        self.specs = []

    def run(self, spec):
        self.specs.append(spec)
        return RunResult(ok=True, exit_code=0, stdout="", stderr="", structured_output=self.structured, usage=Usage())


def setup(cfg, git_repo, tmp_path, structured, verify_full_ok=True):
    board = InMemoryBoard()
    ws = Workspace(git_repo, tmp_path / "wt", gh=lambda a, c: CmdResult(0, "", ""))
    # push a branch with a change
    t = board.create_task(Task(id="", title="Cards", type="frontend", importance="high", status=Status.REVIEW,
                               acceptance="- renders", agent="claude-a", scope=["src/**"]))
    wt = ws.provision(t.id)
    (wt / "src").mkdir()
    (wt / "src" / "c.py").write_text("x = 1\n")
    (wt / "scripts" / "verify_full.sh").write_text("#!/bin/sh\nexit 0\n" if verify_full_ok else "#!/bin/sh\necho FULLFAIL; exit 1\n")
    ws.commit_all(wt, "change")
    ws.push(wt, t.branch)
    ws.dispose(wt)
    adapter = FakeReviewAdapter(structured)
    rev = Reviewer(cfg, board, ws, adapter_factory=lambda a: adapter, log=lambda *a: None, prompt_text="REVIEW RULES")
    return rev, board, t, adapter


def test_parse_verdict_variants(tmp_path):
    v = parse_verdict({"verdict": "approve", "summary": "fine", "findings": []}, tmp_path)
    assert v.verdict == "approve"
    (tmp_path / ".swarm-run").mkdir()
    (tmp_path / ".swarm-run" / "review.json").write_text(json.dumps({"verdict": "request_changes", "summary": "s", "findings": [{"severity": "high", "issue": "x"}]}))
    v2 = parse_verdict(None, tmp_path)
    assert v2.verdict == "request_changes" and v2.findings[0]["issue"] == "x"
    v3 = parse_verdict({"verdict": "nonsense"}, tmp_path / "nowhere")
    assert v3.verdict == "escalate"


def test_prompt_contains_pieces():
    t = Task(id="T-1", title="Cards", acceptance="- renders", scope=["src/**"])
    p = build_review_prompt(t, "diff --git a", "verify tail", "RULES")
    assert "RULES" in p and "- renders" in p and "diff --git a" in p and "verify tail" in p and "T-1" in p


def test_approve_goes_merge_ready(cfg, git_repo, tmp_path):
    rev, board, t, adapter = setup(cfg, git_repo, tmp_path, {"verdict": "approve", "summary": "good", "findings": []})
    out = rev.process(t)
    assert out.status is Status.MERGE_READY and board.get_task(t.id).status is Status.MERGE_READY
    assert adapter.specs[0].read_only is True and adapter.specs[0].model == "gpt-6-sol"
    assert "REVIEW RULES" in adapter.specs[0].prompt_file.read_text()
    assert board.reports[t.id][0][0] == "Review — round 1"


def test_request_changes_then_escalate(cfg, git_repo, tmp_path):
    rev, board, t, adapter = setup(cfg, git_repo, tmp_path,
                                   {"verdict": "request_changes", "summary": "nope",
                                    "findings": [{"severity": "high", "file": "src/c.py", "line": 1, "issue": "wrong", "fix": "make right"}]})
    out = rev.process(t)
    stored = board.get_task(t.id)
    assert out.status is Status.CHANGES_REQUESTED and stored.review_rounds == 1
    assert "src/c.py" in stored.feedback and "make right" in stored.feedback and "resume" in stored.flags
    stored.status = Status.REVIEW
    board.update_task(stored, ["status"])
    rev.process(board.get_task(t.id))
    stored2 = board.get_task(t.id)
    assert stored2.status is Status.CHANGES_REQUESTED and stored2.review_rounds == 2
    stored2.status = Status.REVIEW
    board.update_task(stored2, ["status"])
    rev.process(board.get_task(t.id))
    assert board.get_task(t.id).status is Status.BLOCKED
    assert board.list_questions(status="Open")[0].kind == "blocking"


def test_verify_full_failure_skips_model(cfg, git_repo, tmp_path):
    rev, board, t, adapter = setup(cfg, git_repo, tmp_path, {"verdict": "approve", "summary": "x"}, verify_full_ok=False)
    out = rev.process(t)
    assert out.status is Status.CHANGES_REQUESTED and "FULLFAIL" in board.get_task(t.id).feedback
    assert adapter.specs == []


def test_escalate_creates_question(cfg, git_repo, tmp_path):
    rev, board, t, adapter = setup(cfg, git_repo, tmp_path, {"verdict": "escalate", "summary": "task conflicts with contracts"})
    assert rev.process(t).status is Status.BLOCKED
    assert "conflicts" in board.list_questions()[0].text
```

- [ ] **Step 3: Run to verify it fails**

Run: `pytest tests/test_reviewer.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 4: Implement reviewer.py**

```python
# swarm/reviewer.py
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .adapters import get_adapter
from .adapters.base import RunSpec
from .board.base import Board
from .config import Config
from .models import Question, Status, Task
from .prompt import PROMPTS_DIR
from .report import REVIEW_SCHEMA
from .runner import STRUCTURED_PROVIDERS
from .workspace import Workspace

DIFF_CAP = 60000
REVIEW_TURNS = 25
REVIEW_TIMEOUT_S = 15 * 60


@dataclass
class Verdict:
    verdict: str
    summary: str = ""
    findings: list[dict] = field(default_factory=list)


def parse_verdict(structured: dict | None, worktree: Path) -> Verdict:
    data = structured if isinstance(structured, dict) and "verdict" in structured else None
    if data is None:
        f = Path(worktree) / ".swarm-run" / "review.json"
        if f.exists():
            try:
                loaded = json.loads(f.read_text())
                data = loaded if isinstance(loaded, dict) and "verdict" in loaded else None
            except ValueError:
                data = None
    if data is None or data.get("verdict") not in ("approve", "request_changes", "escalate"):
        return Verdict("escalate", "reviewer produced no usable verdict", [])
    return Verdict(data["verdict"], str(data.get("summary") or ""),
                   [f for f in (data.get("findings") or []) if isinstance(f, dict)])


def build_review_prompt(task: Task, diff: str, verify_tail: str, instructions: str) -> str:
    parts = [f"# Review of {task.id} · {task.title}", "", instructions.strip(), "", "## Task", "",
             f"- Type: {task.type} · Importance: {task.importance} · Scope: {', '.join(task.scope) or 'any'}",
             f"- Flags from the harness: {', '.join(task.flags) or 'none'}", "", "### Description", "",
             task.description.strip() or "(none)", "", "### Acceptance criteria", "",
             task.acceptance.strip() or "(none given)", "", "## Verify output (full suite)", "", "```",
             verify_tail.strip() or "(no verify script)", "```", "", "## Diff against main", "", "```diff",
             diff[:DIFF_CAP] + ("\n[diff truncated]" if len(diff) > DIFF_CAP else ""), "```", "",
             "## Verdict contract", "", "Your final answer MUST be a JSON object matching:", "", "```json",
             json.dumps(REVIEW_SCHEMA, indent=1), "```", "",
             "If your CLI cannot return structured output, write it to `.swarm-run/review.json`."]
    return "\n".join(parts)


def findings_to_feedback(v: Verdict) -> str:
    lines = [f"Reviewer requested changes: {v.summary}".strip()]
    for f in v.findings:
        loc = f.get("file", "")
        if f.get("line"):
            loc += f":{f['line']}"
        lines.append(f"- [{f.get('severity', '?')}] {loc}: {f.get('issue', '')} → {f.get('fix', '')}")
    return "\n".join(lines)[:1900]


class Reviewer:
    def __init__(self, cfg: Config, board: Board, ws: Workspace, *, adapter_factory=get_adapter, log=print,
                 prompt_text: str | None = None):
        self.cfg, self.board, self.ws, self.adapter_factory, self.log = cfg, board, ws, adapter_factory, log
        self.prompt_text = prompt_text if prompt_text is not None else (PROMPTS_DIR / "reviewer.md").read_text()

    def review(self, task: Task) -> Verdict:
        wt = self.ws.provision(task.id, reuse_branch=True)
        try:
            self.ws.run_script(wt, self.cfg.verify.setup_worktree, 600)
            full = self.ws.run_script(wt, self.cfg.verify.full, 1800)
            if full is not None and not full.ok:
                return Verdict("request_changes", "verify_full.sh failed",
                               [{"severity": "high", "file": self.cfg.verify.full or "", "issue": full.tail(1500), "fix": "make the full verify pass"}])
            role = self.cfg.reviewer
            if role is None:
                return Verdict("approve", "no reviewer configured; verify passed", [])
            agent_cfg = self.cfg.agents[role.agent]
            diff = self.ws.git(wt, "diff", f"origin/{self.cfg.main_branch}...HEAD", check=False).out
            prompt = build_review_prompt(task, diff, full.tail(1500) if full else "", self.prompt_text)
            pf = wt / ".swarm-run" / "review_prompt.md"
            pf.write_text(prompt)
            structured = agent_cfg.provider in STRUCTURED_PROVIDERS
            spec = RunSpec(prompt_file=pf, model=role.model or agent_cfg.models["mid"], effort=role.effort,
                           max_turns=REVIEW_TURNS, budget_usd=None, timeout_s=REVIEW_TIMEOUT_S, cwd=wt,
                           schema=REVIEW_SCHEMA if structured else None, read_only=True,
                           sandbox="read-only", extra_args=list(agent_cfg.extra_args))
            result = self.adapter_factory(agent_cfg).run(spec)
            if not result.ok and result.structured_output is None:
                return Verdict("escalate", f"reviewer run failed: {result.error[:300]}", [])
            return parse_verdict(result.structured_output, wt)
        finally:
            self.ws.dispose(wt)

    def apply(self, task: Task, v: Verdict) -> Task:
        round_no = task.review_rounds + 1
        md = f"**Verdict:** `{v.verdict}`\n\n{v.summary}\n\n" + "\n".join(
            f"- [{f.get('severity', '?')}] {f.get('file', '')}:{f.get('line', '')} {f.get('issue', '')} → {f.get('fix', '')}"
            for f in v.findings)
        self.board.append_task_report(task, f"Review — round {round_no}", md)
        if v.verdict == "request_changes" and round_no > self.cfg.max_review_rounds:
            v = Verdict("escalate", f"{round_no - 1} review rounds exhausted: {v.summary}", v.findings)
        if v.verdict == "approve":
            task.status, task.feedback = Status.MERGE_READY, ""
        elif v.verdict == "request_changes":
            task.status, task.review_rounds = Status.CHANGES_REQUESTED, round_no
            task.feedback = findings_to_feedback(v)
            task.flags = list(dict.fromkeys(task.flags + ["resume"]))
        else:
            task.status = Status.BLOCKED
            self.board.create_question(Question(
                id="", text=f"Reviewer escalated {task.id}: {v.summary}"[:190], kind="blocking",
                context=findings_to_feedback(v), options=["cut", "split", "accept as is", "human fix"],
                impact="high", task_id=task.id, asked_by=self.cfg.reviewer.agent if self.cfg.reviewer else "reviewer"))
        self.board.update_task(task, ["status", "feedback", "review_rounds", "flags"])
        self.log(f"[{task.id}] review → {v.verdict}")
        return task

    def process(self, task: Task) -> Task:
        return self.apply(task, self.review(task))
```

- [ ] **Step 5: Run tests**

Run: `pytest tests/test_reviewer.py -v`
Expected: 6 passed

- [ ] **Step 6: Commit**

```bash
git add swarm/reviewer.py prompts/reviewer.md tests/test_reviewer.py
git commit -m "feat: independent reviewer with verify-first gate and bounded rounds"
```

---

### Task 14: Merger

**Files:**
- Create: `swarm/merge.py`
- Test: `tests/test_merge.py`

**Interfaces:**
- Consumes: `Workspace`, `Board`.
- Produces: `Merger(cfg, board, ws, *, log=print)` with `merge(task) -> bool`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_merge.py
import subprocess
from swarm.board.memory import InMemoryBoard
from swarm.merge import Merger
from swarm.models import Task, Status
from swarm.workspace import Workspace, CmdResult


def _git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True).stdout


def prep(cfg, git_repo, tmp_path, gh_ok=True, verify_ok=True):
    calls = []

    def gh(args, cwd):
        calls.append(args)
        if args[:2] == ["pr", "merge"]:
            if gh_ok:
                # emulate GitHub squash-merge by merging the branch into main on the remote
                _git(git_repo, "fetch", "-q", "origin")
                _git(git_repo, "merge", "-q", "--no-edit", f"origin/{args[2]}")
                _git(git_repo, "push", "-q", "origin", "main")
                return CmdResult(0, "merged", "")
            return CmdResult(1, "", "not mergeable")
        return CmdResult(0, "", "")

    board = InMemoryBoard()
    ws = Workspace(git_repo, tmp_path / "wt", gh=gh)
    t = board.create_task(Task(id="", title="Feature", status=Status.MERGE_READY, agent="codex-a"))
    dep = board.create_task(Task(id="", title="Dependent", status=Status.BACKLOG, depends_on=[t.id]))
    wt = ws.provision(t.id)
    (wt / "feature.txt").write_text("f\n")
    if not verify_ok:
        (wt / "scripts" / "verify_fast.sh").write_text("#!/bin/sh\necho FASTFAIL; exit 1\n")
    ws.commit_all(wt, "feature")
    ws.push(wt, t.branch)
    ws.dispose(wt)
    return Merger(cfg, board, ws, log=lambda *a: None), board, t, dep, calls


def test_merge_success_marks_done(cfg, git_repo, tmp_path):
    m, board, t, dep, calls = prep(cfg, git_repo, tmp_path)
    assert m.merge(t) is True
    assert board.get_task(t.id).status is Status.DONE
    assert any(a[:2] == ["pr", "merge"] for a in calls)
    assert board.reports[t.id][-1][0] == "Merged"


def test_rebase_conflict_goes_back(cfg, git_repo, tmp_path):
    m, board, t, dep, calls = prep(cfg, git_repo, tmp_path)
    (git_repo / "feature.txt").write_text("conflict\n")
    _git(git_repo, "add", "feature.txt")
    _git(git_repo, "commit", "-qm", "main feature")
    _git(git_repo, "push", "-q", "origin", "main")
    assert m.merge(t) is False
    stored = board.get_task(t.id)
    assert stored.status is Status.CHANGES_REQUESTED and "feature.txt" in stored.feedback and "resume" in stored.flags


def test_verify_failure_after_rebase_goes_back(cfg, git_repo, tmp_path):
    m, board, t, dep, calls = prep(cfg, git_repo, tmp_path, verify_ok=False)
    assert m.merge(t) is False
    stored = board.get_task(t.id)
    assert stored.status is Status.CHANGES_REQUESTED and "FASTFAIL" in stored.feedback


def test_gh_merge_failure_flags(cfg, git_repo, tmp_path):
    m, board, t, dep, calls = prep(cfg, git_repo, tmp_path, gh_ok=False)
    assert m.merge(t) is False
    stored = board.get_task(t.id)
    assert stored.status is Status.MERGE_READY and "merge_failed" in stored.flags and "not mergeable" in stored.last_error
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_merge.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement merge.py**

```python
# swarm/merge.py
from __future__ import annotations

from .board.base import Board
from .config import Config
from .models import Status, Task, utcnow
from .workspace import Workspace


class Merger:
    def __init__(self, cfg: Config, board: Board, ws: Workspace, *, log=print):
        self.cfg, self.board, self.ws, self.log = cfg, board, ws, log

    def _back(self, task: Task, feedback: str) -> bool:
        task.status = Status.CHANGES_REQUESTED
        task.feedback = feedback[:1900]
        task.flags = list(dict.fromkeys(task.flags + ["resume"]))
        self.board.update_task(task, ["status", "feedback", "flags"])
        self.log(f"[{task.id}] merge → changes requested")
        return False

    def merge(self, task: Task) -> bool:
        if "merge_failed" in task.flags:
            return False
        wt = self.ws.provision(task.id, reuse_branch=True)
        try:
            ok, conflicts = self.ws.rebase_onto_main(wt)
            if not ok:
                return self._back(task, "Rebase onto main conflicted. Resolve conflicts in: " + ", ".join(conflicts)
                                  + ". Run `git fetch origin && git rebase origin/" + self.cfg.main_branch
                                  + "`, resolve, then `git rebase --continue`.")
            self.ws.run_script(wt, self.cfg.verify.setup_worktree, 600)
            verify = self.ws.run_script(wt, self.cfg.verify.fast, 900)
            if verify is not None and not verify.ok:
                return self._back(task, "verify_fast.sh failed after rebase onto main:\n" + verify.tail(1500))
            push = self.ws.push(wt, task.branch, force_with_lease=True)
            if not push.ok:
                task.last_error = ("push failed: " + push.err.strip())[:1900]
                self.board.update_task(task, ["last_error"])
                return False
            r = self.ws.pr_merge(task.branch)
            if not r.ok:
                task.last_error = ("gh pr merge failed: " + (r.err.strip() or r.out.strip()))[:1900]
                task.flags = list(dict.fromkeys(task.flags + ["merge_failed"]))
                self.board.update_task(task, ["last_error", "flags"])
                self.log(f"[{task.id}] gh merge failed: {task.last_error}")
                return False
            task.status, task.claim_nonce = Status.DONE, ""
            self.board.update_task(task, ["status", "claim_nonce"])
            self.board.append_task_report(task, "Merged", f"Squash-merged into {self.cfg.main_branch} at "
                                          f"{utcnow().isoformat(timespec='minutes')}. PR: {task.pr_url or '(none)'}")
            self.log(f"[{task.id}] merged")
            return True
        finally:
            self.ws.dispose(wt)
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_merge.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add swarm/merge.py tests/test_merge.py
git commit -m "feat: merger with rebase, verify, force-with-lease push, squash merge"
```

---

### Task 15: Status page and the Server (control loop)

**Files:**
- Create: `swarm/status.py`, `swarm/serve.py`
- Modify: `swarm/router.py` (add `context_from_board(board, cfg, now) -> RouteContext`)
- Test: `tests/test_status.py`, `tests/test_serve.py`

**Interfaces:**
- Produces:
  - `status.render_status(cfg, tasks, agents, questions, now) -> str`.
  - `router.context_from_board(board, cfg, now) -> RouteContext` (rows from `list_agents`, queue depth and scopes from Ready+Running tasks).
  - `serve.Server(cfg, board, ws, *, reviewer, merger, now=utcnow, sleep=time.sleep, log=print, host="serve", status_every_s=900, review_batch=1)` with `acquire_lock() -> bool`, `heartbeat_lock()`, `reap() -> int`, `retry_failed() -> int`, `relay() -> int`, `promote() -> int`, `review_pending() -> int`, `merge_pending() -> int`, `reroute() -> int`, `write_status(force=False) -> str | None`, `tick() -> dict`, `loop(stop=lambda: False)`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_status.py
from datetime import timedelta
from swarm.status import render_status
from swarm.models import Task, Status, AgentRow, Question, utcnow


def test_render_status_lines(cfg):
    now = cfg.event_end - timedelta(hours=8, minutes=48)
    tasks = [Task(id="T-1", title="a", milestone="M1", status=Status.DONE),
             Task(id="T-2", title="b", milestone="M2", status=Status.RUNNING, agent="claude-a"),
             Task(id="T-3", title="c", milestone="M2", status=Status.BLOCKED),
             Task(id="T-4", title="d", milestone="M2", status=Status.FAILED, attempts=3, last_error="boom"),
             Task(id="T-5", title="e", milestone="M2", status=Status.READY)]
    agents = [AgentRow(name="claude-a", status="running", current_task="T-2", cost_5h_usd=31.2),
              AgentRow(name="codex-a", status="cooldown", cooldown_until=now + timedelta(minutes=9))]
    qs = [Question(id="Q-4", text="Tap or rail?", task_id="T-3", status="Open", impact="high")]
    text = render_status(cfg, tasks, agents, qs, now)
    assert "8.8 h left" in text
    assert "M1" in text and "1/1" in text
    assert "M2" in text and "0/4" in text and "1 running" in text and "1 blocked" in text
    assert "Q-4" in text and "Tap or rail?" in text and "T-4" in text
    assert "claude-a" in text and "$31.2" in text and "cooldown" in text
    assert "RISK" in text  # 3 open M2 tasks vs capacity is fine, but a blocked+failed pair flags risk
```

```python
# tests/test_serve.py
from datetime import timedelta
from swarm.board.memory import InMemoryBoard
from swarm.models import Task, Status, AgentRow, Question, utcnow
from swarm.serve import Server
from swarm.workspace import Workspace, CmdResult


class FakeReviewer:
    def __init__(self):
        self.seen = []

    def process(self, task):
        self.seen.append(task.id)
        task.status = Status.MERGE_READY
        return task


class FakeMerger:
    def __init__(self, board):
        self.board = board
        self.seen = []

    def merge(self, task):
        self.seen.append(task.id)
        task.status = Status.DONE
        self.board.update_task(task, ["status"])
        return True


def make(cfg, git_repo, tmp_path, now=None):
    board = InMemoryBoard()
    ws = Workspace(git_repo, tmp_path / "wt", gh=lambda a, c: CmdResult(0, "", ""))
    clock = {"now": now or utcnow()}
    srv = Server(cfg, board, ws, reviewer=FakeReviewer(), merger=FakeMerger(board), now=lambda: clock["now"],
                 sleep=lambda s: None, log=lambda *a: None)
    return srv, board, clock


def test_lock(cfg, git_repo, tmp_path):
    srv, board, clock = make(cfg, git_repo, tmp_path)
    assert srv.acquire_lock() is True
    srv2, _, _ = make(cfg, git_repo, tmp_path)
    srv2.board = board
    assert srv2.acquire_lock() is False
    clock["now"] += timedelta(minutes=30)
    srv2.now = lambda: clock["now"]
    assert srv2.acquire_lock() is True


def test_reap_stale_running(cfg, git_repo, tmp_path):
    srv, board, clock = make(cfg, git_repo, tmp_path)
    now = clock["now"]
    board.upsert_agent(AgentRow(name="claude-a", last_heartbeat=now - timedelta(minutes=20)))
    board.upsert_agent(AgentRow(name="codex-a", last_heartbeat=now - timedelta(minutes=1)))
    t1 = board.create_task(Task(id="", title="stale", status=Status.RUNNING, agent="claude-a"))
    t2 = board.create_task(Task(id="", title="fresh", status=Status.RUNNING, agent="codex-a"))
    t3 = board.create_task(Task(id="", title="noagentrow", status=Status.RUNNING, agent="fake-b"))
    assert srv.reap() == 2
    s1 = board.get_task(t1.id)
    assert s1.status is Status.READY and s1.attempts == 1 and "resume" in s1.flags and s1.claim_nonce == ""
    assert board.get_task(t2.id).status is Status.RUNNING
    assert board.get_task(t3.id).status is Status.READY
    assert board.get_agent("claude-a").status == "offline"


def test_retry_ladder(cfg, git_repo, tmp_path):
    srv, board, clock = make(cfg, git_repo, tmp_path)
    t = board.create_task(Task(id="", title="f", type="backend", importance="normal", status=Status.FAILED,
                               attempts=1, agent="codex-a", model="gpt-6-sol", last_error="boom"))
    assert srv.retry_failed() == 1
    s = board.get_task(t.id)
    assert s.status is Status.READY and s.importance == "normal" and "boom" in s.feedback
    s.status, s.attempts = Status.FAILED, 2
    board.update_task(s, ["status", "attempts"])
    srv.retry_failed()
    s2 = board.get_task(t.id)
    assert s2.status is Status.READY and s2.importance == "high" and s2.model == "gpt-6-astra"
    s2.status, s2.attempts = Status.FAILED, 3
    board.update_task(s2, ["status", "attempts"])
    srv.retry_failed()
    assert board.get_task(t.id).status is Status.BLOCKED
    assert board.list_questions(status="Open")[0].task_id == t.id


def test_relay_blocking_and_fyi(cfg, git_repo, tmp_path):
    srv, board, clock = make(cfg, git_repo, tmp_path)
    t = board.create_task(Task(id="", title="b", status=Status.BLOCKED, agent="claude-a", milestone="M2", scope=["src/**"]))
    q = board.create_question(Question(id="", text="which?", kind="blocking", task_id=t.id))
    q2 = board.create_question(Question(id="", text="chose 3s", kind="fyi", task_id=t.id, impact="high"))
    assert srv.relay() == 0  # nothing answered yet
    q.answer = "use tap"
    board.update_question(q, ["answer"])
    q2.answer = "make it 5s"
    q2.needs_follow_up = True
    board.update_question(q2, ["answer", "needs_follow_up"])
    assert srv.relay() == 2
    s = board.get_task(t.id)
    assert s.status is Status.READY and "use tap" in s.feedback and "resume" in s.flags
    assert all(x.status == "Applied" for x in board.list_questions())
    follow = [x for x in board.list_tasks() if x.id != t.id][0]
    assert follow.type == "bugfix" and follow.importance == "high" and "make it 5s" in follow.feedback
    assert follow.depends_on == [t.id] and follow.status is Status.BACKLOG and follow.agent is not None


def test_promote_dependents(cfg, git_repo, tmp_path):
    srv, board, clock = make(cfg, git_repo, tmp_path)
    a = board.create_task(Task(id="", title="a", status=Status.DONE))
    b = board.create_task(Task(id="", title="b", status=Status.BACKLOG, depends_on=[a.id], type="frontend", importance="high"))
    c = board.create_task(Task(id="", title="c", status=Status.BACKLOG, depends_on=[a.id, b.id]))
    assert srv.promote() == 1
    sb = board.get_task(b.id)
    assert sb.status is Status.READY and sb.agent == "claude-a" and sb.model == "opus"
    assert board.get_task(c.id).status is Status.BACKLOG


def test_review_and_merge_pending(cfg, git_repo, tmp_path):
    srv, board, clock = make(cfg, git_repo, tmp_path)
    r = board.create_task(Task(id="", title="r", status=Status.REVIEW, priority=2))
    m = board.create_task(Task(id="", title="m", status=Status.MERGE_READY, priority=1))
    d = board.create_task(Task(id="", title="d", status=Status.BACKLOG, depends_on=[m.id]))
    assert srv.review_pending() == 1 and srv.reviewer.seen == [r.id]
    assert srv.merge_pending() == 2  # m, then r (now merge ready)
    assert board.get_task(d.id).status is Status.READY


def test_reroute_unknown_agent_and_cooldown(cfg, git_repo, tmp_path):
    srv, board, clock = make(cfg, git_repo, tmp_path)
    now = clock["now"]
    board.upsert_agent(AgentRow(name="codex-a", status="cooldown", cooldown_until=now + timedelta(minutes=10)))
    ghost = board.create_task(Task(id="", title="g", status=Status.READY, agent="not-configured", type="docs"))
    cold = board.create_task(Task(id="", title="c", status=Status.READY, agent="codex-a", type="backend", importance="normal"))
    crit = board.create_task(Task(id="", title="k", status=Status.READY, agent="codex-a", type="backend", importance="critical"))
    assert srv.reroute() == 2
    assert board.get_task(ghost.id).agent == "claude-a"
    assert board.get_task(cold.id).agent == "claude-a"
    assert board.get_task(crit.id).agent == "codex-a"


def test_tick_writes_status(cfg, git_repo, tmp_path):
    srv, board, clock = make(cfg, git_repo, tmp_path)
    board.create_task(Task(id="", title="x", status=Status.READY, agent="claude-a", milestone="M1"))
    summary = srv.tick()
    assert set(summary) >= {"reaped", "retried", "relayed", "promoted", "reviewed", "merged", "rerouted"}
    assert "SWARM STATUS" in board.status_page
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_status.py tests/test_serve.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Add `context_from_board` to router.py**

Append to `swarm/router.py`:
```python
def context_from_board(board, cfg: Config, now: datetime | None = None) -> RouteContext:
    from .models import Status  # local import keeps module import order simple
    now = now or utcnow()
    rows = {a.name: a for a in board.list_agents()}
    depth: dict[str, int] = {}
    scopes: dict[str, list[str]] = {}
    for t in board.list_tasks(status=[Status.READY, Status.RUNNING, Status.CHANGES_REQUESTED]):
        if t.agent:
            depth[t.agent] = depth.get(t.agent, 0) + 1
            scopes.setdefault(t.agent, []).extend(t.scope)
    return RouteContext(rows=rows, queue_depth=depth, scopes_by_agent=scopes, now=now)
```

- [ ] **Step 4: Implement status.py**

```python
# swarm/status.py
from __future__ import annotations

from datetime import datetime

from .config import Config
from .models import AgentRow, Question, Status, Task

OPEN = {Status.BACKLOG, Status.READY, Status.RUNNING, Status.REVIEW, Status.CHANGES_REQUESTED,
        Status.MERGE_READY, Status.BLOCKED, Status.FAILED}
EST_HOURS_PER_TASK = 0.6


def render_status(cfg: Config, tasks: list[Task], agents: list[AgentRow], questions: list[Question],
                  now: datetime) -> str:
    hours_left = max(0.0, (cfg.event_end - now).total_seconds() / 3600)
    lines = [f"SWARM STATUS · {now.strftime('%Y-%m-%d %H:%M UTC')} · {hours_left:.1f} h left", ""]
    # agents
    parts = []
    for a in sorted(agents, key=lambda x: x.name):
        if a.name == "serve":
            continue
        bit = f"{a.name} {a.status}"
        if a.current_task:
            bit += f"({a.current_task})"
        if a.status == "cooldown" and a.cooldown_until:
            bit += f" until {a.cooldown_until.strftime('%H:%M')}"
        bit += f" ${a.cost_5h_usd:.1f}/5h"
        parts.append(bit)
    lines.append("Agents: " + (" · ".join(parts) or "(none yet)"))
    lines.append("")
    # milestones
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
    # needs human
    needs = [f"{q.id} ({q.task_id}) \"{q.text}\"" for q in questions if q.status == "Open"]
    needs += [f"{t.id} failed ×{t.attempts}: {t.last_error[:60]}" for t in tasks if t.status is Status.FAILED]
    needs += [f"{t.id} blocked" for t in tasks if t.status is Status.BLOCKED and not any(q.task_id == t.id for q in questions)]
    lines.append("Needs you: " + ("; ".join(needs) if needs else "nothing"))
    lines.append("")
    for r in risks:
        lines.append(f"RISK: {r}")
    if not risks:
        lines.append("RISK: none flagged")
    return "\n".join(lines)
```

- [ ] **Step 5: Implement serve.py**

```python
# swarm/serve.py
from __future__ import annotations

import time
from datetime import timedelta
from typing import Callable

from .board.base import Board
from .config import Config
from .models import AgentRow, Question, Status, Task, utcnow
from .router import context_from_board, escalate_importance, route
from .status import render_status
from .workspace import Workspace

IMPACT_TO_IMPORTANCE = {"high": "high", "medium": "normal", "low": "low"}


class Server:
    def __init__(self, cfg: Config, board: Board, ws: Workspace, *, reviewer, merger, now=utcnow,
                 sleep: Callable[[float], None] = time.sleep, log=print, host: str = "serve",
                 status_every_s: int = 900, review_batch: int = 1):
        self.cfg, self.board, self.ws, self.reviewer, self.merger = cfg, board, ws, reviewer, merger
        self.now, self.sleep, self.log, self.host = now, sleep, log, host
        self.status_every_s, self.review_batch = status_every_s, review_batch
        self._last_status = None

    # ----- lock -----
    def acquire_lock(self) -> bool:
        now = self.now()
        row = self.board.get_agent("serve")
        stale = timedelta(minutes=self.cfg.heartbeat_stale_minutes)
        if row and row.last_heartbeat and (now - row.last_heartbeat) < stale and row.host != self.host:
            self.log(f"another serve is alive on {row.host} (heartbeat {row.last_heartbeat})")
            return False
        self.board.upsert_agent(AgentRow(name="serve", provider="serve", host=self.host, status="running",
                                         last_heartbeat=now, page_id=row.page_id if row else ""))
        return True

    def heartbeat_lock(self) -> None:
        row = self.board.get_agent("serve") or AgentRow(name="serve", provider="serve")
        row.host, row.status, row.last_heartbeat = self.host, "running", self.now()
        self.board.upsert_agent(row)

    # ----- steps -----
    def reap(self) -> int:
        now = self.now()
        stale = timedelta(minutes=self.cfg.heartbeat_stale_minutes)
        rows = {a.name: a for a in self.board.list_agents()}
        n = 0
        for t in self.board.list_tasks(status=[Status.RUNNING]):
            row = rows.get(t.agent or "")
            if row and row.last_heartbeat and (now - row.last_heartbeat) < stale:
                continue
            t.status, t.attempts, t.claim_nonce = Status.READY, t.attempts + 1, ""
            t.flags = list(dict.fromkeys(t.flags + ["resume"]))
            t.last_error = "worker heartbeat stale; requeued"
            self.board.update_task(t, ["status", "attempts", "claim_nonce", "flags", "last_error"])
            self.log(f"[{t.id}] reaped from {t.agent}")
            n += 1
        for name, row in rows.items():
            if name == "serve":
                continue
            if row.last_heartbeat and (now - row.last_heartbeat) >= stale and row.status != "offline":
                row.status = "offline"
                self.board.upsert_agent(row)
        return n

    def retry_failed(self) -> int:
        n = 0
        ctx = context_from_board(self.board, self.cfg, self.now())
        for t in self.board.list_tasks(status=[Status.FAILED]):
            if t.attempts >= self.cfg.max_attempts:
                self.board.create_question(Question(
                    id="", text=f"{t.id} failed {t.attempts} times: {t.last_error[:120]}"[:190], kind="blocking",
                    context=t.last_error[:1900], options=["retry once more", "split", "cut", "human fix"],
                    impact="high", task_id=t.id, asked_by="serve"))
                t.status = Status.BLOCKED
                self.board.update_task(t, ["status"])
                self.log(f"[{t.id}] gave up after {t.attempts} attempts → blocked")
                n += 1
                continue
            if t.attempts >= 2:
                t.importance = escalate_importance(t.importance)
            t.agent, t.model, t.effort = route(t, self.cfg, ctx)
            t.status, t.claim_nonce = Status.READY, ""
            t.feedback = f"Previous attempt failed: {t.last_error[:600]}. Start fresh from main." if t.last_error else ""
            t.flags = [f for f in t.flags if f != "resume"]
            self.board.update_task(t, ["status", "claim_nonce", "importance", "agent", "model", "effort", "feedback", "flags"])
            self.log(f"[{t.id}] retry #{t.attempts + 1} on {t.agent}/{t.model}")
            n += 1
        return n

    def relay(self) -> int:
        n = 0
        ctx = None
        for q in self.board.list_questions(status="Open"):
            if not q.answer.strip():
                continue
            t = self.board.get_task(q.task_id) if q.task_id else None
            if q.kind == "blocking":
                if t and t.status is Status.BLOCKED:
                    t.feedback = f"Human answer to \"{q.text}\": {q.answer}"[:1900]
                    t.flags = list(dict.fromkeys(t.flags + ["resume"]))
                    t.status, t.claim_nonce = Status.READY, ""
                    self.board.update_task(t, ["feedback", "flags", "status", "claim_nonce"])
                    self.log(f"[{t.id}] unblocked by {q.id}")
            elif q.needs_follow_up:
                ctx = ctx or context_from_board(self.board, self.cfg, self.now())
                follow = Task(id="", title=f"Follow-up: {q.text[:70]}", description=f"Human answer to \"{q.text}\": {q.answer}\n\nContext: {q.context}",
                              acceptance="- the human's answer is implemented\n- verify passes", type="bugfix",
                              importance=IMPACT_TO_IMPORTANCE.get(q.impact, "normal"), size="S",
                              milestone=t.milestone if t else "", scope=list(t.scope) if t else [],
                              depends_on=[t.id] if t and t.status is not Status.DONE else [], feedback=q.answer[:1900])
                follow.status = Status.BACKLOG if follow.depends_on else Status.READY
                follow.agent, follow.model, follow.effort = route(follow, self.cfg, ctx)
                created = self.board.create_task(follow)
                self.log(f"[{created.id}] follow-up created from {q.id}")
            q.status = "Applied"
            self.board.update_question(q, ["status"])
            n += 1
        return n

    def promote(self) -> int:
        done = {t.id for t in self.board.list_tasks(status=[Status.DONE])}
        ctx = None
        n = 0
        for t in self.board.list_tasks(status=[Status.BACKLOG]):
            if t.depends_on and not all(d in done for d in t.depends_on):
                continue
            ctx = ctx or context_from_board(self.board, self.cfg, self.now())
            t.agent, t.model, t.effort = route(t, self.cfg, ctx)
            t.status = Status.READY
            self.board.update_task(t, ["status", "agent", "model", "effort"])
            ctx.queue_depth[t.agent] = ctx.queue_depth.get(t.agent, 0) + 1
            self.log(f"[{t.id}] promoted → {t.agent}/{t.model}")
            n += 1
        return n

    def review_pending(self) -> int:
        n = 0
        for t in self.board.list_tasks(status=[Status.REVIEW])[: self.review_batch]:
            self.reviewer.process(t)
            n += 1
        return n

    def merge_pending(self) -> int:
        n = 0
        for t in self.board.list_tasks(status=[Status.MERGE_READY]):
            if self.merger.merge(t):
                n += 1
                self.promote()
        return n

    def reroute(self) -> int:
        now = self.now()
        ctx = context_from_board(self.board, self.cfg, now)
        n = 0
        for t in self.board.list_tasks(status=[Status.READY]):
            row = ctx.rows.get(t.agent or "")
            unknown = t.agent not in self.cfg.agents
            cooling = bool(row and row.cooldown_until and row.cooldown_until > now)
            offline = bool(row and row.status == "offline")
            if not unknown and not ((cooling or offline) and t.importance != "critical"):
                continue
            agent, model, effort = route(t, self.cfg, ctx)
            if agent == t.agent:
                continue
            t.agent, t.model, t.effort = agent, model, effort
            self.board.update_task(t, ["agent", "model", "effort"])
            self.log(f"[{t.id}] rerouted → {agent}/{model}")
            n += 1
        return n

    def write_status(self, force: bool = False) -> str | None:
        now = self.now()
        if not force and self._last_status and (now - self._last_status).total_seconds() < self.status_every_s:
            return None
        self._last_status = now
        text = render_status(self.cfg, self.board.list_tasks(), self.board.list_agents(),
                             self.board.list_questions(), now)
        self.board.write_status_page(text)
        return text

    def tick(self) -> dict:
        self.heartbeat_lock()
        summary = {"reaped": self.reap(), "retried": self.retry_failed(), "relayed": self.relay(),
                   "promoted": self.promote(), "reviewed": self.review_pending(), "merged": self.merge_pending(),
                   "rerouted": self.reroute()}
        self.write_status()
        return summary

    def loop(self, stop: Callable[[], bool] = lambda: False) -> None:
        if not self.acquire_lock():
            raise SystemExit("another swarm serve is running; stop it first or wait for its heartbeat to go stale")
        self.write_status(force=True)
        while not stop():
            try:
                s = self.tick()
                if any(s.values()):
                    self.log(" · ".join(f"{k} {v}" for k, v in s.items() if v))
            except Exception as e:  # noqa: BLE001 - keep serving
                self.log(f"serve tick failed: {e!r}")
            self.sleep(self.cfg.serve_seconds)
```

- [ ] **Step 6: Run tests**

Run: `pytest tests/test_status.py tests/test_serve.py -v`
Expected: 9 passed

- [ ] **Step 7: Commit**

```bash
git add swarm/status.py swarm/serve.py swarm/router.py tests/test_status.py tests/test_serve.py
git commit -m "feat: control loop with reaper, retry ladder, relay, promotion, review, merge, reroute, status page"
```

---

### Task 16: Planner (`swarm plan`, `swarm split`)

**Files:**
- Create: `swarm/planner.py`, `prompts/planner.md`
- Test: `tests/test_planner.py`

**Interfaces:**
- Produces: `TASKS_SCHEMA`; `build_plan_prompt(plan_md, docs: dict[str, str], existing: list[Task], milestone: str | None, instructions: str, split_of: Task | None = None) -> str`; `parse_proposals(structured, worktree) -> list[dict]`; `lint_conflicts(proposals) -> list[str]`; `Planner(cfg, board, ws, *, adapter_factory=get_adapter, log=print, prompt_text=None)` with `propose(plan_path: Path, milestone: str | None = None, split_of: Task | None = None) -> list[dict]` and `apply(proposals: list[dict]) -> list[Task]`.
- A proposal's `depends_on` may reference an existing task id (`T-003`) or the exact `title` of another proposal in the same batch.

- [ ] **Step 1: Write prompts/planner.md**

```markdown
# Planner instructions

You turn PLAN.md into small, independent, testable tasks for autonomous coding agents that run in parallel on separate branches. You do not write code.

Rules for good tasks:
- 20 to 60 minutes of work for one agent. Size S ≈ 20 min, M ≈ 40, L ≈ 75. Prefer S and M.
- One clear deliverable with observable acceptance criteria (what a test or a screenshot proves), not "make it nice".
- A `scope` of file globs the task may edit. Two tasks that would edit the same files must not both be open at once: give one a dependency on the other.
- Contracts first: tasks that define API shapes, event schemas, or design tokens come before the tasks that consume them, and consumers depend on them.
- Every milestone must end with something that runs end to end. The first milestone is a thin vertical slice.
- Types: frontend, backend, realtime, ml_audio, ml_vision, ml_fusion, eval, tests, docs, research, bugfix, integration, infra. Importance: critical only for what the demo cannot live without.
- Do not repeat tasks that already exist (they are listed). Plan the requested milestone in detail and later milestones only as a few coarse L tasks.

Your final answer is the JSON object described at the end. If your CLI cannot return structured output, write it to `.swarm-run/plan.json`.
```

- [ ] **Step 2: Write the failing tests**

```python
# tests/test_planner.py
import json
from pathlib import Path
from swarm.board.memory import InMemoryBoard
from swarm.models import Task, Status, RunResult, Usage
from swarm.planner import TASKS_SCHEMA, build_plan_prompt, parse_proposals, lint_conflicts, Planner
from swarm.workspace import Workspace, CmdResult


PROPOSALS = [
    {"title": "Define audio event contract", "description": "d1", "acceptance": "- schema in docs/CONTRACTS.md",
     "type": "backend", "importance": "critical", "size": "S", "milestone": "M1", "scope": ["docs/CONTRACTS.md"]},
    {"title": "WebSocket audio stream", "description": "d2", "acceptance": "- frames arrive", "type": "realtime",
     "importance": "high", "size": "M", "milestone": "M1", "scope": ["backend/**"],
     "depends_on": ["Define audio event contract"]},
    {"title": "Speaker card UI", "description": "d3", "acceptance": "- renders", "type": "frontend",
     "importance": "high", "size": "M", "milestone": "M1", "scope": ["frontend/src/**"], "depends_on": ["T-001"]},
    {"title": "Also edits backend", "description": "d4", "acceptance": "- x", "type": "backend",
     "importance": "low", "size": "S", "milestone": "M1", "scope": ["backend/api/**"]},
]


def test_schema_and_parse(tmp_path):
    assert TASKS_SCHEMA["properties"]["tasks"]["items"]["required"][:2] == ["title", "description"]
    assert parse_proposals({"tasks": PROPOSALS}, tmp_path)[0]["title"] == "Define audio event contract"
    (tmp_path / ".swarm-run").mkdir()
    (tmp_path / ".swarm-run" / "plan.json").write_text(json.dumps({"tasks": PROPOSALS[:1]}))
    assert len(parse_proposals(None, tmp_path)) == 1
    assert parse_proposals({"tasks": [{"title": "no type"}]}, tmp_path)[0]["type"] == "backend"  # defaults filled
    assert parse_proposals({"nope": 1}, tmp_path / "x") == []


def test_lint_conflicts():
    warnings = lint_conflicts(PROPOSALS)
    assert any("WebSocket audio stream" in w and "Also edits backend" in w for w in warnings)
    assert not any("Define audio event contract" in w and "Speaker card UI" in w for w in warnings)


def test_prompt_pieces():
    p = build_plan_prompt("# Plan\nbuild it", {"docs/DESIGN.md": "tokens"}, [Task(id="T-001", title="Existing")], "M1", "PLANNER RULES")
    assert "PLANNER RULES" in p and "build it" in p and "tokens" in p and "T-001" in p and "M1" in p and '"tasks"' in p
    p2 = build_plan_prompt("x", {}, [], None, "R", split_of=Task(id="T-009", title="Big", description="big desc"))
    assert "Split" in p2 and "T-009" in p2 and "big desc" in p2


def test_apply_resolves_deps_and_routes(cfg, git_repo, tmp_path):
    board = InMemoryBoard()
    board.create_task(Task(id="T-001", title="pre-existing", status=Status.DONE))
    ws = Workspace(git_repo, tmp_path / "wt", gh=lambda a, c: CmdResult(0, "", ""))
    pl = Planner(cfg, board, ws, log=lambda *a: None, prompt_text="R")
    created = pl.apply(PROPOSALS)
    ids = [t.id for t in created]
    assert ids == ["T-002", "T-003", "T-004", "T-005"]
    ws_task = board.get_task("T-003")
    assert ws_task.depends_on == ["T-002"] and ws_task.status is Status.BACKLOG
    ui = board.get_task("T-004")
    assert ui.depends_on == ["T-001"] and ui.status is Status.READY and ui.agent == "claude-a" and ui.model == "opus"
    assert board.get_task("T-002").status is Status.READY and board.get_task("T-002").agent == "codex-a"


def test_propose_runs_adapter(cfg, git_repo, tmp_path):
    class FakePlannerAdapter:
        def __init__(self):
            self.specs = []

        def run(self, spec):
            self.specs.append(spec)
            return RunResult(ok=True, exit_code=0, stdout="", stderr="", structured_output={"tasks": PROPOSALS[:2]}, usage=Usage())

    board = InMemoryBoard()
    ws = Workspace(git_repo, tmp_path / "wt", gh=lambda a, c: CmdResult(0, "", ""))
    adapter = FakePlannerAdapter()
    pl = Planner(cfg, board, ws, adapter_factory=lambda a: adapter, log=lambda *a: None, prompt_text="R")
    (git_repo / "PLAN.md").write_text("# P\n\n## Summary\nthe plan\n")
    props = pl.propose(git_repo / "PLAN.md", milestone="M1")
    assert len(props) == 2 and adapter.specs[0].read_only is True and adapter.specs[0].model == "opus"
    assert "the plan" in adapter.specs[0].prompt_file.read_text()
    assert (cfg.repo_root / ".swarm" / "tasks.proposed.json").exists()
```

- [ ] **Step 3: Run to verify it fails**

Run: `pytest tests/test_planner.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 4: Implement planner.py**

```python
# swarm/planner.py
from __future__ import annotations

import json
from pathlib import Path

from .adapters import get_adapter
from .adapters.base import RunSpec
from .board.base import Board
from .config import Config
from .models import IMPORTANCES, SIZES, TASK_TYPES, Status, Task
from .prompt import PROMPTS_DIR, read_doc
from .router import context_from_board, route, scopes_overlap
from .runner import STRUCTURED_PROVIDERS
from .workspace import Workspace

TASKS_SCHEMA = {
    "type": "object", "required": ["tasks"], "additionalProperties": False,
    "properties": {"tasks": {"type": "array", "items": {
        "type": "object",
        "required": ["title", "description", "acceptance", "type", "importance", "size", "milestone"],
        "additionalProperties": False,
        "properties": {
            "title": {"type": "string"}, "description": {"type": "string"}, "acceptance": {"type": "string"},
            "type": {"type": "string", "enum": TASK_TYPES}, "importance": {"type": "string", "enum": IMPORTANCES},
            "size": {"type": "string", "enum": SIZES}, "milestone": {"type": "string"},
            "priority": {"type": "integer"}, "depends_on": {"type": "array", "items": {"type": "string"}},
            "scope": {"type": "array", "items": {"type": "string"}},
        }}}},
}
PLAN_DOCS = ["docs/ARCHITECTURE.md", "docs/DESIGN.md", "docs/CONTRACTS.md"]
PLAN_TURNS = 40
PLAN_TIMEOUT_S = 20 * 60


def build_plan_prompt(plan_md: str, docs: dict[str, str], existing: list[Task], milestone: str | None,
                      instructions: str, split_of: Task | None = None) -> str:
    parts = ["# Planning request", "", instructions.strip(), ""]
    if split_of:
        parts += [f"## Split this task into 2-5 smaller tasks: {split_of.id} · {split_of.title}", "",
                  split_of.description or "(no description)", "", "Acceptance of the original:", "",
                  split_of.acceptance or "(none)", ""]
    else:
        parts += [f"## Milestone to plan in detail: {milestone or 'the next one'}", ""]
    parts += ["## PLAN.md", "", plan_md.strip(), ""]
    for ref, text in docs.items():
        parts += [f"## {ref}", "", text.strip(), ""]
    parts += ["## Existing tasks (do not duplicate; you may depend on them by ID)", ""]
    parts += [f"- {t.id} [{t.status.value}] {t.title} (type={t.type}, milestone={t.milestone or '-'})" for t in existing] or ["(none)"]
    parts += ["", "## Output contract", "", "Your final answer MUST be a JSON object matching:", "", "```json",
              json.dumps(TASKS_SCHEMA, indent=1), "```", "",
              "`depends_on` entries are existing task IDs or the exact title of another task in your list."]
    return "\n".join(parts)


def parse_proposals(structured: dict | None, worktree: Path) -> list[dict]:
    data = structured if isinstance(structured, dict) and isinstance(structured.get("tasks"), list) else None
    if data is None:
        f = Path(worktree) / ".swarm-run" / "plan.json"
        if f.exists():
            try:
                loaded = json.loads(f.read_text())
                data = loaded if isinstance(loaded, dict) and isinstance(loaded.get("tasks"), list) else None
            except ValueError:
                data = None
    if data is None:
        return []
    out = []
    for raw in data["tasks"]:
        if not isinstance(raw, dict) or not raw.get("title"):
            continue
        out.append({
            "title": str(raw["title"]).strip()[:150], "description": str(raw.get("description") or ""),
            "acceptance": str(raw.get("acceptance") or ""),
            "type": raw.get("type") if raw.get("type") in TASK_TYPES else "backend",
            "importance": raw.get("importance") if raw.get("importance") in IMPORTANCES else "normal",
            "size": raw.get("size") if raw.get("size") in SIZES else "M",
            "milestone": str(raw.get("milestone") or ""), "priority": int(raw.get("priority") or 100),
            "depends_on": [str(d) for d in (raw.get("depends_on") or [])],
            "scope": [str(s) for s in (raw.get("scope") or [])],
        })
    return out


def lint_conflicts(proposals: list[dict]) -> list[str]:
    warnings = []
    for i, a in enumerate(proposals):
        for b in proposals[i + 1:]:
            if not a.get("scope") or not b.get("scope"):
                continue
            if scopes_overlap(a["scope"], b["scope"]):
                linked = a["title"] in b.get("depends_on", []) or b["title"] in a.get("depends_on", [])
                if not linked:
                    warnings.append(f"scope overlap without dependency: '{a['title']}' and '{b['title']}'")
    return warnings


class Planner:
    def __init__(self, cfg: Config, board: Board, ws: Workspace, *, adapter_factory=get_adapter, log=print,
                 prompt_text: str | None = None):
        self.cfg, self.board, self.ws, self.adapter_factory, self.log = cfg, board, ws, adapter_factory, log
        self.prompt_text = prompt_text if prompt_text is not None else (PROMPTS_DIR / "planner.md").read_text()

    def propose(self, plan_path: Path, milestone: str | None = None, split_of: Task | None = None) -> list[dict]:
        role = self.cfg.planner
        if role is None:
            raise RuntimeError("no planner configured in .swarm/config.yaml")
        agent_cfg = self.cfg.agents[role.agent]
        plan_md = Path(plan_path).read_text()
        docs = {ref: text for ref in PLAN_DOCS if (text := read_doc(self.cfg.repo_root, ref))}
        existing = [t for t in self.board.list_tasks() if t.status is not Status.CUT]
        prompt = build_plan_prompt(plan_md, docs, existing, milestone, self.prompt_text, split_of=split_of)
        wt = self.ws.main_worktree()
        (wt / ".swarm-run").mkdir(exist_ok=True)
        pf = wt / ".swarm-run" / "plan_prompt.md"
        pf.write_text(prompt)
        structured = agent_cfg.provider in STRUCTURED_PROVIDERS
        spec = RunSpec(prompt_file=pf, model=role.model or agent_cfg.models["high"], effort=role.effort,
                       max_turns=PLAN_TURNS, budget_usd=None, timeout_s=PLAN_TIMEOUT_S, cwd=wt,
                       schema=TASKS_SCHEMA if structured else None, read_only=True, sandbox="read-only",
                       extra_args=list(agent_cfg.extra_args))
        result = self.adapter_factory(agent_cfg).run(spec)
        if not result.ok and result.structured_output is None:
            raise RuntimeError(f"planner run failed: {result.error[:300]}")
        proposals = parse_proposals(result.structured_output, wt)
        out = self.cfg.repo_root / ".swarm" / "tasks.proposed.json"
        out.write_text(json.dumps(proposals, indent=1))
        for w in lint_conflicts(proposals):
            self.log("WARN " + w)
        return proposals

    def apply(self, proposals: list[dict]) -> list[Task]:
        start = int(self.board.next_task_id().split("-")[1])
        ids = [f"T-{start + i:03d}" for i in range(len(proposals))]
        by_title = {p["title"]: tid for p, tid in zip(proposals, ids)}
        done = {t.id for t in self.board.list_tasks(status=[Status.DONE])}
        ctx = context_from_board(self.board, self.cfg)
        created = []
        for p, tid in zip(proposals, ids):
            deps = [by_title.get(d, d) for d in p.get("depends_on", [])]
            deps = [d for d in deps if d != tid]
            task = Task(id=tid, title=p["title"], description=p["description"], acceptance=p["acceptance"],
                        type=p["type"], importance=p["importance"], size=p["size"], milestone=p["milestone"],
                        priority=p.get("priority", 100), depends_on=deps, scope=p.get("scope", []))
            ready = all(d in done for d in deps)
            task.status = Status.READY if ready else Status.BACKLOG
            task.agent, task.model, task.effort = route(task, self.cfg, ctx)
            ctx.queue_depth[task.agent] = ctx.queue_depth.get(task.agent, 0) + (1 if ready else 0)
            ctx.scopes_by_agent.setdefault(task.agent, []).extend(task.scope)
            created.append(self.board.create_task(task))
            self.log(f"[{tid}] {task.status.value} → {task.agent}/{task.model}: {task.title}")
        return created
```

- [ ] **Step 5: Run tests**

Run: `pytest tests/test_planner.py -v`
Expected: 5 passed

- [ ] **Step 6: Commit**

```bash
git add swarm/planner.py prompts/planner.md tests/test_planner.py
git commit -m "feat: planner with structured decomposition, dependency resolution, conflict lint"
```

---

### Task 17: Doctor and CLI

**Files:**
- Create: `swarm/doctor.py`, `swarm/cli.py`
- Test: `tests/test_cli.py`

**Interfaces:**
- Produces: `doctor.run_checks(cfg, host, *, offline=False, notion_token=None, which=shutil.which, run=run_cmd) -> list[Check]` where `Check(name, ok, detail)`; `doctor.smoke_agent(cfg, agent_name, tmp_dir) -> Check`.
- `cli.app` (typer) with commands: `doctor`, `init`, `agents-sync`, `plan`, `add`, `assign`, `cut`, `split`, `answer`, `status`, `reroute`, `run`, `serve`, `logs`, `template`. Global option `--config` (default: walk up from cwd to find `.swarm/config.yaml`). `--memory` uses `InMemoryBoard` (dry runs only). Host from `--host` or `SWARM_HOST`.
- `cli.make_board(cfg, memory=False) -> Board`; `cli.find_config(start: Path) -> Path`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_cli.py
import os
from pathlib import Path
from typer.testing import CliRunner
from swarm.cli import app, find_config
from swarm.doctor import run_checks, Check

runner = CliRunner()


def test_find_config_walks_up(project_dir):
    sub = project_dir / "frontend" / "src"
    sub.mkdir(parents=True)
    assert find_config(sub) == project_dir / ".swarm" / "config.yaml"


def test_help_lists_commands():
    r = runner.invoke(app, ["--help"])
    assert r.exit_code == 0
    for cmd in ("doctor", "init", "plan", "add", "assign", "answer", "status", "run", "serve", "template"):
        assert cmd in r.output


def test_doctor_offline_checks(cfg):
    checks = run_checks(cfg, "host-a", offline=True, which=lambda name: None if name == "codex" else f"/usr/bin/{name}",
                        run=lambda args, cwd, timeout=60: type("R", (), {"ok": True, "out": "v1", "err": ""})())
    names = {c.name: c for c in checks}
    assert names["config"].ok and names["host"].ok
    assert names["cli:claude-a (claude)"].ok and not names["cli:codex-a (codex)"].ok
    assert not names["notion token"].ok  # offline: reported as skipped/not ok without token


def test_add_dry_run_prints_route(project_dir):
    r = runner.invoke(app, ["--config", str(project_dir / ".swarm" / "config.yaml"), "--memory", "add", "Build cards",
                            "--type", "frontend", "--importance", "critical", "--size", "M"])
    assert r.exit_code == 0, r.output
    assert "claude-a" in r.output and "opus" in r.output and "T-001" in r.output


def test_status_memory(project_dir):
    r = runner.invoke(app, ["--config", str(project_dir / ".swarm" / "config.yaml"), "--memory", "status"])
    assert r.exit_code == 0 and "SWARM STATUS" in r.output


def test_template_copies(tmp_path):
    dest = tmp_path / "newproj"
    r = runner.invoke(app, ["template", str(dest)])
    assert r.exit_code == 0, r.output
    assert (dest / "AGENTS.md").exists() and (dest / ".swarm" / "config.yaml").exists()
    assert (dest / "scripts" / "verify_fast.sh").exists() and os.access(dest / "scripts" / "verify_fast.sh", os.X_OK)
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_cli.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement doctor.py**

```python
# swarm/doctor.py
from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

from .config import Config
from .workspace import run_cmd

CLI_BINARY = {"claude": "claude", "codex": "codex", "antigravity": "agy", "gemini": "gemini", "grok": "grok"}


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""


def run_checks(cfg: Config, host: str | None, *, offline: bool = False, notion_token: str | None = None,
               which=shutil.which, run=run_cmd) -> list[Check]:
    checks = [Check("python", sys.version_info >= (3, 11), sys.version.split()[0]),
              Check("config", True, str(cfg.path))]
    checks.append(Check("host", bool(host and host in cfg.hosts),
                        host or "set SWARM_HOST to one of: " + ", ".join(cfg.hosts)))
    token = notion_token if notion_token is not None else os.environ.get("NOTION_TOKEN")
    if not token:
        checks.append(Check("notion token", False, "NOTION_TOKEN not set"))
    elif offline:
        checks.append(Check("notion token", True, "present (offline: not verified)"))
    else:
        from .board.notion import NotionClient, NotionError
        try:
            me = NotionClient(token).me()
            checks.append(Check("notion token", True, f"connection ok as {me.get('name') or me.get('id')}"))
        except NotionError as e:
            checks.append(Check("notion token", False, str(e)))
    ids_ok = bool(cfg.notion.tasks_ds and cfg.notion.questions_ds and cfg.notion.agents_ds)
    checks.append(Check("notion ids", ids_ok, "run `swarm init --parent-page <id>`" if not ids_ok else "present"))
    checks.append(Check("git", bool(which("git")), which("git") or "missing"))
    gh = which("gh")
    if gh and not offline:
        r = run(["gh", "auth", "status"], cwd=cfg.repo_root, timeout=30)
        checks.append(Check("gh auth", r.ok, (r.out + r.err).strip().splitlines()[0] if (r.out + r.err).strip() else ""))
    else:
        checks.append(Check("gh", bool(gh), gh or "missing"))
    for a in (cfg.agents_on_host(host) if host in cfg.hosts else cfg.agents.values()):
        if a.provider == "generic":
            exe = (a.command_template or "").split()[0] if a.command_template else ""
            checks.append(Check(f"cli:{a.name} (generic)", bool(exe and (which(exe) or Path(exe).exists())), exe))
            continue
        binary = CLI_BINARY[a.provider]
        path = which(binary)
        detail = path or f"{binary} not found on PATH"
        if path and not offline:
            r = run([binary, "--version"], cwd=cfg.repo_root, timeout=30)
            detail = (r.out or r.err).strip().splitlines()[0] if (r.out or r.err).strip() else path
        checks.append(Check(f"cli:{a.name} ({a.provider})", bool(path), detail
                            + (" [experimental adapter]" if a.experimental else "")))
    for label, rel in (("verify_fast", cfg.verify.fast), ("verify_full", cfg.verify.full)):
        if rel:
            p = cfg.repo_root / rel
            checks.append(Check(label, p.exists(), str(p) if p.exists() else f"missing {rel}"))
    return checks


def smoke_agent(cfg: Config, agent_name: str, tmp_dir: Path) -> Check:
    from .adapters import get_adapter
    from .adapters.base import RunSpec
    a = cfg.agents[agent_name]
    tmp_dir.mkdir(parents=True, exist_ok=True)
    (tmp_dir / ".swarm-run").mkdir(exist_ok=True)
    pf = tmp_dir / "smoke.md"
    pf.write_text("Reply with exactly this JSON and nothing else, then stop: "
                  '{"status":"done","summary":"smoke ok"}. If you cannot return structured output, '
                  "write that JSON to .swarm-run/report.json.")
    spec = RunSpec(prompt_file=pf, model=a.models["low"], effort=a.effort.get("low"), max_turns=3, budget_usd=0.5,
                   timeout_s=180, cwd=tmp_dir, schema={"type": "object", "properties": {"status": {"type": "string"},
                                                                                        "summary": {"type": "string"}},
                                                        "required": ["status"]}, sandbox=a.sandbox)
    r = get_adapter(a).run(spec)
    ok = r.ok and (r.structured_output is not None or (tmp_dir / ".swarm-run" / "report.json").exists())
    return Check(f"smoke:{agent_name}", ok, (r.error or "ok")[:200] + (f" · ${r.usage.cost_usd}" if r.usage.cost_usd else ""))
```

- [ ] **Step 4: Implement cli.py**

```python
# swarm/cli.py
from __future__ import annotations

import json
import os
import shutil
import stat
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from .config import Config, ConfigError, load_config, save_notion_ids
from .models import IMPORTANCES, SIZES, TASK_TYPES, AgentRow, Question, Status, Task, utcnow

app = typer.Typer(help="swarm-control: Notion board + git worktrees + headless coding agents.", no_args_is_help=True)
console = Console()
TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "template"


class State:
    cfg: Config
    memory: bool = False
    host: str | None = None


state = State()


def find_config(start: Path) -> Path:
    cur = Path(start).resolve()
    for p in [cur, *cur.parents]:
        cand = p / ".swarm" / "config.yaml"
        if cand.exists():
            return cand
    raise typer.BadParameter("no .swarm/config.yaml found in this directory or its parents")


def make_board(cfg: Config, memory: bool = False):
    if memory:
        from .board.memory import InMemoryBoard
        return InMemoryBoard()
    from .board.notion import NotionBoard, NotionClient
    token = os.environ.get("NOTION_TOKEN")
    if not token:
        raise typer.Exit(code=_fail("NOTION_TOKEN is not set"))
    if not cfg.notion.tasks_ds:
        raise typer.Exit(code=_fail("Notion ids missing; run `swarm init --parent-page <id>` first"))
    return NotionBoard(NotionClient(token), cfg.notion)


def _fail(msg: str) -> int:
    console.print(f"[red]error:[/red] {msg}")
    return 1


def _workspace(cfg: Config):
    from .workspace import Workspace
    return Workspace(cfg.repo_root, cfg.worktree_root, cfg.main_branch)


def _ledger(cfg: Config):
    from .usage import Ledger, default_ledger_path
    return Ledger(default_ledger_path(cfg.project))


@app.callback()
def main(config: Path = typer.Option(None, "--config", help="path to .swarm/config.yaml"),
         memory: bool = typer.Option(False, "--memory", help="use an in-memory board (dry runs)"),
         host: str = typer.Option(None, "--host", help="this laptop's name in config.hosts (or SWARM_HOST)")):
    if typer.main.get_command(app).name and "template" in os.sys.argv:  # template needs no config
        return
    try:
        state.cfg = load_config(config or find_config(Path.cwd()))
    except (ConfigError, typer.BadParameter) as e:
        raise typer.Exit(code=_fail(str(e)))
    state.memory = memory
    state.host = host or os.environ.get("SWARM_HOST")


@app.command()
def doctor(offline: bool = typer.Option(False, help="skip network checks"),
           smoke: str = typer.Option(None, help="agent name to smoke-test with a 1-turn prompt")):
    """Check tokens, CLIs, git, gh, verify scripts."""
    from .doctor import run_checks, smoke_agent
    checks = run_checks(state.cfg, state.host, offline=offline)
    if smoke:
        checks.append(smoke_agent(state.cfg, smoke, state.cfg.worktree_root / "_smoke"))
    table = Table("check", "ok", "detail")
    for c in checks:
        table.add_row(c.name, "[green]yes[/green]" if c.ok else "[red]NO[/red]", c.detail)
    console.print(table)
    raise typer.Exit(code=0 if all(c.ok for c in checks if c.name != "notion token" or not offline) else 1)


@app.command()
def init(parent_page: str = typer.Option(..., "--parent-page", help="Notion page id that will hold the databases")):
    """Create the Notion databases, board views, and status page; save ids to .swarm/notion.yaml."""
    from .board.notion import NotionBoard, NotionClient
    token = os.environ.get("NOTION_TOKEN")
    if not token:
        raise typer.Exit(code=_fail("NOTION_TOKEN is not set"))
    ids = NotionBoard.init(NotionClient(token), parent_page, list(state.cfg.agents))
    path = save_notion_ids(state.cfg, ids)
    console.print(f"created databases; ids saved to {path}")
    if not str(ids.get("views_ok", "")).startswith("true"):
        console.print("[yellow]board views could not be created via API; add them by hand: "
                      "open each database → + view → Board → group by Status / Agent.[/yellow]")
    agents_sync()


@app.command("agents-sync")
def agents_sync():
    """Refresh Agent rows (and the Agent select options) from config."""
    board = make_board(state.cfg, state.memory)
    for a in state.cfg.agents.values():
        row = board.get_agent(a.name) or AgentRow(name=a.name)
        row.provider, row.host = a.provider, a.host
        board.upsert_agent(row)
    if not state.memory:
        from .board import notion_props as np
        board.c.request("PATCH", f"/data_sources/{state.cfg.notion.tasks_ds}",
                        json={"properties": {"Agent": np.TASKS_SCHEMA(list(state.cfg.agents))["Agent"]}})
    console.print(f"synced {len(state.cfg.agents)} agents")


@app.command()
def plan(plan_file: Path = typer.Argument(Path("PLAN.md")), milestone: str = typer.Option(None, "--milestone"),
         apply: bool = typer.Option(False, "--apply", help="create the tasks without asking")):
    """Decompose PLAN.md into tasks with the planner model, then create them."""
    from .planner import Planner
    board = make_board(state.cfg, state.memory)
    pl = Planner(state.cfg, board, _workspace(state.cfg), log=console.print)
    proposals = pl.propose(plan_file, milestone=milestone)
    _print_proposals(proposals)
    if not proposals:
        raise typer.Exit(code=1)
    if apply or typer.confirm("create these tasks?", default=True):
        pl.apply(proposals)


@app.command("apply-proposals")
def apply_proposals(file: Path = typer.Argument(Path(".swarm/tasks.proposed.json"))):
    """Create tasks from an edited tasks.proposed.json."""
    from .planner import Planner
    board = make_board(state.cfg, state.memory)
    Planner(state.cfg, board, _workspace(state.cfg), log=console.print).apply(json.loads(file.read_text()))


def _print_proposals(proposals):
    table = Table("#", "title", "type", "imp", "size", "ms", "deps", "scope")
    for i, p in enumerate(proposals):
        table.add_row(str(i), p["title"], p["type"], p["importance"], p["size"], p["milestone"],
                      ", ".join(p.get("depends_on", [])), ", ".join(p.get("scope", [])))
    console.print(table)


@app.command()
def add(title: str, type: str = typer.Option("backend", "--type"), importance: str = typer.Option("normal"),
        size: str = typer.Option("M"), milestone: str = typer.Option(""), description: str = typer.Option(""),
        acceptance: str = typer.Option(""), depends: list[str] = typer.Option([], "--depends"),
        scope: list[str] = typer.Option([], "--scope"), priority: int = typer.Option(100)):
    """Add one task, routed."""
    from .router import context_from_board, route
    if type not in TASK_TYPES or importance not in IMPORTANCES or size not in SIZES:
        raise typer.Exit(code=_fail(f"type/importance/size must be in {TASK_TYPES}/{IMPORTANCES}/{SIZES}"))
    board = make_board(state.cfg, state.memory)
    done = {t.id for t in board.list_tasks(status=[Status.DONE])}
    t = Task(id="", title=title, description=description, acceptance=acceptance, type=type, importance=importance,
             size=size, milestone=milestone, depends_on=list(depends), scope=list(scope), priority=priority)
    t.status = Status.READY if all(d in done for d in t.depends_on) else Status.BACKLOG
    t.agent, t.model, t.effort = route(t, state.cfg, context_from_board(board, state.cfg))
    t = board.create_task(t)
    console.print(f"{t.id} {t.status.value} → {t.agent} / {t.model} / {t.effort}: {t.title}")


@app.command()
def assign(task_id: str, agent: str = typer.Option(..., "--agent"), model: str = typer.Option(None),
           effort: str = typer.Option(None)):
    """Override routing for one task."""
    board = make_board(state.cfg, state.memory)
    t = board.get_task(task_id)
    if not t:
        raise typer.Exit(code=_fail(f"{task_id} not found"))
    if agent not in state.cfg.agents:
        raise typer.Exit(code=_fail(f"unknown agent {agent}"))
    t.agent = agent
    t.model = model or state.cfg.agents[agent].models["mid"]
    t.effort = effort or state.cfg.agents[agent].effort.get("mid")
    board.update_task(t, ["agent", "model", "effort"])
    console.print(f"{t.id} → {t.agent} / {t.model} / {t.effort}")


@app.command()
def cut(task_id: str):
    """Mark a task Cut (never run again)."""
    board = make_board(state.cfg, state.memory)
    t = board.get_task(task_id)
    if not t:
        raise typer.Exit(code=_fail(f"{task_id} not found"))
    t.status = Status.CUT
    board.update_task(t, ["status"])
    console.print(f"{t.id} cut")


@app.command()
def split(task_id: str, apply: bool = typer.Option(False, "--apply")):
    """Ask the planner to split a task into smaller ones; cuts the original when applied."""
    from .planner import Planner
    board = make_board(state.cfg, state.memory)
    t = board.get_task(task_id)
    if not t:
        raise typer.Exit(code=_fail(f"{task_id} not found"))
    pl = Planner(state.cfg, board, _workspace(state.cfg), log=console.print)
    proposals = pl.propose(state.cfg.repo_root / "PLAN.md", split_of=t)
    _print_proposals(proposals)
    if proposals and (apply or typer.confirm("create these and cut the original?", default=True)):
        pl.apply(proposals)
        t.status = Status.CUT
        board.update_task(t, ["status"])


@app.command()
def answer(question_id: str, text: str, follow_up: bool = typer.Option(False, "--follow-up",
                                                                       help="for fyi questions: create a follow-up task")):
    """Answer a question (same as typing in Notion)."""
    board = make_board(state.cfg, state.memory)
    qs = [q for q in board.list_questions() if q.id == question_id]
    if not qs:
        raise typer.Exit(code=_fail(f"{question_id} not found"))
    q = qs[0]
    q.answer, q.needs_follow_up = text, follow_up
    board.update_question(q, ["answer", "needs_follow_up"])
    console.print(f"{q.id} answered; serve will relay it within {state.cfg.serve_seconds}s")


@app.command()
def status():
    """Print the status page."""
    from .status import render_status
    board = make_board(state.cfg, state.memory)
    console.print(render_status(state.cfg, board.list_tasks(), board.list_agents(), board.list_questions(), utcnow()))


@app.command()
def reroute():
    """Re-run routing for Ready tasks whose agent is offline, cooling down, or unknown."""
    from .serve import Server
    board = make_board(state.cfg, state.memory)
    srv = Server(state.cfg, board, _workspace(state.cfg), reviewer=None, merger=None, log=console.print)
    console.print(f"rerouted {srv.reroute()} tasks")


@app.command()
def run(agent: str = typer.Option(None, "--agent", help="only this agent"), once: bool = typer.Option(False),
        dry_run: bool = typer.Option(False, "--dry-run", help="compile the next prompt and stop")):
    """Worker loop for every agent on this host."""
    from .runner import Runner, SyncExecutor
    from .prompt import compile_prompt, load_rules
    if not state.host:
        raise typer.Exit(code=_fail("set SWARM_HOST or pass --host"))
    board = make_board(state.cfg, state.memory)
    r = Runner(state.cfg, board, state.host, _workspace(state.cfg), ledger=_ledger(state.cfg),
               log=console.print, executor=SyncExecutor() if once else None)
    if agent:
        r.agents = {agent: r.agents[agent]}
        r.active = {agent: set()}
    if dry_run:
        tasks = r.pending_tasks()
        if not tasks:
            console.print("no pending tasks")
            raise typer.Exit()
        t = tasks[0]
        console.print(compile_prompt(t, state.cfg, rules_text=load_rules(), deps_summaries={},
                                     structured_output_supported=True))
        raise typer.Exit()
    r.loop(once=once)


@app.command()
def serve(no_review: bool = typer.Option(False, "--no-review"), no_merge: bool = typer.Option(False, "--no-merge"),
          once: bool = typer.Option(False)):
    """Control loop: reap, retry, relay answers, promote, review, merge, reroute, status page."""
    from .merge import Merger
    from .reviewer import Reviewer
    from .serve import Server
    board = make_board(state.cfg, state.memory)
    ws = _workspace(state.cfg)

    class NoReview:
        def process(self, task):
            return task

    class NoMerge:
        def merge(self, task):
            return False

    srv = Server(state.cfg, board, ws, reviewer=NoReview() if no_review else Reviewer(state.cfg, board, ws, log=console.print),
                 merger=NoMerge() if no_merge else Merger(state.cfg, board, ws, log=console.print),
                 log=console.print, host=state.host or "serve")
    if once:
        srv.acquire_lock()
        console.print(srv.tick())
        return
    srv.loop()


@app.command()
def logs(task_id: str, attempt: int = typer.Option(None)):
    """Show prompt/stdout/stderr for a task's run on this laptop."""
    base = Path.home() / ".swarm" / state.cfg.project / "runs" / task_id
    if not base.exists():
        raise typer.Exit(code=_fail(f"no logs under {base}"))
    attempts = sorted(base.glob("attempt-*"))
    d = base / f"attempt-{attempt}" if attempt else attempts[-1]
    for name in ("prompt.md", "stdout.txt", "stderr.txt"):
        f = d / name
        if f.exists():
            console.rule(name)
            console.print(f.read_text()[-6000:])


@app.command()
def template(dest: Path):
    """Copy the hackathon-base template into a new directory."""
    if dest.exists() and any(dest.iterdir()):
        raise typer.Exit(code=_fail(f"{dest} is not empty"))
    shutil.copytree(TEMPLATE_DIR, dest, dirs_exist_ok=True)
    for script in (dest / "scripts").glob("*.sh"):
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
    console.print(f"template copied to {dest}. Next: edit .swarm/config.yaml, then `swarm doctor`.")


if __name__ == "__main__":
    app()
```

- [ ] **Step 5: Run tests**

Run: `pytest tests/test_cli.py -v`
Expected: 6 passed (the `template` test needs Task 19's files; it may fail until then — that is expected, rerun after Task 19)

- [ ] **Step 6: Commit**

```bash
git add swarm/doctor.py swarm/cli.py tests/test_cli.py
git commit -m "feat: doctor checks and typer CLI"
```

---

### Task 18: End-to-end test with a fake agent

**Files:**
- Create: `tests/fake_agent.sh`, `tests/test_e2e.py`

**Interfaces:**
- Consumes everything. Uses `GenericAdapter` with `tests/fake_agent.sh` so the real runner, reviewer, merger, and server code paths execute against an in-memory board and a temporary bare git remote.

- [ ] **Step 1: Write tests/fake_agent.sh**

```bash
#!/usr/bin/env bash
# Fake coding agent for tests. usage: fake_agent.sh <prompt_file> <model> <cwd>
# Behaviour is keyed off the task title found in the prompt:
#   *BLOCKME*  -> writes a blocked report with a blocking question
#   *FAILME*   -> exits 1 the first time (per task id, tracked in $FAKE_STATE_DIR), succeeds after
#   otherwise  -> creates src/<id>.txt and a done report
# When the prompt is a review, writes an approve verdict.
set -e
prompt="$1"; model="$2"; cwd="$3"
cd "$cwd"
mkdir -p .swarm-run
if grep -q "^# Review of" "$prompt"; then
  echo '{"verdict":"approve","summary":"fake approve","findings":[]}' > .swarm-run/review.json
  exit 0
fi
if grep -q "^# Planning request" "$prompt"; then
  echo '{"tasks":[]}' > .swarm-run/plan.json
  exit 0
fi
title=$(grep -m1 '^- Title:' "$prompt" | sed 's/^- Title: //')
id=$(grep -m1 '^- ID:' "$prompt" | sed 's/^- ID: //')
state="${FAKE_STATE_DIR:-/tmp}/fake-$id"
case "$title" in
  *BLOCKME*)
    if grep -q "Human answer" "$prompt"; then :; else
      cat > .swarm-run/report.json <<EOF
{"status":"blocked","summary":"need input","question":{"kind":"blocking","text":"which way?","options":["a","b"]}}
EOF
      exit 0
    fi;;
  *FAILME*)
    if [ ! -f "$state" ]; then touch "$state"; echo "boom" >&2; exit 1; fi;;
esac
mkdir -p src
echo "// $id $title ($model)" >> "src/${id}.txt"
cat > .swarm-run/report.json <<EOF
{"status":"done","summary":"implemented $title","files_changed":["src/${id}.txt"],
 "decisions":[{"decision":"fake decision for $id","why":"test","impact":"low"}]}
EOF
```

- [ ] **Step 2: Write tests/test_e2e.py**

```python
# tests/test_e2e.py
import os
import subprocess
from pathlib import Path

import yaml

from swarm.board.memory import InMemoryBoard
from swarm.config import load_config
from swarm.merge import Merger
from swarm.models import Task, Status
from swarm.reviewer import Reviewer
from swarm.runner import Runner, SyncExecutor
from swarm.serve import Server
from swarm.usage import Ledger
from swarm.workspace import Workspace, CmdResult

FAKE = Path(__file__).parent / "fake_agent.sh"


def e2e_config(project_dir, sample_config_dict):
    d = sample_config_dict
    d["hosts"] = {"lab": {"max_parallel": {"generic": 3}}}
    d["agents"] = {
        "fe": {"provider": "generic", "host": "lab", "parallel": 1,
               "models": {"best": "big", "high": "big", "mid": "mid", "low": "small"},
               "strengths": {"frontend": 5, "backend": 2}, "command_template": f"bash {FAKE} {{prompt_file}} {{model}} {{cwd}}"},
        "be": {"provider": "generic", "host": "lab", "parallel": 2,
               "models": {"best": "big", "high": "big", "mid": "mid", "low": "small"},
               "strengths": {"frontend": 2, "backend": 5}, "command_template": f"bash {FAKE} {{prompt_file}} {{model}} {{cwd}}"},
    }
    d["reviewer"] = {"agent": "be", "model": "mid"}
    d["planner"] = {"agent": "fe", "model": "big"}
    d["review_policy"] = "high_and_above"
    (project_dir / ".swarm" / "config.yaml").write_text(yaml.safe_dump(d))
    return load_config(project_dir / ".swarm" / "config.yaml")


def _git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True).stdout


def test_six_tasks_end_to_end(project_dir, sample_config_dict, git_repo, tmp_path, monkeypatch):
    cfg = e2e_config(project_dir, sample_config_dict)
    cfg.repo_root = git_repo  # run against the git fixture repo
    monkeypatch.setenv("FAKE_STATE_DIR", str(tmp_path / "state"))
    (tmp_path / "state").mkdir()

    def gh(args, cwd):
        if args[:2] == ["pr", "merge"]:
            _git(git_repo, "fetch", "-q", "origin")
            _git(git_repo, "merge", "-q", "--no-edit", f"origin/{args[2]}")
            _git(git_repo, "push", "-q", "origin", "main")
            return CmdResult(0, "merged", "")
        if args[:2] == ["pr", "view"]:
            return CmdResult(1, "", "none")
        if args[:2] == ["pr", "create"]:
            return CmdResult(0, f"https://gh/pr/{args[3]}\n", "")
        return CmdResult(0, "", "")

    board = InMemoryBoard()
    ws = Workspace(git_repo, tmp_path / "wt", gh=gh)
    quiet = lambda *a, **k: None  # noqa: E731
    runner = Runner(cfg, board, "lab", ws, ledger=Ledger(tmp_path / "usage.jsonl"), sleep=lambda s: None,
                    executor=SyncExecutor(), log=quiet, rules_text="rules")
    srv = Server(cfg, board, ws, reviewer=Reviewer(cfg, board, ws, log=quiet, prompt_text="review rules"),
                 merger=Merger(cfg, board, ws, log=quiet), sleep=lambda s: None, log=quiet)

    t1 = board.create_task(Task(id="", title="Contract", type="backend", importance="critical", size="S", status=Status.BACKLOG))
    t2 = board.create_task(Task(id="", title="API", type="backend", importance="high", size="M", depends_on=[t1.id], status=Status.BACKLOG))
    t3 = board.create_task(Task(id="", title="UI", type="frontend", importance="normal", size="M", depends_on=[t1.id], status=Status.BACKLOG))
    t4 = board.create_task(Task(id="", title="Docs BLOCKME", type="docs", importance="low", size="S", status=Status.BACKLOG))
    t5 = board.create_task(Task(id="", title="Flaky FAILME", type="backend", importance="normal", size="S", status=Status.BACKLOG))
    t6 = board.create_task(Task(id="", title="Integration", type="integration", importance="high", size="L",
                                depends_on=[t2.id, t3.id], status=Status.BACKLOG))

    assert srv.acquire_lock()
    for _ in range(12):
        srv.tick()
        runner.tick()
        statuses = {t.id: t.status for t in board.list_tasks()}
        if statuses[t4.id] is Status.BLOCKED and all(statuses[t] is Status.DONE for t in (t1.id, t2.id, t3.id, t5.id, t6.id)):
            break
    statuses = {t.id: t.status for t in board.list_tasks()}
    assert statuses[t1.id] is Status.DONE and statuses[t2.id] is Status.DONE and statuses[t3.id] is Status.DONE
    assert statuses[t6.id] is Status.DONE, statuses
    assert statuses[t5.id] is Status.DONE and board.get_task(t5.id).attempts == 2
    assert statuses[t4.id] is Status.BLOCKED
    q = board.list_questions(status="Open")[0]
    assert q.task_id == t4.id and q.text == "which way?"

    # answer the question; relay unblocks; the fake finishes
    q.answer = "a"
    board.update_question(q, ["answer"])
    for _ in range(4):
        srv.tick()
        runner.tick()
    assert board.get_task(t4.id).status is Status.DONE
    assert board.list_questions()[0].status == "Applied"

    # main has every task's file and decision log
    _git(git_repo, "pull", "-q", "origin", "main")
    for t in (t1, t2, t3, t4, t5, t6):
        assert (git_repo / "src" / f"{t.id}.txt").exists()
        assert (git_repo / "docs" / "decisions" / f"{t.id}.md").exists()
    # reviewed tasks got a review round on their card; normal ones did not
    assert any(h.startswith("Review") for h, _ in board.reports[t2.id])
    assert not any(h.startswith("Review") for h, _ in board.reports[t3.id])
    assert "SWARM STATUS" in board.status_page
```

- [ ] **Step 3: Run to verify it fails, then make it pass**

Run: `pytest tests/test_e2e.py -v`
Expected: initially may fail on integration details; fix the components (not the test) until it passes. Common fixes: `Runner.tick` must not dispatch tasks already Running; `Server.promote` must run after merges; `fake_agent.sh` must be executable (`chmod +x tests/fake_agent.sh`).

- [ ] **Step 4: Run the whole suite**

Run: `pytest -q`
Expected: all passed

- [ ] **Step 5: Commit**

```bash
chmod +x tests/fake_agent.sh
git add tests/fake_agent.sh tests/test_e2e.py
git commit -m "test: end-to-end run with fake agent, review, merge, block, retry, relay"
```

---

### Task 19: Template repo, orchestrator guide, runbook, README

**Files:**
- Create: `template/PLAN.md`, `template/AGENTS.md`, `template/CLAUDE.md`, `template/GEMINI.md`, `template/.gitignore`, `template/docs/ARCHITECTURE.md`, `template/docs/DESIGN.md`, `template/docs/CONTRACTS.md`, `template/docs/decisions/README.md`, `template/docs/debt/README.md`, `template/scripts/setup_worktree.sh`, `template/scripts/verify_fast.sh`, `template/scripts/verify_full.sh`, `template/.swarm/config.yaml`, `template/.github/workflows/verify.yml`, `template/frontend/README.md`, `template/backend/README.md`, `template/ml/README.md`, `template/eval/README.md`
- Create: `ORCHESTRATOR.md`, `docs/RUNBOOK.md`, `README.md`

- [ ] **Step 1: Write the template files**

`template/PLAN.md`:
```markdown
# <Project name>

## Summary
<Two sentences: what it is, who it is for, what the judge sees in the first ten seconds.>

## Demo story
<The 90-second demo, step by step. What is on screen, what the user does, what changes.>

## Success criteria
- <observable thing 1>
- <observable thing 2>

## Milestones
- M0 skeleton: repo boots, verify scripts pass, contracts drafted
- M1 vertical slice: <thinnest end-to-end path that demos>
- M2 core: <the features the demo needs>
- M3 polish: latency, degraded states, demo path, debt
- M4 demo: freeze, rehearse, pitch

## Constraints
- 24 hours, team of <n>, laptops: <specs>, devices: <hardware>
- Floor (must work by hour 6): <simple path>
- Ceiling (only if the floor is safe): <ambitious path>

## Out of scope
- <things we will not build>
```

`template/AGENTS.md`:
```markdown
# Agent rules for this repo

You are one worker among several autonomous agents. Read `PLAN.md` for the product, `docs/ARCHITECTURE.md` for how pieces fit, `docs/CONTRACTS.md` for API and event shapes, and `docs/DESIGN.md` for UX rules. Your task prompt tells you which of these matter for your task.

- Stay inside your task's Scope. Commit as you go. Never push, never open PRs, never switch branches.
- Run `bash scripts/verify_fast.sh` before you finish. Fix what it reports.
- List every temporary hack (mock, hardcode, placeholder, skipped test, assumption) in your report.
- Record every product or interface decision in your report with its impact.
- Do not edit `PLAN.md`. Frontend tasks may edit `docs/DESIGN.md`; backend tasks may edit `docs/CONTRACTS.md` and must say which consumers are affected.
- If you cannot proceed without a human decision, stop and report `blocked` with a `blocking` question. Nobody is watching a chat window.
```

`template/CLAUDE.md`:
```markdown
@AGENTS.md
```

`template/GEMINI.md`:
```markdown
@AGENTS.md
```

`template/.gitignore`:
```
.swarm-run/
.env
node_modules/
.venv/
__pycache__/
*.pyc
artifacts/**/*.png
artifacts/**/*.mp4
```

`template/docs/ARCHITECTURE.md`:
```markdown
# Architecture

## Components
<one line per component: name, responsibility, language, entry point>

## Data flow
<capture → processing → transport → UI, with the event names from docs/CONTRACTS.md>

## How to run
```
scripts/setup_worktree.sh   # once per worktree
scripts/verify_fast.sh      # lint + unit + smoke
```

## ml
<models, inputs, outputs, latency budget per stage, fallback tiers>
```

`template/docs/DESIGN.md`:
```markdown
# Design

## Principles
- <what the product should feel like>
- <what to never show the user>

## Information hierarchy
1. <most important thing on screen>
2. <second>

## Tokens
- color.primary: <hex>
- radius.card: 12px
- font.ui: <family>

## UI state machine
- state → visible elements → available actions → transition conditions

## Components
- <name>: <purpose>, <states>
```

`template/docs/CONTRACTS.md`:
```markdown
# Contracts

## http
<endpoint, method, request, response, errors>

## events
<WebSocket/event name, JSON shape, producer, consumers>

## schemas
<shared JSON schemas or TypeScript types, one per block>
```

`template/docs/decisions/README.md`:
```markdown
One file per task, written by the harness on each attempt: `T-012.md` lists the decisions that task made and why. Concatenate for the pitch.
```

`template/docs/debt/README.md`:
```markdown
One file per task, written by the harness: every mock, hardcode, placeholder, skipped test, or assumption that task left behind, and how to fix it.
```

`template/scripts/setup_worktree.sh`:
```bash
#!/usr/bin/env bash
# Called once per fresh worktree. Link heavy dependencies from the main checkout instead of reinstalling.
set -euo pipefail
MAIN="$(git -C "$(dirname "$0")/.." rev-parse --path-format=absolute --git-common-dir)/.."
for d in frontend/node_modules backend/node_modules .venv; do
  if [ -d "$MAIN/$d" ] && [ ! -e "$d" ]; then
    mkdir -p "$(dirname "$d")" && ln -s "$MAIN/$d" "$d"
  fi
done
```

`template/scripts/verify_fast.sh`:
```bash
#!/usr/bin/env bash
# ≤ 60 seconds. Lint + unit + smoke. Exit non-zero on any failure. Print only summaries.
set -uo pipefail
status=0
if [ -f frontend/package.json ]; then (cd frontend && npx --no-install tsc --noEmit -p . 2>&1 | tail -20) || status=1; fi
if [ -f backend/pyproject.toml ] || [ -d backend/tests ]; then (cd backend && python -m pytest -q -x --no-header 2>&1 | tail -20) || status=1; fi
if [ -d ml/tests ]; then (cd ml && python -m pytest -q -x --no-header -m "not slow" 2>&1 | tail -20) || status=1; fi
exit $status
```

`template/scripts/verify_full.sh`:
```bash
#!/usr/bin/env bash
# Minutes. Everything in verify_fast plus integration and browser checks.
set -uo pipefail
bash "$(dirname "$0")/verify_fast.sh" || exit 1
status=0
if [ -f frontend/package.json ] && [ -d frontend/e2e ]; then (cd frontend && npx --no-install playwright test 2>&1 | tail -30) || status=1; fi
if [ -d eval/acceptance ]; then python -m pytest -q eval/acceptance 2>&1 | tail -30 || status=1; fi
exit $status
```

`template/.swarm/config.yaml`: the full config from the spec §4 with the two-laptop agent roster, `event` dates for Oct 10–11 2026, and a comment block above `strengths` citing the evidence (WebDev Arena Sep 25 2026: Opus 5.5 #1; Terminal-Bench 2.1: Fable 5.1 top; practitioner consensus: Codex for backend; Gemini for multimodal and cheap bulk).

`template/.github/workflows/verify.yml`:
```yaml
name: verify
on: [pull_request]
jobs:
  fast:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.12" }
      - uses: actions/setup-node@v4
        with: { node-version: "22" }
      - run: bash scripts/verify_fast.sh
```

`template/frontend/README.md`, `backend/README.md`, `ml/README.md`, `eval/README.md`: one line each: what lives here and how to run it.

- [ ] **Step 2: Write ORCHESTRATOR.md** (imported by the project's CLAUDE.md when the orchestrator session runs in the project repo; the template's `CLAUDE.md` gets a second line `@../swarm-control/ORCHESTRATOR.md` only on the orchestrator laptop)

```markdown
# Orchestrator guide

You are the orchestrator session: a human plus you, shaping the task graph. You never read or write product code. Workers do that.

## Your tools
- `swarm status` — the status page. Read the RISK lines first.
- `swarm plan PLAN.md --milestone M1` — decompose the next milestone. Review the table; fix titles, scopes, dependencies; apply.
- `swarm add "title" --type backend --importance high --size M --scope "backend/**" --depends T-003` — one task.
- `swarm assign T-012 --agent codex-a --model gpt-6-astra` — override routing.
- `swarm cut T-012` / `swarm split T-012` — remove or break up.
- `swarm answer Q-004 "use tap"` — answer a question (or type in Notion).
- `swarm reroute` — after a rate limit or a laptop going offline.

## Rhythm
- Every hour: `swarm status`. If a milestone's RISK line fires, cut or split before adding anything.
- Plan the next milestone when the current one is ~70% done. Detail the next, keep later ones coarse.
- Answer Open questions within minutes; they are the only thing that waits on a human.
- Contracts before consumers. If two proposed tasks share files, make one depend on the other.
- At T-4h: set `review_policy: all` in config, restart `swarm serve --no-merge`, and merge by hand. Only bugfix and polish tasks after that.
- At T-2h: stop planning. Rehearse the demo. Build the pitch from docs/decisions/*.md.

## What not to do
- Do not open worker branches to "help". Add a task or answer a question instead.
- Do not raise every task to critical; critical is what the demo dies without.
- Do not restart workers to fix a bad task; cut it and add a better one.
```

- [ ] **Step 3: Write docs/RUNBOOK.md**

Minute-by-minute game day from spec §19, expanded: pre-event checklist (both laptops: install, `gh auth login`, `NOTION_TOKEN`, `SWARM_HOST`, `swarm doctor`, `swarm doctor --smoke <agent>`), 0:00–0:45 writing PLAN.md/DESIGN/CONTRACTS with the orchestrator, `swarm plan --milestone M1`, starting `caffeinate -i swarm serve` and `swarm run` on each laptop, first-hour rules, hourly loop, T-4h and T-2h switches, recovery procedures (laptop died: restart `swarm run`; serve died: restart, lock takes over after 10 min or `swarm serve --host <same name>`; Notion down: workers keep working, nothing to do; rate limits: check `swarm status`, optionally `swarm reroute`; broken main: `swarm serve --no-merge`, fix by hand, resume).

- [ ] **Step 4: Write README.md**

Install (`pip install -e .`), concepts in ten lines, quick start (`swarm template`, edit config, `swarm init`, `swarm doctor`, `swarm plan`, `swarm serve`, `swarm run`), the status set, the question channel, how routing works and how to edit strengths, how to add a provider (generic adapter), disclosure note for hackathons, link to the spec and runbook.

- [ ] **Step 5: Run the CLI template test and full suite**

Run: `pytest -q`
Expected: all passed (including `test_template_copies`)

- [ ] **Step 6: Commit**

```bash
git add template ORCHESTRATOR.md docs/RUNBOOK.md README.md
git commit -m "docs: hackathon-base template, orchestrator guide, runbook, readme"
```

---

### Task 20: Notion integration test (opt-in) and manual verification

**Files:**
- Create: `tests/test_notion_integration.py`

- [ ] **Step 1: Write the opt-in test**

```python
# tests/test_notion_integration.py
"""Runs only when SWARM_NOTION_TOKEN and SWARM_NOTION_PARENT are set. Creates real databases under the parent page."""
import os
import pytest

from swarm.board.notion import NotionBoard, NotionClient
from swarm.config import NotionIds
from swarm.board.base import claim_task
from swarm.models import Task, Status, Question, AgentRow

TOKEN = os.environ.get("SWARM_NOTION_TOKEN")
PARENT = os.environ.get("SWARM_NOTION_PARENT")
pytestmark = pytest.mark.skipif(not (TOKEN and PARENT), reason="set SWARM_NOTION_TOKEN and SWARM_NOTION_PARENT")


def test_init_and_roundtrip():
    client = NotionClient(TOKEN)
    ids = NotionBoard.init(client, PARENT, ["claude-a", "codex-a"])
    assert ids["tasks_ds"] and ids["questions_ds"] and ids["agents_ds"] and ids["status_block"]
    board = NotionBoard(client, NotionIds(**{k: v for k, v in ids.items() if k in NotionIds.__dataclass_fields__}))
    t = board.create_task(Task(id="", title="integration task", description="hello **world**", status=Status.READY,
                               agent="claude-a", type="docs", scope=["docs/**"]))
    assert t.id == "T-001"
    assert claim_task(board, t, "claude-a") is True
    assert board.get_task("T-001").status is Status.RUNNING
    board.append_task_report(t, "Report — attempt 1", "did a thing\n\n```json\n{\"a\":1}\n```")
    assert [x.id for x in board.list_tasks(status=[Status.RUNNING], agent=["claude-a"])] == ["T-001"]
    q = board.create_question(Question(id="", text="works?", task_id="T-001", kind="fyi"))
    assert board.list_questions(status="Open")[0].id == q.id
    row = board.upsert_agent(AgentRow(name="claude-a", provider="claude", host="lab", runs=1))
    row.runs = 2
    board.upsert_agent(row)
    assert board.get_agent("claude-a").runs == 2
    board.write_status_page("SWARM STATUS · integration test")
    print("views:", ids.get("views_ok"))
```

- [ ] **Step 2: Run it against a throwaway page**

Run: `SWARM_NOTION_TOKEN=... SWARM_NOTION_PARENT=... pytest tests/test_notion_integration.py -v -s`
Expected: PASS; open the parent page in Notion and confirm three databases, board views (or the printed `views_ok` reason), and the status page.

- [ ] **Step 3: Commit**

```bash
git add tests/test_notion_integration.py
git commit -m "test: opt-in Notion integration round trip"
```

---

## Self-review notes (run after all tasks)

- **Spec coverage:** §3 data model → Tasks 4, 5. §4 config → Task 2. §5 routing → Task 10. §6 worker loop → Task 12. §7 prompt → Task 9. §8 adapters → Task 7. §9 serve → Task 15. §10 reviewer → Task 13. §11 merge → Task 14 (decision/debt logs moved onto the branch as per-task files in Task 12, a deliberate change from the spec's "append on main" to avoid shared-file conflicts; update the spec §11 wording). §12 commands → Task 17. §13 template → Task 19. §14 budget → Tasks 10, 11, 12 (soft caps, cooldown, tiers, caps). §15 failures → Tasks 12, 14, 15. §17 layout → matches. §18 testing → every task plus Tasks 18, 20. §19 runbook → Task 19.
- **Interface consistency:** `claim_task(board, task, agent, *, sleep, nonce, wait_s)`; `Board.update_task(task, fields)`; `Workspace.provision(task_id, *, reuse_branch)`; `Adapter.run(spec) -> RunResult`; `parse_report(structured, worktree, *, changed_files)`; `route(task, cfg, ctx) -> (agent, model, effort)`; `Reviewer.process(task) -> Task`; `Merger.merge(task) -> bool`; `Server.tick() -> dict`. Task 17's CLI uses only these.
- **Review Focus tests present:** unknown agent (Task 12 `test_tick_respects_slots_and_skips_unknown_agent`, Task 15 `test_reroute_unknown_agent_and_cooldown`); long rich_text (Task 4); id-not-title lookup (Task 3); done-without-changes (Task 12); long Retry-After (Task 5).
