import json

from swarm.board.memory import InMemoryBoard
from swarm.models import RunResult, Status, Task, Usage
from swarm.planner import TASKS_SCHEMA, Planner, build_plan_prompt, lint_conflicts, parse_proposals
from swarm.workspace import CmdResult, Workspace

PROPOSALS = [
    {"title": "Define audio event contract", "description": "d1", "acceptance": "- schema in docs/CONTRACTS.md",
     "type": "backend", "importance": "critical", "size": "S", "milestone": "M1", "scope": ["docs/CONTRACTS.md"]},
    {"title": "WebSocket audio stream", "description": "d2", "acceptance": "- frames arrive", "type": "realtime",
     "importance": "high", "size": "M", "milestone": "M1", "scope": ["backend/**"],
     "depends_on": ["Define audio event contract"]},
    {"title": "Speaker card UI", "description": "d3", "acceptance": "- renders", "type": "frontend",
     "importance": "high", "size": "M", "milestone": "M1", "scope": ["frontend/src/**"], "depends_on": ["T-001"]},
    {"title": "Also edits backend", "description": "d4", "acceptance": "- x", "type": "backend",
     "importance": "low", "size": "S", "milestone": "M1", "scope": ["backend/api/**"]},
]


def test_schema_and_parse(tmp_path):
    assert TASKS_SCHEMA["properties"]["tasks"]["items"]["required"][:2] == ["title", "description"]
    assert parse_proposals({"tasks": PROPOSALS}, tmp_path)[0]["title"] == "Define audio event contract"
    (tmp_path / ".swarm-run").mkdir()
    (tmp_path / ".swarm-run" / "plan.json").write_text(json.dumps({"tasks": PROPOSALS[:1]}))
    assert len(parse_proposals(None, tmp_path)) == 1
    assert parse_proposals({"tasks": [{"title": "no type"}]}, tmp_path)[0]["type"] == "backend"
    assert parse_proposals({"nope": 1}, tmp_path / "x") == []


def test_lint_conflicts():
    warnings = lint_conflicts(PROPOSALS)
    assert any("WebSocket audio stream" in w and "Also edits backend" in w for w in warnings)
    assert not any("Define audio event contract" in w and "Speaker card UI" in w for w in warnings)


def test_prompt_pieces():
    p = build_plan_prompt("# Plan\nbuild it", {"docs/DESIGN.md": "tokens"}, [Task(id="T-001", title="Existing")],
                          "M1", "PLANNER RULES")
    assert "PLANNER RULES" in p and "build it" in p and "tokens" in p and "T-001" in p and "M1" in p
    assert '"tasks"' in p
    p2 = build_plan_prompt("x", {}, [], None, "R", split_of=Task(id="T-009", title="Big", description="big desc"))
    assert "Split" in p2 and "T-009" in p2 and "big desc" in p2


def test_apply_resolves_deps_and_routes(cfg, git_repo, tmp_path):
    board = InMemoryBoard()
    board.create_task(Task(id="T-001", title="pre-existing", status=Status.DONE))
    ws = Workspace(git_repo, tmp_path / "wt", gh=lambda a, c: CmdResult(0, "", ""))
    pl = Planner(cfg, board, ws, log=lambda *a: None, prompt_text="R")
    created = pl.apply(PROPOSALS)
    ids = [t.id for t in created]
    assert ids == ["T-002", "T-003", "T-004", "T-005"]
    ws_task = board.get_task("T-003")
    assert ws_task.depends_on == ["T-002"] and ws_task.status is Status.BACKLOG
    ui = board.get_task("T-004")
    assert ui.depends_on == ["T-001"] and ui.status is Status.READY and ui.agent == "claude-a" and ui.model == "opus"
    assert board.get_task("T-002").status is Status.READY and board.get_task("T-002").agent == "codex-a"


def test_propose_runs_adapter(cfg, git_repo, tmp_path):
    class FakePlannerAdapter:
        def __init__(self):
            self.specs = []
            self.prompts = []

        def run(self, spec):
            self.specs.append(spec)
            self.prompts.append(spec.prompt_file.read_text())
            return RunResult(ok=True, exit_code=0, stdout="", stderr="", structured_output={"tasks": PROPOSALS[:2]},
                             usage=Usage())

    board = InMemoryBoard()
    ws = Workspace(git_repo, tmp_path / "wt", gh=lambda a, c: CmdResult(0, "", ""))
    adapter = FakePlannerAdapter()
    pl = Planner(cfg, board, ws, adapter_factory=lambda a: adapter, log=lambda *a: None, prompt_text="R")
    (git_repo / "PLAN.md").write_text("# P\n\n## Summary\nthe plan\n")
    props = pl.propose(git_repo / "PLAN.md", milestone="M1")
    assert len(props) == 2 and adapter.specs[0].read_only is True and adapter.specs[0].model == "opus"
    assert "the plan" in adapter.prompts[0]


def test_propose_records_its_run_in_the_ledger(cfg, git_repo, tmp_path):
    from swarm.usage import Ledger

    class FakePlannerAdapter:
        def run(self, spec):
            return RunResult(ok=True, exit_code=0, stdout="", stderr="", structured_output={"tasks": PROPOSALS[:1]},
                             usage=Usage(100, 50, 0.42))

    board = InMemoryBoard()
    ws = Workspace(git_repo, tmp_path / "wt", gh=lambda a, c: CmdResult(0, "", ""))
    ledger = Ledger(tmp_path / "usage.jsonl")
    pl = Planner(cfg, board, ws, adapter_factory=lambda a: FakePlannerAdapter(), log=lambda *a: None,
                 prompt_text="R", ledger=ledger)
    (git_repo / "PLAN.md").write_text("# P\n\n## Summary\nthe plan\n")
    pl.propose(git_repo / "PLAN.md", milestone="M1")
    rows = ledger._rows()
    assert len(rows) == 1 and rows[0]["task"] == "plan:M1" and rows[0]["cost"] == 0.42 and rows[0]["model"] == "opus"
    assert (cfg.repo_root / ".swarm" / "tasks.proposed.json").exists()
