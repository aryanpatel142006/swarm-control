from datetime import timedelta

from swarm.models import AgentRow, Question, Status, Task
from swarm.status import render_status


def test_render_status_lines(cfg):
    now = cfg.event_end - timedelta(hours=8, minutes=48)
    tasks = [Task(id="T-1", title="a", milestone="M1", status=Status.DONE),
             Task(id="T-2", title="b", milestone="M2", status=Status.RUNNING, agent="claude-a"),
             Task(id="T-3", title="c", milestone="M2", status=Status.BLOCKED),
             Task(id="T-4", title="d", milestone="M2", status=Status.FAILED, attempts=3, last_error="boom"),
             Task(id="T-5", title="e", milestone="M2", status=Status.READY)]
    agents = [AgentRow(name="claude-a", status="running", current_task="T-2", cost_5h_usd=31.2),
              AgentRow(name="codex-a", status="cooldown", cooldown_until=now + timedelta(minutes=9))]
    qs = [Question(id="Q-4", text="Tap or rail?", task_id="T-3", status="Open", impact="high")]
    text = render_status(cfg, tasks, agents, qs, now)
    assert "8.8 h left" in text
    assert "M1" in text and "1/1" in text
    assert "M2" in text and "0/4" in text and "1 running" in text and "1 blocked" in text
    assert "Q-4" in text and "Tap or rail?" in text and "T-4" in text
    assert "claude-a" in text and "$31.2" in text and "cooldown" in text
    assert "RISK" in text  # a blocked + failed pair in one milestone flags risk
