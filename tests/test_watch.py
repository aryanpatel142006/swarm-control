"""`swarm watch` never hides questions addressed to the orchestrator (Oct 11 2026: muse-b's checkpoint requests to
laptop-a were filtered out with the other laptops' chatter) and keys rows by page id (Q-1978 existed twice)."""
from swarm.models import Question, Status, Task
from swarm.watch import Watcher, snapshot


def _q(qid, text, *, kind="fyi", task="T-500", page="", asked_by="muse-b@laptop-b", status="Open"):
    return Question(id=qid, text=text, kind=kind, task_id=task, page_id=page or qid, asked_by=asked_by,
                    status=status)


def test_muted_host_chatter_is_dropped_but_orchestrator_asks_and_blockers_are_not(cfg):
    w = Watcher(cfg, local_host="host-a", mute_hosts=["laptop-b"], always=["Aryan"])
    tasks = [Task(id="T-500", title="x", status=Status.RUNNING, agent="fake-b")]
    prev = snapshot(tasks, [])
    qs = [_q("Q-1", "R121 sprint landed, saturation R87-R120"),
          _q("Q-2", "Please upload the fine-tuned checkpoints for claude-a"),
          _q("Q-3", "Need the A100 job id", kind="blocking"),
          _q("Q-4", "To the orchestrator: is the freeze still on?"),
          _q("Q-5", "Aryan should listen to this")]
    lines = w.diff(prev, snapshot(tasks, qs))
    ids = [ln.split()[1] for ln in lines]
    assert ids == ["Q-2", "Q-3", "Q-4", "Q-5"]
    assert "[for orchestrator]" in lines[0] and "@laptop-b" in lines[0]


def test_duplicate_question_ids_are_both_reported(cfg):
    w = Watcher(cfg, local_host="host-a")
    prev = snapshot([], [_q("Q-1978", "first", page="p1", asked_by="")])
    cur = snapshot([], [_q("Q-1978", "first", page="p1", asked_by=""), _q("Q-1978", "second", page="p2",
                                                                          asked_by="")])
    lines = w.diff(prev, cur)
    assert len(lines) == 1 and "second" in lines[0]


def test_task_status_changes_and_reopened_questions(cfg):
    w = Watcher(cfg, local_host="host-a")
    t1 = [Task(id="T-10", title="a", status=Status.READY, agent="claude-a", page_id="pa")]
    t2 = [Task(id="T-10", title="a", status=Status.RUNNING, agent="claude-a", page_id="pa")]
    q1 = [_q("Q-9", "x", status="Applied", asked_by="")]
    q2 = [_q("Q-9", "x", status="Open", asked_by="")]
    lines = w.diff(snapshot(t1, q1), snapshot(t2, q2))
    assert lines == ["TASK T-10: Ready -> Running (claude-a)", "REOPENED Q-9 [fyi] T-500: x"]
    assert w.diff(snapshot(t2, q2), snapshot(t2, q2)) == []


def test_cli_watch_once_prints_the_open_questions(project_dir):
    from typer.testing import CliRunner
    from swarm.cli import app
    r = CliRunner().invoke(app, ["--config", str(project_dir / ".swarm" / "config.yaml"), "--memory", "watch",
                                 "--once"])
    assert r.exit_code == 0 and "watch armed" in r.output
