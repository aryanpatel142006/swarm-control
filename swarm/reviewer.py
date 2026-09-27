"""Independent reviewer: deterministic verify first, then a read-only model pass from a different family."""
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
                               [{"severity": "high", "file": self.cfg.verify.full or "", "issue": full.tail(1500),
                                 "fix": "make the full verify pass"}])
            role = self.cfg.reviewer
            if role is None:
                return Verdict("approve", "no reviewer configured; verify passed", [])
            agent_cfg = self.cfg.agents[role.agent]
            diff = self.ws.git(wt, "diff", f"{self.ws.remote}/{self.cfg.main_branch}...HEAD", check=False).out
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
                impact="high", task_id=task.id,
                asked_by=self.cfg.reviewer.agent if self.cfg.reviewer else "reviewer"))
        self.board.update_task(task, ["status", "feedback", "review_rounds", "flags"])
        self.log(f"[{task.id}] review → {v.verdict}")
        return task

    def process(self, task: Task) -> Task:
        return self.apply(task, self.review(task))
