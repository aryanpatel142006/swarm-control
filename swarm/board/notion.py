"""Notion API client (httpx, retries) and the NotionBoard implementation of the Board protocol."""
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
            body: dict = {}
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

    # ----- convenience wrappers -----
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
        out: dict = {}
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

    def decorate(self, kind: str, obj_id: str, *, emoji: str | None = None, icon_url: str | None = None,
                 cover_url: str | None = None) -> dict:
        """Set icon and cover on a page or database. kind is 'pages' or 'databases'."""
        body: dict = {}
        if emoji:
            body["icon"] = {"type": "emoji", "emoji": emoji}
        elif icon_url:
            body["icon"] = {"type": "external", "external": {"url": icon_url}}
        if cover_url:
            body["cover"] = {"type": "external", "external": {"url": cover_url}}
        if not body:
            return {}
        return self.request("PATCH", f"/{kind}/{obj_id}", json=body)

    def create_view(self, database_id: str, ds_id: str, name: str, kind: str, configuration: dict) -> dict:
        return self.request("POST", "/views", json={
            "database_id": database_id, "data_source_id": ds_id, "name": name, "type": kind,
            "configuration": configuration,
        })


# ---------- markdown → blocks ----------
def _rt(text: str) -> list[dict]:
    chunks = np._chunks(text)
    return [{"type": "text", "text": {"content": c}} for c in chunks] or [{"type": "text", "text": {"content": ""}}]


def markdown_to_blocks(md: str) -> list[dict]:
    blocks: list[dict] = []
    lines = md.splitlines()
    i = 0
    para: list[str] = []

    def flush():
        if para:
            blocks.append({"object": "block", "type": "paragraph",
                           "paragraph": {"rich_text": _rt("\n".join(para))}})
            para.clear()

    while i < len(lines):
        line = lines[i]
        if line.startswith("```"):
            flush()
            lang = line[3:].strip() or "plain text"
            i += 1
            code: list[str] = []
            while i < len(lines) and not lines[i].startswith("```"):
                code.append(lines[i])
                i += 1
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


# Icons and covers applied by `swarm init` to everything it creates (covers are Notion's own public images).
COVER_BASE = "https://www.notion.so/images/page-cover/"
DECOR = {
    "parent": ("🐝", COVER_BASE + "gradients_11.jpg"),
    "tasks": ("🗂️", COVER_BASE + "gradients_8.png"),
    "questions": ("❓", COVER_BASE + "gradients_10.jpg"),
    "agents": ("🤖", COVER_BASE + "gradients_5.png"),
    "status": ("📊", COVER_BASE + "gradients_4.png"),
}


def decorate_board(client: "NotionClient", ids: dict) -> list[str]:
    """Apply DECOR to the parent page, the three databases, and the status page. Returns what failed."""
    targets = [("pages", ids.get("parent_page_id"), DECOR["parent"]), ("databases", ids.get("tasks_db"), DECOR["tasks"]),
               ("databases", ids.get("questions_db"), DECOR["questions"]),
               ("databases", ids.get("agents_db"), DECOR["agents"]), ("pages", ids.get("status_page"), DECOR["status"])]
    failed = []
    for kind, obj_id, (emoji, cover) in targets:
        if not obj_id:
            continue
        try:
            client.decorate(kind, obj_id, emoji=emoji, cover_url=cover)
        except NotionError as e:
            failed.append(f"{kind}/{obj_id}: {e}")
    return failed


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
        page = {"id": row.page_id} if row.page_id else self._find_agent_page(row.name)
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
        questions = client.create_database(parent_page_id, "Swarm Questions",
                                           np.QUESTIONS_SCHEMA(ids["tasks_ds"]))
        ids["questions_db"], ids["questions_ds"] = questions["id"], questions["data_sources"][0]["id"]
        agents = client.create_database(parent_page_id, "Swarm Agents", np.AGENTS_SCHEMA)
        ids["agents_db"], ids["agents_ds"] = agents["id"], agents["data_sources"][0]["id"]
        page = client.create_child_page(parent_page_id, "Swarm Status", [
            {"object": "block", "type": "code",
             "code": {"language": "plain text", "rich_text": _rt("(no status yet)")}}])
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
                    "group_by": {"type": "select", "property_id": props[prop]["id"],
                                 "sort": {"type": "manual"}, "hide_empty_groups": False},
                    "card_layout": "compact"})
            qds = client.get_data_source(ids["questions_ds"])
            client.create_view(ids["questions_db"], ids["questions_ds"], "Open Questions", "board", {
                "type": "board",
                "group_by": {"type": "select", "property_id": qds["properties"]["Status"]["id"],
                             "sort": {"type": "manual"}, "hide_empty_groups": False},
                "card_layout": "compact"})
        except (NotionError, KeyError) as e:  # views API is new; fall back to manual instructions
            ids["views_ok"] = f"false: {e}"
        failed = decorate_board(client, ids)
        ids["decor_ok"] = "true" if not failed else "false: " + "; ".join(failed)
        return ids
