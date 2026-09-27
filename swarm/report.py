"""Worker report contract: JSON schema, parsing with fallbacks, markdown rendering."""
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
        tests=dict(data.get("tests") or {}),
        debts=[d for d in (data.get("debts") or []) if isinstance(d, dict)],
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
