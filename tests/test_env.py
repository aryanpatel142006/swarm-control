import os

from swarm.env import load_env_file


def test_load_env_file_sets_missing_keys_only(tmp_path, monkeypatch):
    f = tmp_path / "env"
    f.write_text('# secrets\nexport NOTION_TOKEN=ntn_abc\nSWARM_HOST="laptop-b"\nexport QUOTED=\'a b\'\n\nBAD LINE\n')
    monkeypatch.delenv("NOTION_TOKEN", raising=False)
    monkeypatch.setenv("SWARM_HOST", "laptop-a")
    monkeypatch.delenv("QUOTED", raising=False)
    loaded = load_env_file(f)
    assert os.environ["NOTION_TOKEN"] == "ntn_abc"
    assert os.environ["SWARM_HOST"] == "laptop-a"      # never overrides what the shell already set
    assert os.environ["QUOTED"] == "a b"
    assert loaded == ["NOTION_TOKEN", "QUOTED"]


def test_load_env_file_missing_is_noop(tmp_path):
    assert load_env_file(tmp_path / "nope") == []
