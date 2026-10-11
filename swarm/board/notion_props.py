"""Notion property builders/readers and the database schemas for tasks, questions, agents."""
from __future__ import annotations

import re
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


STATUS_COLORS = {"Backlog": "gray", "Ready": "blue", "Running": "yellow", "Review": "purple",
                 "Changes Requested": "orange", "Merge Ready": "green", "Blocked": "red", "Failed": "red",
                 "Done": "green", "Cut": "gray"}
IMPORTANCE_COLORS = {"critical": "red", "high": "orange", "normal": "blue", "low": "gray"}
SIZE_COLORS = {"S": "green", "M": "yellow", "L": "orange"}
TYPE_COLORS = {"frontend": "yellow", "backend": "yellow", "realtime": "yellow", "integration": "yellow", "infra": "yellow",
               "ml_audio": "purple", "ml_vision": "purple", "ml_fusion": "purple", "eval": "purple",
               "tests": "pink", "bugfix": "pink", "docs": "green", "research": "green"}
AGENT_STATUS_COLORS = {"idle": "gray", "running": "green", "cooldown": "yellow", "offline": "red", "removed": "brown"}
PROVIDER_COLORS = {"claude": "orange", "codex": "green", "antigravity": "blue", "gemini": "blue", "grok": "gray",
                   "perplexity": "pink", "generic": "gray", "serve": "purple"}
KIND_COLORS = {"blocking": "red", "fyi": "blue", "harness": "purple", "relay": "gray"}
IMPACT_COLORS = {"high": "red", "medium": "orange", "low": "gray"}
QSTATUS_COLORS = {"Open": "red", "Applied": "green"}


def _sel(names: Iterable[str], colors: dict[str, str] | None = None) -> dict:
    return {"select": {"options": [({"name": n, "color": colors[n]} if colors and n in colors else {"name": n})
                                   for n in names]}}


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

    def g(name: str) -> dict:
        return p.get(name, {})

    full_title = r_title(g("Name"))
    prefix = full_title.split(" · ")[0]
    tid = r_rich(g("ID")) or (prefix if re.fullmatch(r"T-\d+", prefix) else "")
    title = full_title.split(" · ", 1)[1] if " · " in full_title and tid else full_title
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
        created=datetime.fromisoformat(page["created_time"].replace("Z", "+00:00"))
        if page.get("created_time") else None,
        page_id=page.get("id", ""),
    )


def TASKS_SCHEMA(agent_names: list[str]) -> dict:  # noqa: N802 - schema factory, name mirrors the constant style
    return {
        "Name": {"title": {}}, "Status": _sel(STATUS_ORDER, STATUS_COLORS), "ID": {"rich_text": {}},
        "Description": {"rich_text": {}},
        "Acceptance": {"rich_text": {}}, "Type": _sel(TASK_TYPES, TYPE_COLORS), "Importance": _sel(IMPORTANCES, IMPORTANCE_COLORS),
        "Size": _sel(SIZES, SIZE_COLORS), "Milestone": _sel(["M0", "M1", "M2", "M3", "M4"]),
        "Priority": {"number": {"format": "number"}}, "Agent": _sel(agent_names),
        "Model": {"rich_text": {}}, "Effort": {"rich_text": {}},
        "Depends On": {"rich_text": {}}, "Scope": {"rich_text": {}}, "Feedback": {"rich_text": {}},
        "PR": {"url": {}}, "Attempts": {"number": {"format": "number"}},
        "Review Rounds": {"number": {"format": "number"}}, "Claim Nonce": {"rich_text": {}},
        "Last Error": {"rich_text": {}}, "Flags": {"rich_text": {}}, "Started": {"date": {}},
    }


# ---------- questions ----------
QUESTION_FIELD_NAMES = {"status": "Status", "answer": "Answer", "needs_follow_up": "Needs Follow-up",
                        "text": "Question", "context": "Context", "answered_by": "Answered By"}
# A Notion title shows ~200 chars; the full question lives in Details (Q-129, Q-151, Q-153 were cut mid-word).
QUESTION_TITLE_CHARS = 200


def question_to_props(q: Question, *, task_page_id: str | None = None,
                      fields: Iterable[str] | None = None) -> dict:
    all_props = {
        "Question": p_title(_question_title(q)), "Details": p_rich(q.text),
        "ID": p_rich(q.id), "Kind": p_select(q.kind),
        "Context": p_rich(q.context), "Options": p_rich("\n".join(q.options)),
        "Proceeding With": p_rich(q.proceeding_with), "Impact": p_select(q.impact),
        "Task ID": p_rich(q.task_id), "Asked By": p_rich(q.asked_by), "Status": p_select(q.status),
        "Answer": p_rich(q.answer), "Needs Follow-up": p_checkbox(q.needs_follow_up),
        "Answered By": p_rich(q.answered_by),
    }
    if task_page_id:
        all_props["Task"] = p_relation([task_page_id])
    if fields is None:
        return all_props
    out = {QUESTION_FIELD_NAMES[f]: all_props[QUESTION_FIELD_NAMES[f]] for f in fields}
    if "text" in fields:
        out["Details"] = all_props["Details"]
    return out


def _question_title(q: Question) -> str:
    title = f"{q.id} · {q.text}"
    return title if len(title) <= QUESTION_TITLE_CHARS else title[:QUESTION_TITLE_CHARS - 1].rstrip() + "…"


def page_to_question(page: dict) -> Question:
    p = page["properties"]

    def g(name: str) -> dict:
        return p.get(name, {})

    title = r_title(g("Question"))
    qid = r_rich(g("ID")) or title.split(" · ")[0]
    text = r_rich(g("Details")) or (title.split(" · ", 1)[1] if " · " in title else title)
    context = r_rich(g("Context"))
    if not r_rich(g("Details")) and context.startswith("Full question: "):   # board without a Details column
        text, _, context = context[len("Full question: "):].partition("\n\n")
    return Question(
        id=qid, text=text, kind=r_select(g("Kind")) or "blocking", context=context,
        options=[o for o in r_rich(g("Options")).split("\n") if o],
        proceeding_with=r_rich(g("Proceeding With")), impact=r_select(g("Impact")) or "medium",
        task_id=r_rich(g("Task ID")), asked_by=r_rich(g("Asked By")), status=r_select(g("Status")) or "Open",
        answer=r_rich(g("Answer")), needs_follow_up=r_checkbox(g("Needs Follow-up")),
        answered_by=r_rich(g("Answered By")), page_id=page.get("id", ""),
    )


def TASK_RELATION(tasks_ds_id: str) -> dict:  # noqa: N802
    return {"Task": {"relation": {"data_source_id": tasks_ds_id, "type": "single_property", "single_property": {}}}}


def QUESTIONS_SCHEMA(tasks_ds_id: str | None = None) -> dict:  # noqa: N802
    """The Task relation is optional so the Questions table can be created before the Tasks table exists;
    init adds it afterwards with TASK_RELATION (it is cosmetic: the harness links by Task ID)."""
    schema = {
        "Question": {"title": {}}, "Details": {"rich_text": {}},
        "Status": _sel(["Open", "Applied"], QSTATUS_COLORS), "ID": {"rich_text": {}},
        "Kind": _sel(["blocking", "fyi", "harness", "relay"], KIND_COLORS),
        "Context": {"rich_text": {}}, "Options": {"rich_text": {}}, "Proceeding With": {"rich_text": {}},
        "Impact": _sel(["high", "medium", "low"], IMPACT_COLORS),
        "Task ID": {"rich_text": {}}, "Asked By": {"rich_text": {}},
        "Answer": {"rich_text": {}}, "Needs Follow-up": {"checkbox": {}}, "Answered By": {"rich_text": {}},
    }
    if tasks_ds_id:
        schema.update(TASK_RELATION(tasks_ds_id))
    return schema


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

    def g(name: str) -> dict:
        return p.get(name, {})

    return AgentRow(
        name=r_title(g("Name")), provider=r_select(g("Provider")), host=r_rich(g("Host")),
        status=r_select(g("Status")) or "idle", current_task=r_rich(g("Current Task")),
        last_heartbeat=r_date(g("Last Heartbeat")), cooldown_until=r_date(g("Cooldown Until")),
        runs=int(r_number(g("Runs")) or 0), tokens_in=int(r_number(g("Tokens In")) or 0),
        tokens_out=int(r_number(g("Tokens Out")) or 0), cost_usd=float(r_number(g("Cost USD")) or 0.0),
        cost_5h_usd=float(r_number(g("Cost 5h USD")) or 0.0), note=r_rich(g("Note")),
        page_id=page.get("id", ""),
    )


AGENTS_SCHEMA = {   # column order is what people see first: name, status, heartbeat, cooldown, task, spend
    "Name": {"title": {}},
    "Status": _sel(["idle", "running", "cooldown", "offline"], AGENT_STATUS_COLORS),
    "Last Heartbeat": {"date": {}}, "Cooldown Until": {"date": {}}, "Current Task": {"rich_text": {}},
    "Cost 5h USD": {"number": {"format": "number"}},
    "Provider": _sel(["claude", "codex", "antigravity", "gemini", "grok", "perplexity", "generic", "serve"], PROVIDER_COLORS),
    "Host": {"rich_text": {}},
    "Runs": {"number": {"format": "number"}}, "Tokens In": {"number": {"format": "number"}},
    "Tokens Out": {"number": {"format": "number"}}, "Cost USD": {"number": {"format": "number"}},
    "Note": {"rich_text": {}},
}
