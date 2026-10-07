"""Worker report contract: JSON schema, parsing with fallbacks, markdown rendering."""
from __future__ import annotations

import json
import re
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
        "tools_used": {"type": "array", "description": "skills, MCP servers, plugins you actually used",
                       "items": {"type": "object", "properties": {
                           "name": {"type": "string"}, "kind": {"type": "string", "enum": ["skill", "mcp", "plugin", "cli"]},
                           "helped": {"type": "boolean"}, "note": {"type": "string"}}, "required": ["name"]}},
        "question": {"type": "object", "properties": {
            "kind": {"type": "string", "enum": ["blocking", "fyi"]}, "text": {"type": "string"},
            "options": {"type": "array", "items": {"type": "string"}}, "proceeding_with": {"type": "string"}},
            "required": ["kind", "text"]},
        "notes_for_reviewer": {"type": "string"},
        "messages": {"type": "array", "description": "Notes for other tasks' agents (an interface you changed, a "
                     "file you need them to leave alone); relayed into that task's next prompt by the harness",
                     "items": {"type": "object", "properties": {"to": {"type": "string", "description": "task id, e.g. T-012"},
                                                                "text": {"type": "string"}}, "required": ["to", "text"]}},
        "harness_feedback": {"type": "array", "description": "Problems with the harness, prompt, rules, skills, "
                             "tools or verify scripts that cost you time; the orchestrator fixes them, not you",
                             "items": {"type": "object", "properties": {
                                 "what": {"type": "string"}, "suggestion": {"type": "string"}},
                                 "required": ["what"]}},
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


def parse_report(structured: dict | None, worktree: Path, *, changed_files: list[str], error: str = "") -> Report:
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
        summary = "Report missing; synthesized from the diff." if changed_files else "Report missing and no files changed."
        if error:
            summary += f" CLI: {error.strip()[:300]}"
        return Report(status="done" if changed_files else "failed", summary=summary,
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
        tests=dict(data.get("tests") or {}),
        debts=[d for d in (data.get("debts") or []) if isinstance(d, dict)],
        decisions=[d for d in (data.get("decisions") or []) if isinstance(d, dict)],
        tools_used=_tools(data.get("tools_used")), question=q,
        notes_for_reviewer=str(data.get("notes_for_reviewer") or ""),
        harness_feedback=_feedback(data.get("harness_feedback")),
        messages=[m for m in (data.get("messages") or []) if isinstance(m, dict)],
    )


def _feedback(raw) -> list[dict]:
    out = []
    for f in raw or []:
        if isinstance(f, str) and f.strip():
            out.append({"what": f.strip(), "suggestion": ""})
        elif isinstance(f, dict) and str(f.get("what", "")).strip():
            out.append({"what": str(f["what"]).strip(), "suggestion": str(f.get("suggestion") or "").strip()})
    return out


def harness_feedback_question(task: Task, r: Report) -> dict | None:
    """One fyi board note per task carrying the worker's harness feedback, so the orchestrator sees it on the
    board from any laptop and ships the fix (workers never edit the harness or the skills)."""
    if not r.harness_feedback:
        return None
    n = len(r.harness_feedback)
    lines = [f"- {f['what']}" + (f" → {f['suggestion']}" if f.get("suggestion") else "") for f in r.harness_feedback]
    return {"kind": "harness", "text": f"[harness] {task.id}: {n} note{'s' if n != 1 else ''} from {task.agent or 'worker'}"
            f" — {r.harness_feedback[0]['what']}",
            "options": [], "proceeding_with": "orchestrator triages: fix harness / tune project / update skill",
            "context": "\n".join(lines)}


def _words(text: str) -> set[str]:
    return set(re.sub(r"\W+", " ", (text or "").lower()).split())


def repeated_feedback(item: dict, earlier: list[str], *, threshold: float = 0.8) -> bool:
    """True when a harness note says what an earlier note for the same task already said. A resumed attempt reads
    the previous report and re-files its harness_feedback reworded ("said" → "says", one clause dropped): T-110's
    attempt 2 opened Q-288 with two of Q-286's three notes. Word overlap over the shorter note, not equality."""
    words = _words(item.get("what", ""))
    if len(words) < 5:
        return False
    for line in earlier:
        other = _words(line.split(" → ", 1)[0])
        if len(other) >= 5 and len(words & other) / min(len(words), len(other)) >= threshold:
            return True
    return False


def earlier_feedback_lines(questions, task_id: str) -> list[str]:
    """The `- what → suggestion` lines of every harness note already filed for this task (open or answered)."""
    out = []
    for q in questions:
        if q.kind == "harness" and q.task_id == task_id and q.text.startswith("[harness]"):
            out += [ln[2:] for ln in (q.context or "").splitlines() if ln.startswith("- ")]
    return out


def _tools(raw) -> list[dict]:
    out = []
    for t in raw or []:
        if isinstance(t, str) and t.strip():
            out.append({"name": t.strip()})
        elif isinstance(t, dict) and str(t.get("name", "")).strip():
            out.append({k: v for k, v in t.items() if k in ("name", "kind", "helped", "note")})
    return out


def _tool_line(t: dict) -> str:
    bits = [str(t.get("kind"))] if t.get("kind") else []
    if "helped" in t:
        bits.append("helped" if t["helped"] else "did not help")
    text = t["name"] + (f" ({', '.join(bits)})" if bits else "")
    return text + (f": {t['note']}" if t.get("note") else "")


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
    if r.tools_used:
        lines += ["", "Tools used: " + "; ".join(_tool_line(t) for t in r.tools_used)]
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
    if not r.decisions and not r.tools_used and not r.harness_feedback:
        return ""
    stamp = utcnow().strftime("%Y-%m-%d %H:%M UTC")
    out = [f"## {task.id} · {task.title} ({stamp})"]
    out += [f"- **[{d.get('impact', '?')}]** {d.get('decision', '')} — {d.get('why', '')}" for d in r.decisions]
    if r.tools_used:
        out.append("- **Tools used:** " + "; ".join(_tool_line(t) for t in r.tools_used))
    if r.harness_feedback:
        out.append("- **Harness feedback:** " + "; ".join(
            f["what"] + (f" → {f['suggestion']}" if f.get("suggestion") else "") for f in r.harness_feedback))
    return "\n".join(out) + "\n\n"


def debts_markdown(task: Task, r: Report) -> str:
    if not r.debts:
        return ""
    stamp = utcnow().strftime("%Y-%m-%d %H:%M UTC")
    out = [f"## {task.id} · {task.title} ({stamp})"]
    out += [f"- `{d.get('kind', '?')}` `{d.get('location', '?')}` — {d.get('reason', '')} → fix: {d.get('fix', '')}"
            for d in r.debts]
    return "\n".join(out) + "\n\n"
