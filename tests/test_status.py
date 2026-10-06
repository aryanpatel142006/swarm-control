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


def test_render_status_marks_agents_never_seen(cfg):
    now = cfg.event_end - timedelta(hours=8)
    agents = [AgentRow(name="codex-b", status="offline", last_heartbeat=None),
              AgentRow(name="claude-a", status="idle", last_heartbeat=now)]
    text = render_status(cfg, [], agents, [], now)
    assert "codex-b no heartbeat yet" in text and "claude-a idle" in text


def test_render_status_flags_a_stale_heartbeat_before_the_offline_cutoff(cfg):
    """Between 'alive' and 'offline' (10 min) an agent whose heartbeat is minutes old must not read as idle."""
    now = cfg.event_end - timedelta(hours=8)
    agents = [AgentRow(name="codex-b", status="idle", last_heartbeat=now - timedelta(minutes=4)),
              AgentRow(name="claude-a", status="running", current_task="T-2", last_heartbeat=now - timedelta(seconds=30))]
    text = render_status(cfg, [], agents, [], now)
    assert "codex-b idle (no heartbeat for 4 min)" in text and "claude-a running(T-2)" in text


def test_render_status_labels_tasks_without_a_milestone(cfg):
    now = cfg.event_end - timedelta(hours=8)
    text = render_status(cfg, [Task(id="T-1", title="a", status=Status.READY)], [], [], now)
    assert "(no milestone)" in text and "\n  -  " not in text


def test_headline_is_green_yellow_or_red_with_the_one_thing_to_know(cfg):
    from swarm.status import render_headline
    now = cfg.event_end - timedelta(hours=8)
    live = [AgentRow(name="claude-a", status="running", current_task="T-2", last_heartbeat=now),
            AgentRow(name="codex-a", status="offline", last_heartbeat=now - timedelta(hours=1))]
    ok_tasks = [Task(id="T-1", title="a", milestone="M1", status=Status.DONE),
                Task(id="T-2", title="b", milestone="M1", status=Status.RUNNING, agent="claude-a")]
    text, color = render_headline(cfg, ok_tasks, live, [], now)
    assert color == "green_background" and text.startswith("All good")
    assert "1 running" in text and "1/2 done" in text and "claude-a" in text and "codex-a" not in text.split("offline")[0]
    assert "04:00 UTC" in text   # when it was written, so a stale banner is obvious
    q = [Question(id="Q-4", text="Tap or rail?", task_id="T-2", status="Open", impact="high")]
    text, color = render_headline(cfg, ok_tasks, live, q, now)
    assert color == "yellow_background" and text.startswith("Needs you") and "Q-4" in text and "Tap or rail?" in text
    bad = ok_tasks + [Task(id="T-3", title="c", milestone="M1", status=Status.BLOCKED),
                      Task(id="T-4", title="d", milestone="M1", status=Status.FAILED, attempts=3, last_error="boom")]
    text, color = render_headline(cfg, bad, live, q, now)
    assert color == "red_background" and text.startswith("Risk")


def test_needs_you_hides_harness_and_relay_notes(cfg):
    from swarm.models import utcnow
    qs = [Question(id="Q-1", text="Tap or rail?", task_id="T-3", status="Open", kind="blocking"),
          Question(id="Q-2", text="[harness] T-3: 1 note", task_id="T-3", status="Open", kind="harness"),
          Question(id="Q-3", text="[relay] T-3 → T-4: hi", task_id="T-4", status="Applied", kind="relay"),
          Question(id="Q-4", text="[harness] T-4: 2 notes", task_id="T-4", status="Open", kind="harness")]
    text = render_status(cfg, [], [], qs, utcnow())
    needs = next(ln for ln in text.splitlines() if ln.startswith("Needs you: "))
    assert "Q-1" in needs and "Q-2" not in needs and "Q-4" not in needs
    assert "Harness notes: 2 open" in text


def test_idle_agent_with_nothing_ready_is_a_risk(cfg):
    from swarm.models import utcnow
    now = utcnow()
    agents = [AgentRow(name="claude-a", status="running", current_task="T-1", last_heartbeat=now),
              AgentRow(name="codex-a", status="idle", last_heartbeat=now)]
    tasks = [Task(id="T-1", title="a", status=Status.RUNNING, agent="claude-a", milestone="M0"),
             Task(id="T-2", title="b", status=Status.BACKLOG, agent="claude-a", milestone="M0", depends_on=["T-1"]),
             Task(id="T-3", title="c", status=Status.BACKLOG, agent="codex-a", milestone="M1", depends_on=["T-2"])]
    text = render_status(cfg, tasks, agents, [], now)
    assert "RISK" in text and "codex-a is idle" in text and "un-chain" in text
    tasks.append(Task(id="T-4", title="d", status=Status.READY, agent="codex-a", milestone="M0"))
    text = render_status(cfg, tasks, agents, [], now)
    assert "codex-a is idle" not in text


def test_an_exhausted_plan_reads_as_usage_limit_not_idle(cfg):
    now = cfg.event_end - timedelta(hours=8)
    agents = [AgentRow(name="codex-b", status="idle", last_heartbeat=now, cooldown_until=now + timedelta(hours=3),
                       note="usage limit until 10:03 UTC")]
    text = render_status(cfg, [Task(id="T-1", title="a", status=Status.BACKLOG)], agents, [], now)
    until = (now + timedelta(hours=3)).strftime("%H:%M")
    assert f"codex-b usage-limit until {until}" in text and "codex-b idle" not in text
    assert "is idle with nothing Ready" not in text
