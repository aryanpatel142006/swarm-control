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
