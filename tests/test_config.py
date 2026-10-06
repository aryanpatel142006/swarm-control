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


def test_mcp_by_type_and_agent_mcp(project_dir, sample_config_dict):
    sample_config_dict["mcp_by_type"] = {"frontend": ["magic", "playwright"]}
    sample_config_dict["agents"]["claude-a"]["mcp"] = ["context7"]
    (project_dir / ".swarm" / "config.yaml").write_text(yaml.safe_dump(sample_config_dict))
    cfg = load_config(project_dir / ".swarm" / "config.yaml")
    assert cfg.mcp_by_type == {"frontend": ["magic", "playwright"]}
    assert cfg.agents["claude-a"].mcp == ["context7"] and cfg.agents["codex-a"].mcp == []
    assert cfg.mcp_for("frontend", cfg.agents["claude-a"]) == ["magic", "playwright", "context7"]
    assert cfg.mcp_for("backend", cfg.agents["codex-a"]) == []


def test_skills_and_importance_mcp(project_dir, sample_config_dict):
    sample_config_dict["mcp_by_type"] = {"frontend": ["magic"]}
    sample_config_dict["mcp_by_importance"] = {"critical": ["context7"]}
    sample_config_dict["skills_by_type"] = {"frontend": ["frontend-design", "impeccable"]}
    sample_config_dict["plugins_required"] = ["frontend-design", "humanizer"]
    (project_dir / ".swarm" / "config.yaml").write_text(yaml.safe_dump(sample_config_dict))
    cfg = load_config(project_dir / ".swarm" / "config.yaml")
    a = cfg.agents["claude-a"]
    assert cfg.mcp_for("frontend", a) == ["magic"]
    assert cfg.mcp_for("frontend", a, importance="critical") == ["magic", "context7"]
    assert cfg.skills_for("frontend") == ["frontend-design", "impeccable"] and cfg.skills_for("backend") == []
    assert cfg.plugins_required == ["frontend-design", "humanizer"]


def test_inline_mcp_server_definitions(project_dir, sample_config_dict):
    sample_config_dict["mcp_servers"] = {"playwright": {"command": "npx", "args": ["@playwright/mcp@latest", "--headless"]}}
    (project_dir / ".swarm" / "config.yaml").write_text(yaml.safe_dump(sample_config_dict))
    cfg = load_config(project_dir / ".swarm" / "config.yaml")
    assert cfg.mcp_servers["playwright"]["args"][-1] == "--headless"


def test_family_keys_cover_subtypes(project_dir, sample_config_dict):
    """`ml` in a *_by_type map applies to ml_audio / ml_vision / ml_fusion; the exact type is listed first."""
    sample_config_dict["skills_by_type"] = {"ml": ["huggingface-best"], "ml_audio": ["audio-stack"]}
    sample_config_dict["mcp_by_type"] = {"ml": ["context7"]}
    sample_config_dict["plugins_by_type"] = {"ml": ["hf-plugin"]}
    sample_config_dict["docs_by_type"] = {"_all": ["PLAN.md#summary"], "ml": ["docs/ARCHITECTURE.md#ml"]}
    (project_dir / ".swarm" / "config.yaml").write_text(yaml.safe_dump(sample_config_dict))
    cfg = load_config(project_dir / ".swarm" / "config.yaml")
    assert cfg.skills_for("ml_audio") == ["audio-stack", "huggingface-best"]
    assert cfg.skills_for("ml_vision") == ["huggingface-best"]
    assert cfg.mcp_for("ml_fusion", cfg.agents["claude-a"]) == ["context7"]
    assert cfg.plugins_for("ml_vision") == ["hf-plugin"]
    assert cfg.skills_for("frontend") == []
    from swarm.prompt import select_docs
    assert select_docs(cfg, "ml_audio") == ["PLAN.md#summary", "docs/ARCHITECTURE.md#ml"]


def test_placeholder_check_is_configurable(project_dir, sample_config_dict):
    path = project_dir / ".swarm" / "config.yaml"
    cfg = load_config(path)
    assert cfg.verify.placeholders is None and cfg.verify.placeholder_files is None    # defaults apply
    sample_config_dict["verify"]["placeholders"] = [r"\bTBD\b"]
    sample_config_dict["verify"]["placeholder_files"] = ["**/*.md", "**/*.py"]
    path.write_text(yaml.safe_dump(sample_config_dict))
    cfg = load_config(path)
    assert cfg.verify.placeholders == [r"\bTBD\b"] and "**/*.py" in cfg.verify.placeholder_files
    sample_config_dict["verify"]["placeholders"] = ["("]
    path.write_text(yaml.safe_dump(sample_config_dict))
    with pytest.raises(ConfigError, match="bad regex"):
        load_config(path)


def test_planner_rules_ask_docs_tasks_to_grep_cited_ui_controls():
    from swarm.prompt import PROMPTS_DIR
    text = (PROMPTS_DIR / "planner.md").read_text()
    assert "every control the doc names exists in the UI source" in text
