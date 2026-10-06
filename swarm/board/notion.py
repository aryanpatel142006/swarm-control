"""Notion API client (httpx, retries) and the NotionBoard implementation of the Board protocol."""
from __future__ import annotations

import hashlib
import os
import random
import re
import threading
import time
from pathlib import Path
from typing import Callable, Iterable

import httpx

from ..config import NotionIds
from ..models import AgentRow, Question, Status, Task, next_id, utcnow
from . import notion_props as np

API = "https://api.notion.com/v1"
VERSION = "2026-03-11"
MAX_RETRIES = 5
MAX_SLEEP = 300.0


def _retry_after_seconds(value) -> float | None:
    """Retry-After may be integer seconds or an HTTP-date."""
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        pass
    try:
        from email.utils import parsedate_to_datetime
        from datetime import datetime, timezone
        when = parsedate_to_datetime(str(value))
        return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())
    except (TypeError, ValueError):
        return None


class NotionError(RuntimeError):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(f"Notion {status} {code}: {message}")
        self.status, self.code, self.message = status, code, message


class NotionClient:
    def __init__(self, token: str, *, version: str = VERSION, transport=None,
                 sleep: Callable[[float], None] = time.sleep, timeout: float = 30.0):
        self._sleep = sleep
        self._transport, self._timeout = transport, timeout
        self._auth = {"Authorization": f"Bearer {token}", "Notion-Version": version}
        self._http = httpx.Client(base_url=API, transport=transport, timeout=timeout,
                                  headers={**self._auth, "Content-Type": "application/json"})

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
            wait = _retry_after_seconds(ra)
            if wait is None:
                wait = 2 ** attempt + random.random()
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

    def create_page(self, ds_id: str, props: dict, children: list | None = None, icon: dict | None = None) -> dict:
        body: dict = {"parent": {"type": "data_source_id", "data_source_id": ds_id}, "properties": props}
        if children:
            body["children"] = children[:100]
        if icon:
            body["icon"] = icon
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

    def create_database(self, parent_page_id: str, title: str, properties: dict, inline: bool = False) -> dict:
        return self.request("POST", "/databases", json={
            "parent": {"type": "page_id", "page_id": parent_page_id},
            "title": [{"type": "text", "text": {"content": title}}],
            "is_inline": inline,
            "initial_data_source": {"properties": properties},
        })

    def get_data_source(self, ds_id: str) -> dict:
        return self.request("GET", f"/data_sources/{ds_id}")

    def upload_file(self, path: Path, content_type: str = "image/png") -> str:
        """Two-step Notion file upload; returns the file_upload id to reference from icon/cover/blocks."""
        created = self.request("POST", "/file_uploads", json={"mode": "single_part", "filename": path.name,
                                                                "content_type": content_type})
        fid = created["id"]
        with httpx.Client(base_url=API, transport=self._transport, timeout=max(self._timeout, 120),
                          headers=self._auth) as raw, path.open("rb") as f:
            resp = raw.post(f"/file_uploads/{fid}/send", files={"file": (path.name, f, content_type)})
        if resp.status_code >= 400:
            raise NotionError(resp.status_code, "upload_failed", resp.text[:200])
        return fid

    def decorate(self, kind: str, obj_id: str, *, emoji: str | None = None, icon_url: str | None = None,
                 cover_url: str | None = None, icon_upload: str | None = None,
                 cover_upload: str | None = None) -> dict:
        """Set icon and cover on a page or database. kind is 'pages' or 'databases'."""
        body: dict = {}
        if icon_upload:
            body["icon"] = {"type": "file_upload", "file_upload": {"id": icon_upload}}
        elif emoji:
            body["icon"] = {"type": "emoji", "emoji": emoji}
        elif icon_url:
            body["icon"] = {"type": "external", "external": {"url": icon_url}}
        if cover_upload:
            body["cover"] = {"type": "file_upload", "file_upload": {"id": cover_upload}}
        elif cover_url:
            body["cover"] = {"type": "external", "external": {"url": cover_url}}
        if not body:
            return {}
        return self.request("PATCH", f"/{kind}/{obj_id}", json=body)

    def create_view(self, database_id: str, ds_id: str, name: str, kind: str, configuration: dict) -> dict:
        """A filter must sit at the top level of the view: nested in `configuration` Notion silently drops it
        (found Sep 28 2026: "Needs you" showed every task)."""
        configuration = dict(configuration)
        body = {"database_id": database_id, "data_source_id": ds_id, "name": name, "type": kind}
        if "filter" in configuration:
            body["filter"] = configuration.pop("filter")
        body["configuration"] = configuration
        return self.request("POST", "/views", json=body)


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


ASSETS_DIR = Path(__file__).resolve().parent.parent / "assets"   # shipped with the package
ASSET_KEY = {"parent": "board", "tasks": "tasks", "questions": "questions", "agents": "agents", "status": "status"}


# Public URL of the packaged art. Notion renders external images reliably and they never expire, unlike
# API file uploads, which were observed to 404 from Notion's storage a few hours after attaching.
ASSETS_REPO_URL = "https://raw.githubusercontent.com/aryanpatel142006/swarm-control/"


def _assets_commit() -> str:
    """Short hash of the last commit that touched the packaged art; empty outside a git checkout."""
    import subprocess
    try:
        r = subprocess.run(["git", "-C", str(ASSETS_DIR), "log", "-1", "--format=%h", "--", str(ASSETS_DIR)],
                           capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return r.stdout.strip() if r.returncode == 0 else ""


def default_assets_base(git=_assets_commit) -> str:
    """Where the packaged art is served from. The assets' commit hash sits in the PATH: Notion caches external
    images by path and ignores the query string, so a `?v=` alone left old art on screen."""
    explicit = os.environ.get("SWARM_ASSETS_URL")
    if explicit:
        return explicit
    ref = os.environ.get("SWARM_ASSETS_REF") or git() or "main"
    return f"{ASSETS_REPO_URL}{ref}/swarm/assets/"


ASSETS_URL_BASE = default_assets_base()


def decorate_object(client: "NotionClient", kind: str, obj_id: str, key: str, assets_dir: Path = ASSETS_DIR,
                    assets_url: str | None = ASSETS_URL_BASE) -> str:
    """Custom icon + banner: linked from the public repo, else uploaded, else emoji + Notion gradient."""
    emoji, cover_url = DECOR[key]
    name = ASSET_KEY[key]
    if assets_url:
        icon_url = asset_url(f"icon-{name}.png", assets_url)
        try:
            client.decorate(kind, obj_id, icon_url=icon_url, cover_url=asset_url(f"banner-{name}.png", assets_url))
            return "external"
        except NotionError as e:
            if "cover" in e.message.lower():   # inline databases take an icon but refuse covers
                try:
                    client.decorate(kind, obj_id, icon_url=icon_url)
                    return "external"
                except NotionError:
                    pass
    icon_png = Path(assets_dir) / f"icon-{name}.png"
    banner_png = Path(assets_dir) / f"banner-{name}.png"
    if icon_png.exists() and banner_png.exists():
        try:
            client.decorate(kind, obj_id, icon_upload=client.upload_file(icon_png),
                            cover_upload=client.upload_file(banner_png))
            return "upload"
        except (NotionError, OSError):
            pass
    client.decorate(kind, obj_id, emoji=emoji, cover_url=cover_url)
    return "fallback"


def notion_url(obj_id: str) -> str:
    return "https://www.notion.so/" + obj_id.replace("-", "")


def _links(pairs: list[tuple[str, str]]) -> list[dict]:
    """Rich text 'A · B · C' where each name links to a Notion page or database id."""
    out: list[dict] = []
    for i, (label, obj_id) in enumerate(pairs):
        if i:
            out.append({"type": "text", "text": {"content": " · "}})
        out.append({"type": "text", "text": {"content": label,
                                             "link": {"url": f"https://www.notion.so/{obj_id.replace('-', '')}"}}})
    return out


def _heading(text: str) -> dict:
    return {"object": "block", "type": "heading_2", "heading_2": {"rich_text": _rt(text)}}


def decorate_board(client: "NotionClient", ids: dict, assets_dir: Path = ASSETS_DIR,
                   assets_url: str | None = ASSETS_URL_BASE) -> list[str]:
    """Decorate the parent page, the three databases, and the status page. Returns what failed entirely."""
    targets = [("pages", ids.get("home_page") or ids.get("parent_page_id"), "parent"),
               ("databases", ids.get("tasks_db"), "tasks"), ("databases", ids.get("questions_db"), "questions"),
               ("databases", ids.get("agents_db"), "agents")]
    if ids.get("status_page") and ids["status_page"] != ids.get("home_page"):
        targets.append(("pages", ids["status_page"], "status"))
    failed = []
    for kind, obj_id, key in targets:
        if not obj_id:
            continue
        try:
            decorate_object(client, kind, obj_id, key, assets_dir, assets_url)
        except NotionError as e:
            failed.append(f"{kind}/{obj_id}: {e}")
    return failed


_ASSET_HASHES: dict[str, str] = {}


def asset_url(filename: str, assets_url: str | None = None) -> str | None:
    """Public URL of a packaged image, with a content hash so Notion never serves a stale cached copy."""
    base = ASSETS_URL_BASE if assets_url is None else assets_url
    if not base:
        return None
    if filename not in _ASSET_HASHES:
        path = ASSETS_DIR / filename
        try:
            _ASSET_HASHES[filename] = hashlib.md5(path.read_bytes()).hexdigest()[:8]
        except OSError:
            _ASSET_HASHES[filename] = "0"
    return f"{base}{filename}?v={_ASSET_HASHES[filename]}"


def row_icon(kind: str, value: str) -> dict | None:
    """Icon object for a task type, question kind, or agent row; None when no public art is configured."""
    if kind == "question" and value not in ("blocking", "fyi"):
        value = "fyi"   # harness / relay notes reuse the fyi art
    name = {"type": f"type-{value}", "question": f"q-{value}", "agent": "serve" if value == "serve" else "agent"}[kind]
    url = asset_url(f"icon-{name}.png")
    return {"type": "external", "external": {"url": url}} if url else None


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
        self._details_ok: bool | None = None   # Questions has a Details column (checked once per process)
        # Highest id this process handed out per prefix. Notion's query is eventually consistent: a page created a
        # moment ago may be missing from the next query, so two questions filed in one runner publish (T-096's
        # harness note and its fyi) both became Q-209 and `swarm answer Q-209` closed only one (Oct 6).
        self._issued: dict[str, int] = {}
        self._id_lock = threading.Lock()

    # ----- tasks -----
    def _allocate(self, prefix: str, ds: str) -> str:
        with self._id_lock:
            rows = self.c.query(ds)
            n = int(next_id(prefix, (np.r_rich(r["properties"].get("ID", {})) for r in rows)).split("-")[1])
            n = max(n, self._issued.get(prefix, 0) + 1)
            self._issued[prefix] = n
            return f"{prefix}-{n:03d}"

    def next_task_id(self) -> str:
        return self._allocate("T", self.ids.tasks_ds)

    def create_task(self, task: Task) -> Task:
        if not task.id:
            task.id = self.next_task_id()
        children = markdown_to_blocks(task.description) if task.description else None
        page = self.c.create_page(self.ids.tasks_ds, np.task_to_props(task), children, icon=row_icon("type", task.type))
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
        return self._allocate("Q", self.ids.questions_ds)

    def _dedupe_question_id(self, q: Question) -> None:
        """Another process (serve, the other laptop's runner) may have taken the same id meanwhile: the row created
        later moves to a fresh id. Best effort; a query that cannot see the other row yet cannot help."""
        for _ in range(3):
            rows = self.c.query(self.ids.questions_ds, filter={"property": "ID", "rich_text": {"equals": q.id}})
            if len(rows) < 2:
                return
            rows.sort(key=lambda r: (str(r.get("created_time") or ""), str(r.get("id"))))
            if rows[0].get("id") == q.page_id:
                return
            q.id = self.next_question_id()
            props = np.question_to_props(q)
            self.c.update_page(q.page_id, {"ID": props["ID"], "Question": props["Question"]})

    def _questions_have_details(self) -> bool:
        """Boards made before Oct 6 2026 have no Details column: add it once (the integration owns the schema).
        If that fails, the full text goes into Context instead, so it is never lost to the title's length."""
        if self._details_ok is None:
            try:
                props = (self.c.get_data_source(self.ids.questions_ds) or {}).get("properties") or {}
                if "Details" not in props:
                    self.c.request("PATCH", f"/data_sources/{self.ids.questions_ds}",
                                   json={"properties": {"Details": {"rich_text": {}}}})
                self._details_ok = True
            except NotionError:
                self._details_ok = False
        return self._details_ok

    def _question_props(self, q: Question, **kw) -> dict:
        props = np.question_to_props(q, **kw)
        if "Details" in props and not self._questions_have_details():
            props.pop("Details")
            if len(q.text) > np.QUESTION_TITLE_CHARS - len(q.id) - 3 and "Context" in props:
                props["Context"] = np.p_rich(f"Full question: {q.text}\n\n{q.context}".strip())
        return props

    def create_question(self, q: Question) -> Question:
        if not q.id:
            q.id = self.next_question_id()
        task_page = None
        if q.task_id:
            t = self.get_task(q.task_id)
            task_page = t.page_id if t else None
        page = self.c.create_page(self.ids.questions_ds, self._question_props(q, task_page_id=task_page),
                                  icon=row_icon("question", q.kind))
        q.page_id = page["id"]
        try:
            self._dedupe_question_id(q)
        except NotionError:
            pass
        return q

    def list_questions(self, *, status: str | None = None) -> list[Question]:
        flt = _sel_filter("Status", [status]) if status else None
        qs = [np.page_to_question(r) for r in self.c.query(self.ids.questions_ds, filter=flt)]
        qs.sort(key=lambda q: q.id)
        return qs

    def update_question(self, q: Question, fields: Iterable[str]) -> Question:
        self.c.update_page(q.page_id, self._question_props(q, fields=fields))
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
            created = self.c.create_page(self.ids.agents_ds, np.agent_to_props(row), icon=row_icon("agent", row.name))
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

    def write_headline(self, text: str, color: str) -> None:
        if self.ids.headline_block:
            self.c.update_block(self.ids.headline_block, {"callout": {"rich_text": _rt(text), "color": color}})

    # ----- init -----
    @staticmethod
    def init(client: NotionClient, parent_page_id: str, agent_names: list[str], project: str = "") -> dict:
        """One dashboard page per project under the parent: how-to, Agents, Questions for you, then two columns:
        a tile that opens the Tasks board (a full page next to the dashboard, so a task opens full-screen) and
        the live Status block. Layout as the user arranged it by hand on Sep 28, 2026."""
        ids: dict = {"parent_page_id": parent_page_id}
        label = project or "project"
        icon_url = asset_url("icon-board.png")
        home = client.create_child_page(parent_page_id, f"Swarm · {label}", [
            {"object": "block", "type": "callout", "callout": {   # the Now banner: serve rewrites text and colour
                "icon": ({"type": "external", "external": {"url": icon_url}} if icon_url
                         else {"type": "emoji", "emoji": "🐝"}),
                "color": "gray_background",
                "rich_text": _rt("Waiting for swarm serve: this line turns green, yellow or red with what matters now.")}},
            {"object": "block", "type": "toggle", "toggle": {
                "rich_text": _rt("How to use this page"),
                "children": [{"object": "block", "type": "numbered_list_item",
                              "numbered_list_item": {"rich_text": _rt(line)}} for line in (
                    "The line above says what matters now: green all good, yellow something waits on you, red a risk.",
                    "Agents: who is working and who is offline.",
                    "Questions for you: type in Answer; it is the only thing agents wait on.",
                    "Tasks opens the full board. Drag a card to Cut to drop it, or to Ready to retry it.",
                    "Status (bottom right) is the full report, refreshed every few minutes.")]}}])
        blocks = client.list_children(home["id"])
        ids["headline_block"] = next((b["id"] for b in blocks if b.get("type") == "callout"), "")
        ids["home_page"] = ids["status_page"] = home["id"]

        client.append_blocks(home["id"], [_heading("Agents")])
        agents = client.create_database(home["id"], "Agents", np.AGENTS_SCHEMA, inline=True)
        ids["agents_db"], ids["agents_ds"] = agents["id"], agents["data_sources"][0]["id"]
        client.append_blocks(home["id"], [_heading("Questions for you")])
        questions = client.create_database(home["id"], "Questions", np.QUESTIONS_SCHEMA(), inline=True)
        ids["questions_db"], ids["questions_ds"] = questions["id"], questions["data_sources"][0]["id"]
        # the board is a full page next to the dashboard (the API cannot place a database inside a column)
        tasks = client.create_database(parent_page_id, f"Tasks · {label}", np.TASKS_SCHEMA(agent_names), inline=False)
        ids["tasks_db"], ids["tasks_ds"] = tasks["id"], tasks["data_sources"][0]["id"]
        try:   # link Questions → Tasks now that Tasks exists (cosmetic; the harness links by Task ID)
            client.request("PATCH", f"/data_sources/{ids['questions_ds']}",
                           json={"properties": np.TASK_RELATION(ids["tasks_ds"])})
        except NotionError:
            pass
        cols = client.append_blocks(home["id"], [{"object": "block", "type": "column_list", "column_list": {"children": [
            {"object": "block", "type": "column", "column": {"children": [
                _heading("Tasks"),
                {"object": "block", "type": "link_to_page",
                 "link_to_page": {"type": "database_id", "database_id": ids["tasks_db"]}}]}},
            {"object": "block", "type": "column", "column": {"children": [
                _heading("Status"),
                {"object": "block", "type": "code",
                 "code": {"language": "plain text", "rich_text": _rt("(no status yet: start swarm serve)")}}]}}]}}])
        ids["status_block"] = ""
        try:
            col_list = (cols.get("results") or [{}])[0].get("id", "")
            for col in client.list_children(col_list):
                for b in client.list_children(col["id"]):
                    if b.get("type") == "code":
                        ids["status_block"] = b["id"]
        except NotionError:
            pass

        ids["views_ok"] = "true"
        try:
            props = client.get_data_source(ids["tasks_ds"])["properties"]
            for name, prop in (("Board", "Status"), ("By agent", "Agent"), ("By milestone", "Milestone")):
                client.create_view(ids["tasks_db"], ids["tasks_ds"], name, "board", {
                    "type": "board",
                    "group_by": {"type": "select", "property_id": props[prop]["id"],
                                 "sort": {"type": "manual"}, "hide_empty_groups": False},
                    "card_layout": "compact"})
            client.create_view(ids["tasks_db"], ids["tasks_ds"], "Needs you", "table", {
                "type": "table",
                "filter": {"or": [{"property": props["Status"]["id"], "select": {"equals": "Blocked"}},
                                  {"property": props["Status"]["id"], "select": {"equals": "Failed"}}]}})
            qprops = client.get_data_source(ids["questions_ds"])["properties"]
            client.create_view(ids["questions_db"], ids["questions_ds"], "All", "table", {"type": "table"})
            ids["questions_open_filter"] = qprops["Status"]["id"]   # applied to the default view by order_columns
        except (NotionError, KeyError) as e:  # views API is new; fall back to manual instructions
            ids["views_ok"] = f"false: {e}"

        descriptions = {
            "tasks_db": _links([("Dashboard", home["id"])])
            + _rt("  ·  Board: where every task is. By milestone: progress per milestone. Needs you: Blocked and Failed. "
                  "Drag a card to Cut to drop it, or to Ready to retry it."),
            "questions_db": _rt("Type your answer in Answer. Tick Needs follow-up when an fyi decision must change."),
            "agents_db": _rt("One row per agent: status, current task, spend in the last 5 hours."),
        }
        for key, rich in descriptions.items():
            try:
                client.request("PATCH", f"/databases/{ids[key]}", json={"description": rich})
            except NotionError:
                pass
        failed = decorate_board(client, ids)
        ids["decor_ok"] = "true" if not failed else "false: " + "; ".join(failed)
        try:
            order_columns(client, ids)
            ids["columns_ok"] = "true"
        except (NotionError, KeyError) as e:
            ids["columns_ok"] = f"false: {e}"
        return ids


# Column order is a per-view setting (the API ignores schema order), so every table view gets the list people
# asked for and each board view gets its card fields. Anything not listed keeps its place after these.
COLUMN_ORDER = {
    "tasks": ["Name", "Status", "Agent", "Importance", "Type", "Size", "Milestone", "Model", "PR", "Attempts",
              "Review Rounds", "Depends On", "Feedback", "Last Error", "Flags", "Description", "Acceptance", "Scope",
              "Priority", "Effort", "Started", "ID", "Claim Nonce"],
    "questions": ["Question", "Status", "Kind", "Impact", "Answer", "Needs Follow-up", "Task ID", "Task", "Options",
                  "Proceeding With", "Context", "Asked By", "ID"],
    "agents": ["Name", "Status", "Last Heartbeat", "Cooldown Until", "Current Task", "Cost 5h USD", "Provider", "Host",
               "Runs", "Tokens In", "Tokens Out", "Cost USD", "Note"],
}
# what a person reads; the rest stays in the data (and on each card's page) but off the table
HIDDEN_COLUMNS = {
    "tasks": {"Claim Nonce", "Description", "Acceptance", "Scope", "Priority", "Effort", "Started", "ID", "Flags",
              "Depends On", "Feedback", "Last Error", "Review Rounds"},
    "questions": {"ID", "Context", "Options", "Proceeding With", "Asked By", "Task"},
    "agents": {"Provider", "Host", "Runs", "Tokens In", "Tokens Out", "Note"},
}
# card fields per board view: never repeat the field the board is grouped by
BOARD_CARD_FIELDS = {"tasks": {"Board": ["Agent", "Importance", "Type", "Size"],
                               "By agent": ["Status", "Importance", "Type", "Size"],
                               "By milestone": ["Status", "Agent", "Type"]}}
DEFAULT_VIEW_NAMES = {"tasks": "All tasks", "agents": "All agents"}


def order_columns(client: "NotionClient", ids: dict) -> None:
    for key in ("tasks", "questions", "agents"):
        db, ds = ids.get(f"{key}_db"), ids.get(f"{key}_ds")
        if not db or not ds:
            continue
        props = client.get_data_source(ds)["properties"]
        wanted = [n for n in COLUMN_ORDER[key] if n in props] + [n for n in props if n not in COLUMN_ORDER[key]]
        hidden = HIDDEN_COLUMNS.get(key, set())
        table = [{"property_id": props[n]["id"], "visible": n not in hidden} for n in wanted]
        card_sets = {name: [{"property_id": props[n]["id"], "visible": True} for n in fields if n in props]
                     for name, fields in BOARD_CARD_FIELDS.get(key, {}).items()}
        for row in client.request("GET", f"/views?database_id={db}").get("results", []):
            view = client.request("GET", f"/views/{row['id']}")
            cards = card_sets.get(view.get("name", ""))
            if view.get("type") == "table":
                cfg = {"type": "table", "properties": table}
                if view.get("name") == "Default view" and key in DEFAULT_VIEW_NAMES:
                    client.request("PATCH", f"/views/{row['id']}", json={"name": DEFAULT_VIEW_NAMES[key]})
                if key == "questions" and view.get("name") == "Default view" and ids.get("questions_open_filter"):
                    client.request("PATCH", f"/views/{row['id']}", json={
                        "name": "Open", "filter": {"property": ids["questions_open_filter"], "select": {"equals": "Open"}}})
            elif view.get("type") == "board" and cards:
                cfg = dict(view.get("configuration") or {"type": "board"})
                cfg["properties"] = cards
            else:
                continue
            client.request("PATCH", f"/views/{row['id']}", json={"configuration": cfg})
