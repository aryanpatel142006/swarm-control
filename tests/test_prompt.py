from swarm.models import Task
from swarm.prompt import compile_prompt, load_rules, read_doc, select_docs


def test_read_doc_whole_and_section(project_dir):
    assert "Blue buttons" in read_doc(project_dir, "docs/DESIGN.md")
    assert read_doc(project_dir, "docs/CONTRACTS.md#events").strip() == "## events\n{\"a\":1}"
    assert read_doc(project_dir, "docs/CONTRACTS.md#nope") is None
    assert read_doc(project_dir, "docs/missing.md") is None


def test_read_doc_truncates(project_dir):
    (project_dir / "big.md").write_text("x" * 20000)
    text = read_doc(project_dir, "big.md")
    assert len(text) < 12200 and text.endswith("[truncated]")


def test_select_docs(cfg):
    assert select_docs(cfg, "frontend") == ["PLAN.md#summary", "AGENTS.md", "docs/DESIGN.md", "docs/CONTRACTS.md"]
    assert select_docs(cfg, "ml_audio") == ["PLAN.md#summary", "AGENTS.md"]


def test_compile_prompt_contents(cfg):
    t = Task(id="T-016", title="Speaker card", description="Build it", acceptance="- renders\n- 60fps",
             type="frontend", importance="critical", size="M", scope=["frontend/src/**"], depends_on=["T-003"],
             feedback="Reviewer: fix contrast")
    p = compile_prompt(t, cfg, rules_text="RULES HERE", deps_summaries={"T-003": "Events API"},
                       structured_output_supported=True)
    assert "RULES HERE" in p and "T-016" in p and "Speaker card" in p and "Build it" in p
    assert "- renders" in p and "frontend/src/**" in p and "T-003: Events API" in p
    assert "Reviewer: fix contrast" in p
    assert "Blue buttons" in p and "A demo product." in p and "ml section" not in p
    assert '"status"' in p and ".swarm-run/report.json" not in p
    p2 = compile_prompt(t, cfg, rules_text="R", deps_summaries={}, structured_output_supported=False)
    assert ".swarm-run/report.json" in p2


def test_load_rules():
    assert "Worker rules" in load_rules()


def test_prompt_lists_tools_and_allows_install_for_critical(cfg):
    cfg.mcp_by_type = {"frontend": ["magic"]}
    cfg.skills_by_type = {"frontend": ["frontend-design"]}
    t = Task(id="T-020", title="Hero section", type="frontend", importance="normal", size="M")
    p = compile_prompt(t, cfg, rules_text="R", deps_summaries={}, structured_output_supported=True,
                       mcp=["magic"], skills=["frontend-design"])
    assert "## Tools for this task" in p and "magic" in p and "frontend-design" in p
    assert "plugin install" not in p
    t.importance = "critical"
    p2 = compile_prompt(t, cfg, rules_text="R", deps_summaries={}, structured_output_supported=True,
                        mcp=["magic"], skills=["frontend-design"])
    assert "claude plugin install" in p2
    t.importance = "normal"
    p3 = compile_prompt(t, cfg, rules_text="R", deps_summaries={}, structured_output_supported=True)
    assert "## Tools for this task" not in p3


def test_tools_section_asks_for_tools_used_in_the_report(cfg):
    t = Task(id="T-021", title="Hero", type="frontend", importance="normal", size="S")
    p = compile_prompt(t, cfg, rules_text="R", deps_summaries={}, structured_output_supported=True,
                       mcp=["playwright"], skills=[])
    assert "tools_used" in p


def test_small_tasks_get_skills_as_optional(cfg):
    """Q-123/Q-127: an S wiring change was told to invoke large frontend guides it did not need."""
    t = Task(id="T-064", title="Toggle", type="frontend", importance="normal", size="S")
    p = compile_prompt(t, cfg, rules_text="R", deps_summaries={}, structured_output_supported=True,
                       skills=["frontend-design"])
    assert "Skills available for this task" in p and "small task" in p
    t.size = "M"
    p2 = compile_prompt(t, cfg, rules_text="R", deps_summaries={}, structured_output_supported=True,
                        skills=["frontend-design"])
    assert "Skills to invoke before you start" in p2


def test_skill_line_tells_cli_without_a_skill_tool_where_to_read(cfg):
    """Q-128: a Codex worker was told to use a Skill tool it does not have."""
    t = Task(id="T-063", title="Page", type="frontend", importance="normal", size="M")
    p = compile_prompt(t, cfg, rules_text="R", deps_summaries={}, structured_output_supported=True,
                       skills=["frontend-design"])
    assert ".claude/skills/<name>/SKILL.md" in p
