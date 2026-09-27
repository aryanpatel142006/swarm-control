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
