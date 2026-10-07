"""Self-improvement: the retro turns a run's evidence into lessons and config changes."""
from datetime import timedelta

import yaml

from swarm.models import Status, Task, Usage, utcnow
from swarm.retro import Evidence, apply_patch, findings_from, lessons_markdown
from swarm.usage import Ledger


def _ledger(tmp_path):
    led = Ledger(tmp_path / "usage.jsonl", board="b1")
    now = utcnow()
    # T-013: three haiku runs ended at the turn limit, then sonnet, then opus succeeded
    for i in range(3):
        led.append(agent="claude-a", model="haiku", task_id="T-013", usage=Usage(1, 1, 0.2), duration_s=60, ok=False,
                   now=now - timedelta(minutes=30 - i))
    led.append(agent="claude-a", model="opus", task_id="T-013", usage=Usage(1, 1, 0.9), duration_s=200, ok=True, now=now)
    led.append(agent="claude-a", model="sonnet", task_id="T-013", usage=Usage(1, 1, 0.1), duration_s=20, ok=True,
               now=now, role="reviewer")
    led.append(agent="claude-a", model="sonnet", task_id="T-009", usage=Usage(1, 1, 0.5), duration_s=90, ok=True, now=now)
    return led


def _tasks():
    return [
        Task(id="T-013", title="Settings page", type="frontend", importance="low", size="S", status=Status.DONE,
             agent="claude-a", model="opus", attempts=4, review_rounds=2, flags=["out_of_scope"],
             last_error="error_max_turns: "),
        Task(id="T-011", title="Landing page", type="frontend", importance="high", size="M", status=Status.DONE,
             agent="claude-a", model="opus", attempts=3, review_rounds=3,
             feedback="Rebase onto main conflicted. Resolve conflicts in: docs/DESIGN.md."),
        Task(id="T-009", title="Live levels", type="backend", importance="normal", size="S", status=Status.DONE,
             agent="codex-b", model="gpt-6-sol", attempts=1, review_rounds=0),
        Task(id="T-003", title="Speaker panel", type="frontend", importance="critical", size="M", status=Status.DONE,
             agent="claude-a", model="opus", attempts=1, review_rounds=1),
    ]


def test_findings_cover_turn_limits_shared_files_and_helpful_tools(tmp_path):
    ev = Evidence(tasks=_tasks(), ledger=_ledger(tmp_path), board="b1",
                  files_by_task={"T-003": ["docs/DESIGN.md", "frontend/app.html"], "T-011": ["docs/DESIGN.md", "frontend/index.html"],
                                 "T-013": ["frontend/settings.html", "docs/DESIGN.md"]},
                  tools_by_task={"T-003": [{"name": "playwright", "kind": "mcp", "helped": True}],
                                 "T-011": [{"name": "playwright", "kind": "mcp", "helped": True},
                                           {"name": "frontend-design", "kind": "skill", "helped": True}],
                                 "T-013": [{"name": "playwright", "kind": "mcp", "helped": False}]},
                  agents={"claude-a": "claude", "codex-b": "codex"},
                  models_by_agent={"claude-a": {"best": "fable", "high": "opus", "mid": "sonnet", "low": "haiku"},
                                   "codex-b": {"best": "gpt-6-astra", "high": "gpt-6-astra", "mid": "gpt-6-sol", "low": "gpt-6-luna"}})
    f = {x.rule: x for x in findings_from(ev)}
    # cheap model kept hitting the turn limit on this task type → raise the floor for that type
    assert f["turn-limit"].patch == {"routing.type_model_overrides.claude.frontend.low": "sonnet"}
    assert "haiku" in f["turn-limit"].text and "frontend" in f["turn-limit"].text
    # an S task that needed several attempts → planner lesson about sizing (text only)
    assert f["undersized"].patch is None and "frontend" in f["undersized"].text and "S" in f["undersized"].text
    # one doc edited by three tasks → lesson naming the file and the tasks
    assert "docs/DESIGN.md" in f["shared-file"].text and "T-003" in f["shared-file"].text and f["shared-file"].patch is None
    # a tool that helped on two frontend tasks becomes a default for the type
    assert f["tool-default"].patch == {"mcp_by_type.frontend+": ["playwright"]}
    # a review-heavy task type is reported, not auto-changed
    assert f["review-rounds"].patch is None and "T-011" in f["review-rounds"].text


def test_no_evidence_no_findings(tmp_path):
    ev = Evidence(tasks=[Task(id="T-1", title="x", type="docs", status=Status.DONE, attempts=1)],
                  ledger=Ledger(tmp_path / "u.jsonl", board="b1"), board="b1", files_by_task={}, tools_by_task={},
                  agents={"claude-a": "claude"}, models_by_agent={"claude-a": {"mid": "sonnet", "low": "haiku"}})
    assert findings_from(ev) == []


def test_apply_patch_writes_a_tuning_file_that_overlays_config(tmp_path):
    """config.yaml keeps its comments; auto changes live in .swarm/tuning.yaml and are merged over it on load."""
    base = {"routing": {"type_model_overrides": {"claude": {"frontend": {"best": "opus"}}}},
            "mcp_by_type": {"frontend": ["context7"]}}
    tuning = tmp_path / "tuning.yaml"
    changed = apply_patch(tuning, {"routing.type_model_overrides.claude.frontend.low": "sonnet",
                                   "mcp_by_type.frontend+": ["playwright", "context7"]}, base=base)
    data = yaml.safe_load(tuning.read_text())
    assert changed is True
    assert data["routing"]["type_model_overrides"]["claude"]["frontend"] == {"low": "sonnet"}   # only the delta
    assert data["mcp_by_type"]["frontend"] == ["context7", "playwright"]   # the full list: base + new, no duplicates
    assert apply_patch(tuning, {"routing.type_model_overrides.claude.frontend.low": "sonnet",
                                "mcp_by_type.frontend+": ["playwright"]}, base=base) is False


def test_load_config_merges_the_tuning_file(project_dir, sample_config_dict):
    from swarm.config import load_config
    sample_config_dict["mcp_by_type"] = {"frontend": ["context7"]}
    (project_dir / ".swarm" / "config.yaml").write_text(yaml.safe_dump(sample_config_dict))
    (project_dir / ".swarm" / "tuning.yaml").write_text(yaml.safe_dump({
        "routing": {"type_model_overrides": {"claude": {"frontend": {"low": "sonnet"}}}},
        "mcp_by_type": {"frontend": ["context7", "playwright"]}}))
    cfg = load_config(project_dir / ".swarm" / "config.yaml")
    assert cfg.routing.type_model_overrides["claude"]["frontend"]["low"] == "sonnet"
    assert cfg.mcp_by_type["frontend"] == ["context7", "playwright"]
    assert cfg.agents["claude-a"].models["high"] == "opus"   # everything else untouched


def test_lessons_markdown_is_short_and_dedupes_by_rule(tmp_path):
    from swarm.retro import Finding
    lessons = tmp_path / "LESSONS.md"
    first = [Finding(rule="turn-limit", text="frontend on haiku hits the turn limit", patch=None),
             Finding(rule="shared-file", text="docs/DESIGN.md edited by T-003, T-011", patch=None)]
    lessons.write_text(lessons_markdown(None, first, when="2026-09-28"))
    text = lessons.read_text()
    assert text.startswith("# Lessons") and "turn-limit" in text and "docs/DESIGN.md" in text
    again = lessons_markdown(text, [Finding(rule="turn-limit", text="frontend on haiku hits the turn limit (again)", patch=None)],
                             when="2026-09-29")
    assert again.count("turn-limit") == 1 and "(again)" in again and "docs/DESIGN.md" in again   # newest wins per rule
    assert len(again.splitlines()) < 40


def _commit_on_main(git_repo, rel, content, subject):
    import subprocess
    p = git_repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    subprocess.run(["git", "-C", str(git_repo), "add", rel], check=True)
    subprocess.run(["git", "-C", str(git_repo), "commit", "-qm", subject], check=True)
    subprocess.run(["git", "-C", str(git_repo), "push", "-q", "origin", "main"], check=True)


def test_run_retro_writes_lessons_and_tuning_to_main(cfg, git_repo, tmp_path):
    """The whole loop: board + ledger + main history → LESSONS.md, tuning.yaml and a retro report committed to main."""
    import subprocess
    from swarm.board.memory import InMemoryBoard
    from swarm.retro import run_retro
    from swarm.workspace import CmdResult, Workspace
    board = InMemoryBoard()
    board.create_task(Task(id="", title="Panel", type="frontend", size="M", status=Status.DONE, agent="claude-a", milestone="M1"))
    board.create_task(Task(id="", title="Landing", type="frontend", size="M", status=Status.DONE, agent="claude-a", milestone="M1", review_rounds=3))
    board.create_task(Task(id="", title="Settings", type="frontend", size="S", status=Status.DONE, agent="claude-a", milestone="M1",
                           attempts=4, last_error="error_max_turns: "))
    _commit_on_main(git_repo, "docs/DESIGN.md", "a\n", "T-001 · Panel (#1)")
    _commit_on_main(git_repo, "docs/decisions/T-001.md", "## T-001\n- **Tools used:** playwright (mcp, helped): checked the page\n", "T-001 · Panel logs")
    _commit_on_main(git_repo, "docs/DESIGN.md", "b\n", "T-002 · Landing (#2)")
    _commit_on_main(git_repo, "docs/decisions/T-002.md", "## T-002\n- **Tools used:** playwright (mcp, helped): 390px check; frontend-design (skill, helped)\n", "T-002 · Landing logs")
    _commit_on_main(git_repo, "docs/DESIGN.md", "c\n", "T-003 · Settings (#3)")
    led = Ledger(tmp_path / "usage.jsonl", board=cfg.notion.tasks_ds[:8] if cfg.notion.tasks_ds else "")
    for _ in range(3):
        led.append(agent="claude-a", model="haiku", task_id="T-003", usage=Usage(1, 1, 0.2), duration_s=60, ok=False)
    led.append(agent="claude-a", model="sonnet", task_id="T-003", usage=Usage(1, 1, 0.5), duration_s=60, ok=True)
    ws = Workspace(git_repo, tmp_path / "wt", gh=lambda a, c: CmdResult(0, "", ""))
    logs = []
    findings = run_retro(cfg, board, ws, ledger=led, log=logs.append)
    rules = {f.rule for f in findings}
    assert {"turn-limit", "shared-file", "tool-default", "review-rounds", "undersized"} <= rules
    subprocess.run(["git", "-C", str(git_repo), "pull", "-q", "origin", "main"], check=True)
    lessons = (git_repo / "docs" / "LESSONS.md").read_text()
    assert "docs/DESIGN.md" in lessons and "haiku" in lessons
    tuning = yaml.safe_load((git_repo / ".swarm" / "tuning.yaml").read_text())
    assert tuning["routing"]["type_model_overrides"]["claude"]["frontend"]["low"] == "sonnet"
    assert "playwright" in tuning["mcp_by_type"]["frontend"]
    assert any(p.name.startswith("20") for p in (git_repo / "docs" / "retro").glob("*.md"))
    assert subprocess.run(["git", "-C", str(git_repo), "log", "-1", "--format=%s"], capture_output=True, text=True).stdout.startswith("swarm retro")
    # dry run: findings only, nothing written
    before = subprocess.run(["git", "-C", str(git_repo), "rev-parse", "origin/main"], capture_output=True, text=True).stdout
    run_retro(cfg, board, ws, ledger=led, log=logs.append, dry_run=True)
    after = subprocess.run(["git", "-C", str(git_repo), "rev-parse", "origin/main"], capture_output=True, text=True).stdout
    assert before == after


def test_tool_defaults_only_promote_real_skills_and_known_mcp_servers(tmp_path):
    """Workers labelled Bash, Edit, git and pytest as skills; the retro wrote them into skills_by_type."""
    tasks = [Task(id=f"T-{i}", title="x", type="backend", status=Status.DONE, attempts=1) for i in (1, 2)]
    tools = {f"T-{i}": [{"name": "git", "kind": "cli", "helped": True}, {"name": "Bash", "kind": "skill", "helped": True},
                        {"name": "test-driven-development", "kind": "skill", "helped": True},
                        {"name": "context7", "kind": "mcp", "helped": True}, {"name": "made-up", "kind": "mcp", "helped": True}]
             for i in (1, 2)}
    ev = Evidence(tasks=tasks, ledger=Ledger(tmp_path / "u.jsonl", board="b"), board="b", files_by_task={},
                  tools_by_task=tools, agents={"claude-a": "claude"},
                  known_skills={"test-driven-development", "frontend-design"}, known_mcp={"context7", "playwright"})
    f = {x.rule: x for x in findings_from(ev)}
    assert f["tool-default"].patch == {"skills_by_type.backend+": ["test-driven-development"], "mcp_by_type.backend+": ["context7"]}


def test_a_milestone_retro_counts_only_that_milestone_and_skips_tools_already_default(tmp_path):
    """Oct 6: every milestone retro (M4..M9) re-reported the same five all-time findings (M0's T-002 among them)
    and re-announced tools tuning.yaml already made default."""
    from swarm.retro import scoped
    tasks = [Task(id="T-002", title="old", type="infra", size="S", status=Status.DONE, attempts=4, milestone="M0"),
             Task(id="T-110", title="gpu", type="infra", size="M", status=Status.DONE, attempts=2, milestone="M9"),
             Task(id="T-111", title="deps", type="infra", size="S", status=Status.DONE, attempts=1, milestone="M9"),
             Task(id="T-009", title="docs", type="docs", size="S", status=Status.DONE, attempts=1, milestone="M0"),
             Task(id="T-024", title="docs2", type="docs", size="S", status=Status.DONE, attempts=1, milestone="M1")]
    tools = {tid: [{"name": "hearing-stack", "kind": "skill", "helped": True},
                   {"name": "systematic-debugging", "kind": "skill", "helped": True}] for tid in ("T-110", "T-111")}
    ev = Evidence(tasks=tasks, ledger=Ledger(tmp_path / "u.jsonl", board="b"), board="b",
                  files_by_task={"T-110": ["docs/DEPLOY.md", "scripts/gpu_up.sh"], "T-111": ["docs/DEPLOY.md"],
                                 "T-009": ["docs/BENCHMARKS.md"], "T-024": ["docs/BENCHMARKS.md"]},
                  tools_by_task=tools, agents={"claude-a": "claude"},
                  defaults={"skills_by_type.infra": ["hearing-stack"]})
    whole = {f.rule: f for f in findings_from(ev)}
    assert "T-002" in whole["undersized"].text
    # BENCHMARKS.md by T-009 (M0) and T-024 (M1) is two milestones, one owner each: not a shared-file finding
    assert "BENCHMARKS" not in whole["shared-file"].text and "docs/DEPLOY.md by T-110, T-111 (M9)" in whole["shared-file"].text
    m9 = {f.rule: f for f in findings_from(scoped(ev, "M9"))}
    assert "undersized" not in m9
    assert m9["tool-default"].patch == {"skills_by_type.infra+": ["systematic-debugging"]}
    assert "hearing-stack" not in m9["tool-default"].text and m9["tool-default"].text.startswith("New defaults by task type: infra:")
