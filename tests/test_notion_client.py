import json

import httpx

from swarm.board import notion_props as np
import pytest

from swarm.board.notion import NotionBoard, NotionClient, NotionError, markdown_to_blocks
from swarm.config import NotionIds
from swarm.models import AgentRow, Question, Status, Task


def make_client(handler, sleeps=None):
    transport = httpx.MockTransport(handler)
    sleeps = sleeps if sleeps is not None else []
    return NotionClient("tok", transport=transport, sleep=lambda s: sleeps.append(s)), sleeps


def test_headers_and_me():
    seen = {}

    def handler(req: httpx.Request):
        seen["auth"] = req.headers["Authorization"]
        seen["ver"] = req.headers["Notion-Version"]
        return httpx.Response(200, json={"object": "user", "id": "u1"})

    client, _ = make_client(handler)
    assert client.me()["id"] == "u1"
    assert seen == {"auth": "Bearer tok", "ver": "2026-03-11"}


def test_retries_on_429_with_retry_after():
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(429, headers={"Retry-After": "2"}, json={"code": "rate_limited", "message": "slow"})
        return httpx.Response(200, json={"ok": True})

    client, sleeps = make_client(handler)
    assert client.request("GET", "/users/me") == {"ok": True}
    assert calls["n"] == 3 and sleeps[:2] == [2.0, 2.0]


def test_retry_after_long_wait_is_capped():
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "900"}, json={})
        return httpx.Response(200, json={"ok": True})

    client, sleeps = make_client(handler)
    client.request("GET", "/users/me")
    assert sleeps == [300.0]


def test_gives_up_after_five_retries():
    def handler(req):
        return httpx.Response(429, headers={"Retry-After": "1"}, json={"code": "rate_limited", "message": "no"})

    client, sleeps = make_client(handler)
    with pytest.raises(NotionError) as ei:
        client.request("GET", "/users/me")
    assert ei.value.status == 429 and len(sleeps) == 5


def test_400_raises_without_retry():
    def handler(req):
        return httpx.Response(400, json={"code": "validation_error", "message": "bad"})

    client, sleeps = make_client(handler)
    with pytest.raises(NotionError, match="bad"):
        client.request("POST", "/pages", json={})
    assert sleeps == []


def test_query_paginates():
    pages = {"n": 0}

    def handler(req):
        body = json.loads(req.content)
        pages["n"] += 1
        if body.get("start_cursor") is None:
            return httpx.Response(200, json={"results": [{"id": "a"}], "has_more": True, "next_cursor": "c1"})
        return httpx.Response(200, json={"results": [{"id": "b"}], "has_more": False, "next_cursor": None})

    client, _ = make_client(handler)
    rows = client.query("ds1", filter={"x": 1})
    assert [r["id"] for r in rows] == ["a", "b"] and pages["n"] == 2


def test_markdown_to_blocks():
    blocks = markdown_to_blocks("## Report\n\nline one\nline two\n\n```json\n{\"a\":1}\n```\n")
    assert blocks[0]["type"] == "heading_2"
    assert blocks[1]["type"] == "paragraph"
    assert "line one" in blocks[1]["paragraph"]["rich_text"][0]["text"]["content"]
    assert blocks[2]["type"] == "code" and blocks[2]["code"]["language"] == "json"


def _board_with_fake_notion():
    """A fake Notion that stores pages by data source and supports the calls NotionBoard makes."""
    store = {"ds-tasks": {}, "ds-q": {}, "ds-agents": {}, "blocks": {}}
    counter = {"n": 0}

    def match(page, f):
        if "and" in f:
            return all(match(page, x) for x in f["and"])
        if "or" in f:
            return any(match(page, x) for x in f["or"])
        prop = page["properties"][f["property"]]
        if "select" in f:
            sel = prop.get("select")
            return (sel or {}).get("name") == f["select"].get("equals")
        if "rich_text" in f:
            text = "".join(x["text"]["content"] for x in prop.get("rich_text", []))
            if "equals" in f["rich_text"]:
                return text == f["rich_text"]["equals"]
            if "is_not_empty" in f["rich_text"]:
                return bool(text)
        if "title" in f:
            text = "".join(x["text"]["content"] for x in prop.get("title", []))
            return text == f["title"].get("equals")
        return True

    def find(pid):
        for ds in ("ds-tasks", "ds-q", "ds-agents"):
            if pid in store[ds]:
                return store[ds][pid]
        return None

    def handler(req: httpx.Request):
        path = req.url.path
        body = json.loads(req.content) if req.content else {}
        if req.method == "POST" and path.endswith("/query"):
            ds = path.split("/")[3]
            results = list(store[ds].values())
            if body.get("filter"):
                results = [p for p in results if match(p, body["filter"])]
            return httpx.Response(200, json={"results": results, "has_more": False, "next_cursor": None})
        if req.method == "POST" and path == "/v1/pages":
            counter["n"] += 1
            ds = body["parent"]["data_source_id"]
            page = {"id": f"pg{counter['n']}", "properties": body["properties"], "icon": body.get("icon"),
                    "last_edited_time": "2026-10-10T16:00:00.000Z"}
            store[ds][page["id"]] = page
            return httpx.Response(200, json=page)
        if req.method == "PATCH" and path.startswith("/v1/pages/"):
            page = find(path.split("/")[3])
            if page is None:
                return httpx.Response(404, json={"message": "nope"})
            page["properties"].update(body["properties"])
            return httpx.Response(200, json=page)
        if req.method == "GET" and path.startswith("/v1/pages/"):
            page = find(path.split("/")[3])
            return httpx.Response(200, json=page) if page else httpx.Response(404, json={"message": "nope"})
        if req.method == "PATCH" and path.startswith("/v1/blocks/") and path.endswith("/children"):
            store["blocks"].setdefault(path.split("/")[3], []).extend(body["children"])
            return httpx.Response(200, json={"results": body["children"]})
        if req.method == "PATCH" and path.startswith("/v1/blocks/"):
            store["blocks"][path.split("/")[3]] = body
            return httpx.Response(200, json=body)
        return httpx.Response(500, json={"message": f"unhandled {req.method} {path}"})

    client = NotionClient("tok", transport=httpx.MockTransport(handler), sleep=lambda s: None)
    ids = NotionIds(tasks_ds="ds-tasks", questions_ds="ds-q", agents_ds="ds-agents",
                    status_page="sp", status_block="sb")
    return NotionBoard(client, ids), store


def test_board_task_crud():
    board, store = _board_with_fake_notion()
    t = board.create_task(Task(id="", title="first", status=Status.READY, agent="claude-a"))
    assert t.id == "T-001" and t.page_id == "pg1"
    t2 = board.create_task(Task(id="", title="second"))
    assert t2.id == "T-002"
    assert [x.id for x in board.list_tasks(status=[Status.READY], agent=["claude-a"])] == ["T-001"]
    t.status = Status.RUNNING
    board.update_task(t, ["status"])
    assert board.get_task("T-001").status is Status.RUNNING
    board.append_task_report(t, "Report — attempt 1", "hello")
    assert store["blocks"]["pg1"][0]["type"] == "heading_2"


def test_board_questions_agents_status():
    board, store = _board_with_fake_notion()
    t = board.create_task(Task(id="", title="first"))
    q = board.create_question(Question(id="", text="why?", task_id=t.id))
    assert q.id == "Q-001"
    assert board.list_questions(status="Open")[0].text == "why?"
    q.answer = "because"
    board.update_question(q, ["answer"])
    assert board.list_questions()[0].answer == "because"
    row = board.upsert_agent(AgentRow(name="claude-a", provider="claude"))
    row.runs = 2
    board.upsert_agent(row)
    assert board.get_agent("claude-a").runs == 2 and len(board.list_agents()) == 1
    board.write_status_page("status text")
    assert store["blocks"]["sb"]["code"]["rich_text"][0]["text"]["content"] == "status text"


def test_init_builds_dashboard_with_tasks_and_status_as_own_pages():
    """Dashboard: how-to with links, Agents table, Questions for you. Tasks and Status are their own pages ("tabs")."""
    decorated, views, dbs, appended, pages, descriptions, relations = {}, [], [], {}, [], {}, []
    view_store, ordered = {}, {}
    counter = {"n": 0}

    def handler(req: httpx.Request):
        path = req.url.path
        body = json.loads(req.content) if req.content else {}
        if req.method == "POST" and path == "/v1/databases":
            counter["n"] += 1
            dbs.append(body)
            return httpx.Response(200, json={"id": f"db{counter['n']}", "data_sources": [{"id": f"ds{counter['n']}"}]})
        if req.method == "POST" and path == "/v1/pages":
            pages.append(body)
            return httpx.Response(200, json={"id": "home" if len(pages) == 1 else "status"})
        if req.method == "GET" and path == "/v1/blocks/status/children":
            return httpx.Response(200, json={"results": [{"id": "nav", "type": "paragraph"}, {"id": "code1", "type": "code"}]})
        if req.method == "PATCH" and path.startswith("/v1/data_sources/"):
            relations.append(body["properties"])
            return httpx.Response(200, json=body)
        if req.method == "PATCH" and path.endswith("/children"):
            appended.setdefault(path.split("/")[3], []).append(body)
            return httpx.Response(200, json={"results": body["children"]})
        if req.method == "PATCH" and path.startswith("/v1/databases/") and "description" in body:
            descriptions[path.split("/")[3]] = body["description"]
            return httpx.Response(200, json=body)
        if req.method == "GET" and path.startswith("/v1/data_sources/"):
            n = path.split("/")[3][-1]
            schema = {"1": np.AGENTS_SCHEMA, "2": np.QUESTIONS_SCHEMA("ds3"), "3": np.TASKS_SCHEMA(["claude-a"])}[n]
            props = {name: {"id": "title" if "title" in spec else name} for name, spec in schema.items()}
            return httpx.Response(200, json={"properties": props})
        if req.method == "POST" and path == "/v1/views":
            views.append((body["database_id"], body["name"], body["type"]))
            view_store[f"v{len(views)}"] = {"id": f"v{len(views)}", "name": body["name"], "type": body["type"],
                                            "database_id": body["database_id"], "configuration": body["configuration"]}
            return httpx.Response(200, json={"id": f"v{len(views)}"})
        if req.method == "GET" and path == "/v1/views":
            db = req.url.params["database_id"]
            listed = [{"id": vid} for vid, v in view_store.items() if v["database_id"] == db]
            listed.append({"id": f"default-{db}"})
            return httpx.Response(200, json={"results": listed})
        if req.method == "GET" and path.startswith("/v1/views/"):
            vid = path.split("/")[3]
            v = view_store.get(vid) or {"id": vid, "name": "Default view", "type": "table", "configuration": {"type": "table"}}
            return httpx.Response(200, json=v)
        if req.method == "PATCH" and path.startswith("/v1/views/"):
            ordered[path.split("/")[3]] = body["configuration"]
            return httpx.Response(200, json=body)
        if req.method == "PATCH" and (path.startswith("/v1/databases/") or path.startswith("/v1/pages/")):
            decorated[path.split("/")[3]] = body
            return httpx.Response(200, json=body)
        return httpx.Response(500, json={"message": f"unhandled {req.method} {path}"})

    client = NotionClient("tok", transport=httpx.MockTransport(handler), sleep=lambda s: None)
    ids = NotionBoard.init(client, "parent", ["claude-a"], project="demo")
    # column order is a per-view setting: every table view gets it, boards get their card fields
    assert ids["columns_ok"] == "true"
    agents_default = ordered["default-db1"]["properties"]
    assert [p["property_id"] for p in agents_default[:6]] == ["title", "Status", "Last Heartbeat", "Cooldown Until",
                                                               "Current Task", "Cost 5h USD"]
    tasks_default = ordered["default-db3"]["properties"]
    assert [p["property_id"] for p in tasks_default[:3]] == ["title", "Status", "Agent"]
    assert {p["property_id"]: p["visible"] for p in tasks_default}["Claim Nonce"] is False
    assert [p["property_id"] for p in ordered["default-db2"]["properties"][:2]] == ["title", "Status"]
    assert ordered["v1"]["type"] == "board" and "group_by" in ordered["v1"]   # board keeps its grouping
    assert [p["property_id"] for p in ordered["v1"]["properties"]] == ["Agent", "Importance", "Type", "Size"]
    # the dashboard page, then the Status page under it
    assert [pg["parent"]["page_id"] for pg in pages] == ["parent", "home"]
    assert pages[0]["properties"]["title"][0]["text"]["content"] == "Swarm · demo"
    assert pages[1]["properties"]["title"][0]["text"]["content"] == "Status"
    assert [b["type"] for b in pages[1]["children"]] == ["paragraph", "code"]   # nav line, then the live block
    assert ids["home_page"] == "home" and ids["status_page"] == "status" and ids["status_block"] == "code1"
    # dashboard order: Agents, Questions for you, then Tasks as a full page (opening a task fills the screen)
    assert [(d["title"][0]["text"]["content"], d["is_inline"], d["parent"]["page_id"]) for d in dbs] == \
        [("Agents", True, "home"), ("Questions", True, "home"), ("Tasks", False, "home")]
    heads = [b["heading_2"]["rich_text"][0]["text"]["content"]
             for call in appended["home"] for b in call["children"] if b["type"] == "heading_2"]
    assert heads == ["Agents", "Questions for you"]
    assert relations and "Task" in relations[0] and relations[0]["Task"]["relation"]["data_source_id"] == "ds3"
    assert "Task" not in dbs[1]["initial_data_source"]["properties"]   # relation added once Tasks exists
    callout_calls = [call for call in appended["home"] if call["children"][0]["type"] == "callout"]
    assert callout_calls and callout_calls[0]["position"] == {"type": "start"}
    links = [seg["text"]["link"]["url"] for seg in callout_calls[0]["children"][0]["callout"]["rich_text"]
             if seg["text"].get("link")]
    assert any("db3" in u for u in links) and any("status" in u for u in links)
    # views: Board, By agent and Needs you on Tasks; Open on Questions
    assert ids["views_ok"] == "true"
    assert views == [("db3", "Board", "board"), ("db3", "By agent", "board"), ("db3", "Needs you", "table"),
                     ("db2", "Open", "table")]
    # help text and links where people look
    task_desc = "".join(seg["text"]["content"] for seg in descriptions["db3"])
    assert "Dashboard" in task_desc and "Cut" in task_desc
    assert any(seg["text"].get("link") for seg in descriptions["db3"])
    assert "Answer" in "".join(seg["text"]["content"] for seg in descriptions["db2"])
    assert ids["decor_ok"] == "true"
    for key in ("home", "status", "db1", "db2", "db3"):
        assert decorated[key]["icon"]["type"] == "external", key
    assert "icon-board.png?v=" in decorated["home"]["icon"]["external"]["url"]
    assert "icon-status.png?v=" in decorated["status"]["icon"]["external"]["url"]


def test_tasks_schema_puts_status_right_after_the_title():
    from swarm.board import notion_props as np
    assert list(np.TASKS_SCHEMA(["a"]))[:2] == ["Name", "Status"]
    assert list(np.AGENTS_SCHEMA)[:6] == ["Name", "Status", "Last Heartbeat", "Cooldown Until", "Current Task",
                                           "Cost 5h USD"]


def test_decorate_uploads_when_no_public_url(tmp_path):
    from swarm.board.notion import decorate_object
    decorated, uploads = {}, {"n": 0}

    def handler(req: httpx.Request):
        path = req.url.path
        if req.method == "POST" and path.startswith("/v1/file_uploads/") and path.endswith("/send"):
            return httpx.Response(200, json={"status": "uploaded"})
        if req.method == "POST" and path == "/v1/file_uploads":
            uploads["n"] += 1
            return httpx.Response(200, json={"id": f"up{uploads['n']}"})
        if req.method == "PATCH":
            decorated[path.split("/")[3]] = json.loads(req.content)
            return httpx.Response(200, json={})
        return httpx.Response(500, json={"message": f"unexpected {req.method} {path}"})

    client = NotionClient("tok", transport=httpx.MockTransport(handler), sleep=lambda s: None)
    from swarm.board.notion import ASSETS_DIR
    assert decorate_object(client, "pages", "p1", "tasks", ASSETS_DIR, assets_url=None) == "upload"
    assert decorated["p1"]["icon"]["type"] == "file_upload" and uploads["n"] == 2


def test_decorate_falls_back_to_emoji_without_assets(tmp_path):
    from swarm.board.notion import decorate_board
    decorated = {}

    def handler(req: httpx.Request):
        path = req.url.path
        if req.method == "PATCH":
            decorated[path.split("/")[3]] = json.loads(req.content)
            return httpx.Response(200, json={})
        return httpx.Response(500, json={"message": f"unexpected {req.method} {path}"})

    client = NotionClient("tok", transport=httpx.MockTransport(handler), sleep=lambda s: None)
    failed = decorate_board(client, {"parent_page_id": "parent", "tasks_db": "db1"}, assets_dir=tmp_path / "none",
                            assets_url=None)
    assert failed == []
    assert decorated["parent"]["icon"] == {"type": "emoji", "emoji": "🐝"}
    assert decorated["db1"]["cover"]["external"]["url"].startswith("https://www.notion.so/images/page-cover/")


def test_retry_after_http_date_is_honored():
    from email.utils import format_datetime
    from datetime import datetime, timedelta, timezone
    calls = {"n": 0}
    when = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=7))

    def handler(req):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": when}, json={})
        return httpx.Response(200, json={"ok": True})

    client, sleeps = make_client(handler)
    assert client.request("GET", "/users/me") == {"ok": True}
    assert len(sleeps) == 1 and 4 <= sleeps[0] <= 8


def test_rows_are_created_with_icons():
    board, store = _board_with_fake_notion()
    t = board.create_task(Task(id="", title="ui", type="frontend"))
    q = board.create_question(Question(id="", text="why?", kind="fyi", task_id=t.id))
    a = board.upsert_agent(AgentRow(name="claude-a", provider="claude"))
    icons = {pid: (page.get("icon") or {}).get("external", {}).get("url", "")
             for ds in ("ds-tasks", "ds-q", "ds-agents") for pid, page in store[ds].items()}
    assert "icon-type-frontend.png?v=" in icons[t.page_id]
    assert "icon-q-fyi.png?v=" in icons[q.page_id] and "icon-agent.png?v=" in icons[a.page_id]


def test_asset_urls_change_path_when_the_art_changes(monkeypatch):
    """Notion caches external images by path and ignores the query string, so the assets' commit goes in the path."""
    from swarm.board import notion
    monkeypatch.setenv("SWARM_ASSETS_REF", "abc1234")
    url = notion.asset_url("icon-type-backend.png", notion.default_assets_base())
    assert "/aryanpatel142006/swarm-control/abc1234/swarm/assets/icon-type-backend.png?v=" in url
    monkeypatch.delenv("SWARM_ASSETS_REF")
    base = notion.default_assets_base(git=lambda: "9f0e1d2")
    assert base.endswith("/swarm-control/9f0e1d2/swarm/assets/")
    assert notion.default_assets_base(git=lambda: "").endswith("/swarm-control/main/swarm/assets/")
    monkeypatch.setenv("SWARM_ASSETS_URL", "https://cdn.example/x/")
    assert notion.default_assets_base(git=lambda: "9f0e1d2") == "https://cdn.example/x/"
