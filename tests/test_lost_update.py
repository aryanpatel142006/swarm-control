"""Lost updates: a write built from an older copy of a task must not undo a change made since (T-361, Oct 10 2026:
the orchestrator pinned the task to laptop-b, answered its question, and serve's unblock wrote back the flags it had
read before the pin; rebalance then moved the task to laptop-a)."""
import copy

from swarm.board.base import fresh_flags
from swarm.board.memory import InMemoryBoard
from swarm.models import AgentRow, Question, Status, Task, utcnow
from swarm.serve import Server
from swarm.workspace import CmdResult, Workspace


class LaggingBoard(InMemoryBoard):
    """Lookups by id and listings return the snapshot taken by `freeze` (Notion's query index lags behind page
    writes); a read by page id returns the row as it is now."""

    def __init__(self):
        super().__init__()
        self.frozen: dict[str, Task] = {}

    def freeze(self, task_id):
        self.frozen[task_id] = copy.deepcopy(self.tasks[task_id])

    def get_task(self, task_id, *, page_id=None, agent=None):
        if page_id is None and task_id in self.frozen:
            return copy.deepcopy(self.frozen[task_id])
        return super().get_task(task_id, page_id=page_id, agent=agent)

    def list_tasks(self, *, status=None, agent=None):
        out = []
        for t in super().list_tasks():
            t = copy.deepcopy(self.frozen.get(t.id, t))
            if (status is None or t.status in set(status)) and (agent is None or t.agent in set(agent)):
                out.append(t)
        return out


def make(cfg, git_repo, tmp_path, board):
    ws = Workspace(git_repo, tmp_path / "wt", gh=lambda a, c: CmdResult(0, "", ""))
    return Server(cfg, board, ws, reviewer=None, merger=None, now=utcnow, sleep=lambda s: None, log=lambda *a: None)


def pin(board, task_id, host):
    t = InMemoryBoard.get_task(board, task_id)
    t.flags = t.flags + [f"host:{host}"]
    board.update_task(t, ["flags"])


def test_unblock_keeps_a_host_pin_set_after_serve_read_the_task(cfg, git_repo, tmp_path):
    board = LaggingBoard()
    srv = make(cfg, git_repo, tmp_path, board)
    t = board.create_task(Task(id="", title="t361", status=Status.BLOCKED, agent="fake-b", flags=["resume"]))
    q = board.create_question(Question(id="", text="which mic?", kind="blocking", task_id=t.id))
    board.freeze(t.id)                     # serve's lookup still sees the row from before the pin
    pin(board, t.id, "host-b")             # the orchestrator pins it ...
    q.answer = "use the iPhone"            # ... then answers its question
    board.update_question(q, ["answer"])
    assert srv.relay() == 1
    now = InMemoryBoard.get_task(board, t.id)
    assert now.status is Status.READY and "use the iPhone" in now.feedback
    assert now.flags == ["resume", "host:host-b"] and now.pinned_host == "host-b"


def test_merge_question_answer_keeps_a_pin_set_meanwhile(cfg, git_repo, tmp_path):
    board = LaggingBoard()
    srv = make(cfg, git_repo, tmp_path, board)
    t = board.create_task(Task(id="", title="m", status=Status.BLOCKED, agent="fake-b", flags=["merge_failed_3"]))
    q = board.create_question(Question(id="", text="merge?", kind="blocking", task_id=t.id))
    board.freeze(t.id)
    pin(board, t.id, "host-b")
    q.answer = "retry"
    board.update_question(q, ["answer"])
    assert srv.relay() == 1
    now = InMemoryBoard.get_task(board, t.id)
    assert now.status is Status.MERGE_READY and now.flags == ["host:host-b"]


def test_rebalance_does_not_move_a_task_pinned_after_the_listing(cfg, git_repo, tmp_path):
    board = LaggingBoard()
    srv = make(cfg, git_repo, tmp_path, board)
    board.upsert_agent(AgentRow(name="fake-b", status="idle", last_heartbeat=utcnow()))
    ids = [board.create_task(Task(id="", title=f"q{i}", status=Status.READY, agent="codex-a", type="ml_audio")).id
           for i in range(3)]
    board.create_task(Task(id="", title="busy", status=Status.RUNNING, agent="codex-a", type="ml_audio"))
    for i in ids:
        board.freeze(i)
    pin(board, ids[-1], "host-a")          # the task rebalance would pick for fake-b (host-b)
    srv.rebalance()
    assert InMemoryBoard.get_task(board, ids[-1]).agent != "fake-b"


def test_fresh_flags_rebases_on_the_board_and_falls_back_to_the_callers_copy():
    board = InMemoryBoard()
    t = board.create_task(Task(id="", title="x", flags=["merge_failed_1", "resume"]))
    pin(board, t.id, "laptop-b")
    assert fresh_flags(board, t, add=["env"], drop=lambda f: f.startswith("merge_failed")) == \
        ["resume", "host:laptop-b", "env"]
    gone = Task(id="T-999", title="gone", flags=["resume"])
    assert fresh_flags(board, gone, add=["env"]) == ["resume", "env"]
