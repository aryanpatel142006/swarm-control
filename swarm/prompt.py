"""Prompt compiler: rules + task card + feedback + only the docs this task type needs + report contract."""
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
             "- Scope (files you may edit): "
             + (", ".join(task.scope) if task.scope else "whole repo, keep it minimal")]
    if task.depends_on:
        parts.append("- Depends on: " + "; ".join(f"{d}: {deps_summaries.get(d, '(see repo)')}"
                                                   for d in task.depends_on))
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
    parts += ["", "```json", json.dumps(REPORT_SCHEMA, separators=(",", ":")), "```", ""]
    return "\n".join(parts)
