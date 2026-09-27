import pytest
import yaml

from swarm.config import ConfigError, load_config, save_notion_ids


def test_loads_sample(project_dir):
    cfg = load_config(project_dir / ".swarm" / "config.yaml")
    assert cfg.project == "demo"
    assert cfg.repo_root == project_dir
    assert cfg.worktree_root == (project_dir / ".." / "demo-wt").resolve()
    assert cfg.limit_for("M").turns == 60
    assert cfg.agents["claude-a"].parallel == 2
    assert cfg.agents["codex-a"].parallel == 1
    assert [a.name for a in cfg.agents_on_host("host-b")] == ["fake-b"]
    assert cfg.agents["claude-a"].strengths["ml_audio"] == 3  # default when missing
    assert cfg.event_start.tzinfo is not None
    assert cfg.notion.tasks_ds == ""  # no notion.yaml yet


def test_rejects_unknown_host(project_dir, sample_config_dict):
    sample_config_dict["agents"]["claude-a"]["host"] = "nowhere"
    (project_dir / ".swarm" / "config.yaml").write_text(yaml.safe_dump(sample_config_dict))
    with pytest.raises(ConfigError, match="unknown host"):
        load_config(project_dir / ".swarm" / "config.yaml")


def test_rejects_missing_tier(project_dir, sample_config_dict):
    del sample_config_dict["agents"]["codex-a"]["models"]["low"]
    (project_dir / ".swarm" / "config.yaml").write_text(yaml.safe_dump(sample_config_dict))
    with pytest.raises(ConfigError, match="models.low"):
        load_config(project_dir / ".swarm" / "config.yaml")


def test_rejects_bad_review_policy(project_dir, sample_config_dict):
    sample_config_dict["review_policy"] = "sometimes"
    (project_dir / ".swarm" / "config.yaml").write_text(yaml.safe_dump(sample_config_dict))
    with pytest.raises(ConfigError, match="review_policy"):
        load_config(project_dir / ".swarm" / "config.yaml")


def test_generic_requires_command_template(project_dir, sample_config_dict):
    del sample_config_dict["agents"]["fake-b"]["command_template"]
    (project_dir / ".swarm" / "config.yaml").write_text(yaml.safe_dump(sample_config_dict))
    with pytest.raises(ConfigError, match="command_template"):
        load_config(project_dir / ".swarm" / "config.yaml")


def test_notion_ids_roundtrip(project_dir):
    cfg = load_config(project_dir / ".swarm" / "config.yaml")
    save_notion_ids(cfg, {"tasks_db": "db1", "tasks_ds": "ds1"})
    cfg2 = load_config(project_dir / ".swarm" / "config.yaml")
    assert cfg2.notion.tasks_db == "db1" and cfg2.notion.tasks_ds == "ds1"
