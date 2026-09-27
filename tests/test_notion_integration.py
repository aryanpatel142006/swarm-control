"""Runs only when SWARM_NOTION_TOKEN and SWARM_NOTION_PARENT are set. Creates real databases under the parent page."""
import os

import pytest

from swarm.board.base import claim_task
from swarm.board.notion import NotionBoard, NotionClient
from swarm.config import NotionIds
from swarm.models import AgentRow, Question, Status, Task

TOKEN = os.environ.get("SWARM_NOTION_TOKEN")
PARENT = os.environ.get("SWARM_NOTION_PARENT")
pytestmark = pytest.mark.skipif(not (TOKEN and PARENT), reason="set SWARM_NOTION_TOKEN and SWARM_NOTION_PARENT")


def test_init_and_roundtrip():
    client = NotionClient(TOKEN)
    ids = NotionBoard.init(client, PARENT, ["claude-a", "codex-a"])
    assert ids["tasks_ds"] and ids["questions_ds"] and ids["agents_ds"] and ids["status_block"]
    board = NotionBoard(client, NotionIds(**{k: v for k, v in ids.items() if k in NotionIds.__dataclass_fields__}))
    t = board.create_task(Task(id="", title="integration task", description="hello **world**", status=Status.READY,
                               agent="claude-a", type="docs", scope=["docs/**"]))
    assert t.id == "T-001"
    assert claim_task(board, t, "claude-a") is True
    assert board.get_task("T-001").status is Status.RUNNING
    board.append_task_report(t, "Report — attempt 1", "did a thing\n\n```json\n{\"a\":1}\n```")
    assert [x.id for x in board.list_tasks(status=[Status.RUNNING], agent=["claude-a"])] == ["T-001"]
    q = board.create_question(Question(id="", text="works?", task_id="T-001", kind="fyi"))
    assert board.list_questions(status="Open")[0].id == q.id
    row = board.upsert_agent(AgentRow(name="claude-a", provider="claude", host="lab", runs=1))
    row.runs = 2
    board.upsert_agent(row)
    assert board.get_agent("claude-a").runs == 2
    board.write_status_page("SWARM STATUS · integration test")
    print("views:", ids.get("views_ok"))
