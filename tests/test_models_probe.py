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
