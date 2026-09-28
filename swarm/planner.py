"""Planner: decompose PLAN.md into routed tasks with the planner model; split a task into smaller ones."""
from __future__ import annotations

import json
import time
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
    parts += [f"- {t.id} [{t.status.value}] {t.title} (type={t.type}, milestone={t.milestone or '-'})"
              for t in existing] or ["(none)"]
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
                 prompt_text: str | None = None, ledger=None):
        self.cfg, self.board, self.ws, self.adapter_factory, self.log = cfg, board, ws, adapter_factory, log
        self.ledger = ledger
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
        started = time.monotonic()
        result = self.adapter_factory(agent_cfg).run(spec)
        if self.ledger is not None:   # the planner's spend is part of the project's bill too
            label = f"plan:{milestone}" if milestone else (f"split:{split_of.id}" if split_of else "plan")
            self.ledger.append(agent=role.agent, model=spec.model, task_id=label, usage=result.usage, role="planner",
                               duration_s=time.monotonic() - started, ok=result.ok)
        if not result.ok and result.structured_output is None:
            raise RuntimeError(f"planner run failed: {result.error[:300]}")
        proposals = parse_proposals(result.structured_output, wt)
        out = self.cfg.repo_root / ".swarm" / "tasks.proposed.json"
        out.parent.mkdir(parents=True, exist_ok=True)
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
