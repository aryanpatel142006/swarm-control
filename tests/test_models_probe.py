from pathlib import Path

from swarm.doctor import Check, models_note, probe_models


def test_models_note_lists_tiers_and_cli_version(cfg):
    a = cfg.agents["claude-a"]
    note = models_note(a, "2.1.283")
    assert note.startswith("claude 2.1.283") and "best=" in note and a.models["low"] in note


def test_probe_models_tries_each_distinct_model_and_records_on_the_board(cfg, tmp_path):
    from swarm.board.memory import InMemoryBoard
    board = InMemoryBoard()
    calls = []

    def fake_smoke(cfg_, name, tmp, model=None):
        calls.append(model)
        return Check(f"smoke:{name}:{model}", model != "haiku", "ok" if model != "haiku" else "model not found")

    checks = probe_models(cfg, "claude-a", tmp_path, board=board, smoke=fake_smoke)
    distinct = list(dict.fromkeys(cfg.agents["claude-a"].models.values()))
    assert calls == distinct                       # each model id once, even when tiers share a model
    assert [c.ok for c in checks] == [m != "haiku" for m in distinct]
    row = board.get_agent("claude-a")
    assert row is not None and "models ok:" in row.note and "failed: haiku" in row.note


def test_a_good_smoke_check_lifts_a_lost_login_and_a_bad_one_keeps_it(cfg, tmp_path):
    from datetime import timedelta
    from swarm.board.memory import InMemoryBoard
    from swarm.models import AgentRow, auth_lost, utcnow
    board = InMemoryBoard()
    now = utcnow()
    board.upsert_agent(AgentRow(name="claude-a", status="cooldown", cooldown_until=now + timedelta(minutes=50),
                                note="auth lost until 05:45 UTC (hit at 04:45)"))
    probe_models(cfg, "claude-a", tmp_path, board=board, smoke=lambda c, n, t, model=None: Check(n, False, "401"))
    assert auth_lost(board.get_agent("claude-a"), utcnow())
    probe_models(cfg, "claude-a", tmp_path, board=board, smoke=lambda c, n, t, model=None: Check(n, True, "ok"))
    row = board.get_agent("claude-a")
    assert not auth_lost(row, utcnow()) and row.cooldown_until is None and row.status == "idle"
    assert row.note.startswith("models ok:")
