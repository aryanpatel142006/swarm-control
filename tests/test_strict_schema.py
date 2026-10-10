"""OpenAI strict structured outputs (newer Codex CLIs, codex-c on laptop-c, Oct 10): the Codex adapter sends a
strict form of each schema, and the parsers read a null optional field as a missing one."""
import json

from swarm.adapters.base import RunSpec
from swarm.adapters.claude import ClaudeAdapter
from swarm.adapters.codex import CodexAdapter
from swarm.planner import TASKS_SCHEMA, parse_proposals
from swarm.report import REPORT_SCHEMA, REVIEW_SCHEMA, parse_report
from swarm.reviewer import parse_verdict
from swarm.strict_schema import drop_nulls, strict_violations, to_openai_strict

SMOKE = {"type": "object", "properties": {"status": {"type": "string"}, "summary": {"type": "string"}},
         "required": ["status"]}


def test_report_review_tasks_and_smoke_schemas_violate_strict_before_and_comply_after():
    for schema in (REPORT_SCHEMA, REVIEW_SCHEMA, TASKS_SCHEMA, SMOKE):
        strict = to_openai_strict(schema)
        assert strict_violations(strict) == [], strict_violations(strict)
        assert strict["type"] == "object"
    assert strict_violations(REPORT_SCHEMA) and strict_violations(REVIEW_SCHEMA)


def test_conversion_is_a_copy_and_keeps_required_fields_non_null():
    before = json.dumps(REPORT_SCHEMA, sort_keys=True)
    strict = to_openai_strict(REPORT_SCHEMA)
    assert json.dumps(REPORT_SCHEMA, sort_keys=True) == before
    props = strict["properties"]
    assert props["status"] == {"type": "string", "enum": ["done", "blocked", "failed"]}
    assert props["summary"]["type"] == "string" and props["summary"]["description"]
    assert props["notes_for_reviewer"] == {"type": ["string", "null"]}
    files = props["files_changed"]
    assert files["anyOf"][1] == {"type": "null"} and files["anyOf"][0]["type"] == "array"
    tools = props["tools_used"]
    assert tools["description"] and "description" not in tools["anyOf"][0]
    item = tools["anyOf"][0]["items"]
    assert item["required"] == ["name", "kind", "helped", "note"] and item["additionalProperties"] is False
    assert item["properties"]["name"] == {"type": "string"}
    assert item["properties"]["kind"] == {"type": ["string", "null"], "enum": ["skill", "mcp", "plugin", "cli"]
                                          + [None]}
    tests = props["tests"]["anyOf"][0]
    assert tests["additionalProperties"] is False and set(tests["required"]) == {"command", "passed", "output_tail"}


def test_ref_oneof_untyped_and_unsupported_keywords():
    s = {"type": "object", "properties": {
        "a": {"$ref": "#/$defs/x", "description": "d"}, "b": {"oneOf": [{"type": "string"}, {"type": "integer"}]},
        "c": {"description": "anything"}, "d": {"type": "string", "minLength": 1, "maxLength": 9, "default": "x"},
        "e": {"type": ["string", "null"]}},
        "required": ["d"], "$defs": {"x": {"type": "object", "properties": {"y": {"type": "integer"}}}}}
    out = to_openai_strict(s)
    assert strict_violations(out) == []
    p = out["properties"]
    assert p["a"] == {"description": "d", "anyOf": [{"$ref": "#/$defs/x"}, {"type": "null"}]}
    assert p["b"] == {"anyOf": [{"type": "string"}, {"type": "integer"}, {"type": "null"}]}
    assert p["c"] == {"description": "anything", "anyOf": [{}, {"type": "null"}]}
    assert p["d"] == {"type": "string"}
    assert p["e"] == {"type": ["string", "null"]}
    assert out["$defs"]["x"]["required"] == ["y"] and out["$defs"]["x"]["additionalProperties"] is False


def _full_report():
    return {"status": "done", "summary": "did it", "files_changed": ["a.py"],
            "tests": {"command": "pytest", "passed": True},
            "debts": [{"kind": "todo", "location": "a.py:1", "reason": "later"}],
            "decisions": [{"decision": "x", "impact": "low"}],
            "tools_used": [{"name": "context7"}],
            "question": {"kind": "fyi", "text": "is this ok?"},
            "messages": [{"to": "T-001", "text": "hi"}],
            "harness_feedback": [{"what": "slow verify"}]}


def test_report_with_nulls_parses_like_report_without_them(tmp_path):
    absent = _full_report()
    nulls = json.loads(json.dumps(absent))
    nulls["tests"]["output_tail"] = None
    nulls["debts"][0]["fix"] = None
    nulls["decisions"][0]["why"] = None
    nulls["tools_used"][0].update(kind=None, helped=None, note=None)
    nulls["question"].update(options=None, proceeding_with=None)
    nulls["harness_feedback"][0]["suggestion"] = None
    nulls["notes_for_reviewer"] = None
    a = parse_report(absent, tmp_path, changed_files=["a.py"])
    b = parse_report(nulls, tmp_path, changed_files=["a.py"])
    assert a == b and not b.synthesized
    assert b.tools_used == [{"name": "context7"}] and b.debts == [{"kind": "todo", "location": "a.py:1", "reason": "later"}]
    minimal = {"status": "failed", "summary": "s", **{k: None for k in REPORT_SCHEMA["properties"]
                                                      if k not in ("status", "summary")}}
    m = parse_report(minimal, tmp_path, changed_files=["b.py"])
    assert m == parse_report({"status": "failed", "summary": "s"}, tmp_path, changed_files=["b.py"])
    assert m.files_changed == ["b.py"] and m.question is None and m.tests == {}


def test_verdict_and_proposals_with_nulls(tmp_path):
    v1 = parse_verdict({"verdict": "approve", "summary": "ok",
                        "findings": [{"severity": "low", "issue": "nit", "file": None, "line": None, "fix": None}]},
                       tmp_path)
    v2 = parse_verdict({"verdict": "approve", "summary": "ok", "findings": [{"severity": "low", "issue": "nit"}]},
                       tmp_path)
    assert v1 == v2
    assert parse_verdict({"verdict": "approve", "summary": "ok", "findings": None}, tmp_path).findings == []
    t = {"title": "t", "description": "d", "acceptance": "a", "type": "backend", "importance": "high", "size": "S",
         "milestone": "M1"}
    assert parse_proposals({"tasks": [dict(t, priority=None, depends_on=None, scope=None, host=None)]}, tmp_path) == \
        parse_proposals({"tasks": [t]}, tmp_path)


def test_drop_nulls_keeps_falsy_values():
    assert drop_nulls({"a": None, "b": False, "c": 0, "d": "", "e": [None, {"f": None}]}) == \
        {"b": False, "c": 0, "d": "", "e": [{}]}


def _spec(tmp_path, schema):
    pf = tmp_path / "prompt.md"
    pf.write_text("x")
    (tmp_path / ".swarm-run").mkdir(exist_ok=True)
    return RunSpec(prompt_file=pf, model="m", effort=None, max_turns=3, budget_usd=1.0, timeout_s=60,
                   cwd=tmp_path, schema=schema)


def test_codex_writes_the_strict_schema(tmp_path):
    argv, _ = CodexAdapter(mcp_lookup=lambda: set()).build_command(_spec(tmp_path, REPORT_SCHEMA))
    written = json.loads((tmp_path / ".swarm-run" / "schema.json").read_text())
    assert argv[argv.index("--output-schema") + 1] == str(tmp_path / ".swarm-run" / "schema.json")
    assert written == to_openai_strict(REPORT_SCHEMA) and strict_violations(written) == []


def test_claude_adapter_still_gets_the_original_schema(tmp_path):
    argv, _ = ClaudeAdapter().build_command(_spec(tmp_path, REPORT_SCHEMA))
    assert json.loads(argv[argv.index("--json-schema") + 1]) == REPORT_SCHEMA


def test_smoke_dir_becomes_a_git_repo_and_the_codex_smoke_schema_is_strict(tmp_path, monkeypatch):
    """Codex 0.162 needs a trusted (git) directory and a strict schema for `doctor --smoke` (Q-896)."""
    from swarm import doctor
    from swarm.config import AgentConfig
    from swarm.models import RunResult, Usage

    seen = {}

    class Fake(CodexAdapter):
        def run(self, spec):
            argv, _ = self.build_command(spec)
            seen["schema"] = json.loads((spec.cwd / ".swarm-run" / "schema.json").read_text())
            return RunResult(ok=True, exit_code=0, stdout="", stderr="", structured_output={"status": "done"},
                             usage=Usage())

    agent = AgentConfig(name="codex-c", provider="codex", host="laptop-c", models={"low": "m"})
    cfg = type("C", (), {"agents": {"codex-c": agent}})()
    monkeypatch.setattr("swarm.adapters.get_adapter", lambda a: Fake(a, mcp_lookup=lambda: set()))
    smoke = tmp_path / "_smoke"
    check = doctor.smoke_agent(cfg, "codex-c", smoke)
    assert check.ok and (smoke / ".git").is_dir()
    assert strict_violations(seen["schema"]) == []
    assert doctor.ensure_git_repo(smoke)   # idempotent
