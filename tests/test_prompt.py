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
