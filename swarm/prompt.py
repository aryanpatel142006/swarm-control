"""Prompt compiler: rules + task card + feedback + only the docs this task type needs + report contract."""
from __future__ import annotations

import json
import re
from pathlib import Path

from .config import Config, by_type
from .models import Task
from .report import REPORT_SCHEMA

DOC_CHAR_CAP = 12000
PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"   # package data: ships with pip install


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
    refs = list(cfg.docs_by_type.get("_all", [])) + by_type(cfg.docs_by_type, task_type)
    return list(dict.fromkeys(refs))


def tools_section(task: Task, mcp: list[str], skills: list[str], skill_tool: bool = True) -> list[str]:
    """Tell the worker which MCP servers are live and which skills to invoke; critical tasks may add more."""
    if not mcp and not skills and task.importance != "critical":
        return []
    parts = ["", "## Tools for this task", ""]
    parts.append("- In your final report, fill `tools_used` with every skill, MCP server or plugin you actually used "
                 "and whether it helped; the harness writes it into docs/decisions/<task id>.md.")
    if mcp:
        parts.append("- MCP servers enabled for this run: " + ", ".join(mcp)
                     + ". Use them (docs lookup, browser checks, component search) instead of guessing.")
    names = ", ".join(f"`{s}`" for s in skills)
    # Codex and the other non-Claude CLIs have no Skill tool; telling them to use one cost a harness note per
    # task (Q-128, Q-134). They get the file path and nothing about a tool they do not have.
    where = ("read `.claude/skills/<name>/SKILL.md` in the worktree (or `~/.claude/skills/<name>/SKILL.md`, "
             "`~/.codex/skills/<name>/SKILL.md`) and follow it; reading those files is allowed")
    if skills and task.size == "S":   # a small wiring change paid ~6k tokens of general guides it never used (Q-123, Q-127)
        how = "invoke them with the Skill tool" if skill_tool else where
        parts.append(f"- Skills available for this task: {names}. This is a small task: use only the ones whose area "
                     f"your change actually touches ({how}), and read only the sections you need.")
    elif skills:
        how = ("Invoke them with the Skill tool (or `/<name>`) and follow them." if skill_tool
               else f"Your CLI has no Skill tool: for each one, {where}.")
        parts.append(f"- Skills to use before you start the relevant work: {names}. {how}")
    if task.importance == "critical":
        parts.append("- This task is critical. If another official plugin or skill would clearly raise the "
                     "quality of the result, install it: `claude plugin install <name>@claude-plugins-official` "
                     "(`claude plugin list --available --json` lists them). A plugin installed mid-run is not "
                     "loaded into this session, so read its SKILL.md under ~/.claude/plugins/cache/ and follow "
                     "it directly. Record what you installed and why in docs/decisions/<task id>.md so the "
                     "orchestrator can make it a default for this task type.")
    return parts


def compile_prompt(task: Task, cfg: Config, *, rules_text: str, deps_summaries: dict[str, str],
                   structured_output_supported: bool, mcp: list[str] = (), skills: list[str] = (),
                   skill_tool: bool = True, conflicts_note: str = "") -> str:
    import platform
    host_line = (f"Host: {platform.node()} · {platform.system()} {platform.machine()} · Python {platform.python_version()}. "
                 "Measurements requested for another host are not yours to take: say so in the report.")
    parts = [f"# Worker task {task.id}", "", "## Rules", "", rules_text.strip(), "", host_line, "", "## Task", "",
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
    if conflicts_note.strip():
        parts += ["", "## Merge conflicts: resolve these first", "", conflicts_note.strip()]
    if task.feedback.strip():
        parts += ["", "## Feedback and messages for this task (address every item)", "", task.feedback.strip()]
    parts += tools_section(task, list(mcp), list(skills), skill_tool)
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
