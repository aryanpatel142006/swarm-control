from datetime import timedelta

from swarm.models import Usage, utcnow
from swarm.usage import Ledger, default_ledger_path


def test_ledger_append_window_totals(tmp_path):
    led = Ledger(tmp_path / "usage.jsonl")
    now = utcnow()
    led.append(agent="a", model="m", task_id="T-1", usage=Usage(10, 5, 0.5), duration_s=1.0, ok=True,
               now=now - timedelta(hours=6))
    led.append(agent="a", model="m", task_id="T-2", usage=Usage(20, 5, 1.0), duration_s=1.0, ok=True,
               now=now - timedelta(hours=1))
    led.append(agent="b", model="m", task_id="T-3", usage=Usage(1, 1, None), duration_s=1.0, ok=False, now=now)
    w = led.window("a", hours=5, now=now)
    assert (w.input_tokens, w.output_tokens, w.cost_usd) == (20, 5, 1.0)
    t = led.totals("a")
    assert (t.input_tokens, t.cost_usd) == (30, 1.5)
    assert led.totals("b").cost_usd == 0.0 and led.all_agents() == ["a", "b"]


def test_ledger_missing_file_is_empty(tmp_path):
    led = Ledger(tmp_path / "nope" / "usage.jsonl")
    assert led.window("x").cost_usd == 0.0
    led.append(agent="x", model="m", task_id="T", usage=Usage(), duration_s=0, ok=True)
    assert (tmp_path / "nope" / "usage.jsonl").exists()


def test_default_path():
    assert str(default_ledger_path("demo")).endswith("/.swarm/demo/usage.jsonl")


def test_ledger_records_cache_tokens(tmp_path):
    led = Ledger(tmp_path / "usage.jsonl")
    led.append(agent="a", model="m", task_id="T-1", usage=Usage(10, 5, 0.5, cache_write_tokens=7000, cache_read_tokens=90000),
               duration_s=1.0, ok=True)
    row = led._rows()[0]
    assert row["cache_w"] == 7000 and row["cache_r"] == 90000
    t = led.totals("a")
    assert t.cache_write_tokens == 7000 and t.cache_read_tokens == 90000


def test_ledger_rows_carry_a_role(tmp_path):
    led = Ledger(tmp_path / "usage.jsonl")
    led.append(agent="a", model="m", task_id="T-1", usage=Usage(1, 1, 0.1), duration_s=1, ok=True)
    led.append(agent="a", model="m", task_id="T-1", usage=Usage(1, 1, 0.2), duration_s=1, ok=True, role="reviewer")
    rows = led._rows()
    assert rows[0]["role"] == "worker" and rows[1]["role"] == "reviewer"


def test_usage_report_splits_spend_and_waste(tmp_path):
    from swarm.usage import usage_report
    led = Ledger(tmp_path / "usage.jsonl")
    now = utcnow()
    led.append(agent="p", model="opus", task_id="plan:M1", usage=Usage(10, 5, 1.0), duration_s=60, ok=True, role="planner", now=now)
    led.append(agent="a", model="sonnet", task_id="T-1", usage=Usage(10, 5, 0.5, cache_write_tokens=1000, cache_read_tokens=9000), duration_s=60, ok=True, now=now)
    led.append(agent="a", model="sonnet", task_id="T-1", usage=Usage(1, 1, 0.1), duration_s=10, ok=True, role="reviewer", now=now)
    led.append(agent="a", model="opus", task_id="T-2", usage=Usage(10, 5, 0.3), duration_s=5, ok=False, now=now)     # spawn failure
    led.append(agent="a", model="opus", task_id="T-2", usage=Usage(10, 5, 0.9, cache_write_tokens=500, cache_read_tokens=500), duration_s=90, ok=True, now=now)
    led.append(agent="a", model="sonnet", task_id="T-2", usage=Usage(1, 1, 0.1), duration_s=10, ok=True, role="reviewer", now=now)
    led.append(agent="a", model="sonnet", task_id="T-2", usage=Usage(1, 1, 0.1), duration_s=10, ok=True, role="reviewer", now=now)
    r = usage_report(led, done=["T-1", "T-2"])
    assert r["total_cost"] == 3.0 and r["by_role"] == {"planner": 1.0, "worker": 1.7, "reviewer": 0.3}
    assert r["cost_per_done_task"] == 1.5
    t2 = {t["task"]: t for t in r["tasks"]}["T-2"]
    assert t2["worker_runs"] == 2 and t2["review_runs"] == 2 and t2["cost"] == 1.4
    # waste = failed runs + worker attempts beyond the first + review rounds beyond the first
    assert r["waste"]["failed_runs"] == 0.3 and r["waste"]["extra_review_rounds"] == 0.1 and r["waste"]["total"] == 0.4
    assert r["cache"]["write"] == 1500 and r["cache"]["read"] == 9500
    assert t2["models"] == ["opus", "sonnet"]


def test_usage_report_since_ignores_older_rows(tmp_path):
    from swarm.usage import usage_report
    led = Ledger(tmp_path / "usage.jsonl")
    now = utcnow()
    led.append(agent="a", model="m", task_id="T-1", usage=Usage(1, 1, 5.0), duration_s=1, ok=True, now=now - timedelta(days=1))
    led.append(agent="a", model="m", task_id="T-1", usage=Usage(1, 1, 0.5), duration_s=1, ok=True, now=now)
    r = usage_report(led, done=["T-1"], since=now - timedelta(hours=1))
    assert r["total_cost"] == 0.5 and r["runs"] == 1


def test_ledger_rows_carry_a_board_id_and_reports_filter_on_it(tmp_path):
    """Task ids restart at T-001 on every board; the ledger must not mix two boards' T-019s."""
    from swarm.usage import usage_report
    led = Ledger(tmp_path / "usage.jsonl")
    led.append(agent="a", model="m", task_id="T-019", usage=Usage(1, 1, 5.0), duration_s=1, ok=True, board="old12345")
    led.append(agent="a", model="m", task_id="T-019", usage=Usage(1, 1, 0.5), duration_s=1, ok=True, board="new67890")
    led.append(agent="a", model="m", task_id="T-001", usage=Usage(1, 1, 0.2), duration_s=1, ok=True)   # legacy row, no board
    assert led._rows()[0]["board"] == "old12345"
    r = usage_report(led, done=["T-019"], board="new67890")
    assert r["total_cost"] == 0.5 and r["runs"] == 1
    assert usage_report(led)["runs"] == 3   # no filter: everything, as before
