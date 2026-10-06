import json
import stat
from datetime import datetime, timedelta, timezone

from swarm.adapters import get_adapter
from swarm.adapters.antigravity import AntigravityAdapter
from swarm.adapters.base import RATE_LIMIT_RE, RunSpec, parse_reset_at
from swarm.adapters.claude import ClaudeAdapter
from swarm.adapters.codex import CodexAdapter
from swarm.adapters.gemini import GeminiAdapter
from swarm.adapters.generic import GenericAdapter
from swarm.adapters.grok import GrokAdapter
from swarm.config import AgentConfig


def spec(tmp_path, **kw):
    pf = tmp_path / "prompt.md"
    pf.write_text("do the thing")
    base = dict(prompt_file=pf, model="m", effort="high", max_turns=30, budget_usd=3.0, timeout_s=60,
                cwd=tmp_path, schema={"type": "object"})
    base.update(kw)
    (tmp_path / ".swarm-run").mkdir(exist_ok=True)
    return RunSpec(**base)


def test_claude_command_and_parse(tmp_path):
    a = ClaudeAdapter()
    argv, stdin = a.build_command(spec(tmp_path))
    assert argv[:3] == ["claude", "-p", "--output-format"] and "json" in argv
    assert argv[argv.index("--permission-mode") + 1] == "bypassPermissions"
    assert argv[argv.index("--model") + 1] == "m" and argv[argv.index("--effort") + 1] == "high"
    assert argv[argv.index("--max-turns") + 1] == "30" and "--json-schema" in argv
    assert stdin == b"do the thing"
    ro, _ = a.build_command(spec(tmp_path, read_only=True))
    assert ro[ro.index("--permission-mode") + 1] == "dontAsk" and "--allowedTools" in ro
    out = json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "done",
                      "structured_output": {"status": "done"}, "total_cost_usd": 0.12,
                      "usage": {"input_tokens": 100, "output_tokens": 20}, "session_id": "s1"})
    r = a.parse_output(0, out, "")
    assert r.ok and r.structured_output == {"status": "done"} and r.usage.cost_usd == 0.12
    assert r.usage.input_tokens == 100 and r.session_id == "s1"
    bad = json.dumps({"type": "result", "subtype": "error_max_turns", "is_error": True, "result": "hit max turns"})
    r2 = a.parse_output(1, bad, "")
    assert not r2.ok and "max_turns" in r2.error
    rl = json.dumps({"type": "result", "subtype": "error_during_execution", "is_error": True,
                     "result": "You've hit your session limit · resets 3pm"})
    r3 = a.parse_output(1, rl, "")
    assert r3.rate_limited


def test_codex_command_and_parse(tmp_path):
    a = CodexAdapter(AgentConfig(name="c", provider="codex", host="h", sandbox="workspace-write"))
    argv, stdin = a.build_command(spec(tmp_path))
    assert argv[:3] == ["codex", "exec", "--json"] and argv[-1] == "-" and stdin == b"do the thing"
    assert argv[argv.index("--sandbox") + 1] == "workspace-write"
    assert argv[argv.index("-c") + 1] == 'model_reasoning_effort="high"'
    assert (tmp_path / ".swarm-run" / "schema.json").exists() and "--output-schema" in argv
    ro, _ = a.build_command(spec(tmp_path, read_only=True))
    assert ro[ro.index("--sandbox") + 1] == "read-only"
    lines = [
        {"type": "thread.started", "thread_id": "th1"},
        {"type": "item.completed", "item": {"type": "agent_message",
                                            "text": json.dumps({"status": "done", "summary": "ok"})}},
        {"type": "turn.completed", "usage": {"input_tokens": 50, "cached_input_tokens": 10, "output_tokens": 7}},
    ]
    r = a.parse_output(0, "\n".join(json.dumps(line) for line in lines), "")
    assert r.ok and r.structured_output["summary"] == "ok" and r.session_id == "th1"
    assert r.usage.input_tokens == 50 and r.usage.output_tokens == 7 and r.usage.cost_usd is None
    fail = json.dumps({"type": "error", "message": "Rate limit reached for gpt-6-sol"})
    r2 = a.parse_output(1, fail, "")
    assert not r2.ok and r2.rate_limited


def test_antigravity_gemini_grok_generic_commands(tmp_path):
    ag, _ = AntigravityAdapter().build_command(spec(tmp_path))
    assert ag[0] == "agy" and ag[ag.index("--model") + 1] == "m" and "--dangerously-skip-permissions" in ag
    r = AntigravityAdapter().parse_output(0, json.dumps({"response": "hi", "stats": {}}), "")
    assert r.ok and r.structured_output is None
    ge, _ = GeminiAdapter().build_command(spec(tmp_path))
    assert ge[0] == "gemini" and ge[ge.index("-m") + 1] == "m"
    gr, _ = GrokAdapter().build_command(spec(tmp_path))
    assert gr[0] == "grok" and gr[gr.index("--prompt-file") + 1].endswith("prompt.md") and "--cwd" in gr
    rg = GrokAdapter().parse_output(0, json.dumps({"text": "t", "usage": {"input_tokens": 3, "output_tokens": 4},
                                                  "total_cost_usd": 0.01}), "")
    assert rg.ok and rg.usage.cost_usd == 0.01 and rg.usage.output_tokens == 4
    gen = GenericAdapter(AgentConfig(name="g", provider="generic", host="h",
                                     command_template="bash run.sh {prompt_file} {model} {cwd}"))
    argv, stdin = gen.build_command(spec(tmp_path))
    assert argv[:2] == ["bash", "run.sh"] and argv[3] == "m" and stdin is None


def test_get_adapter_dispatch():
    for provider, cls in [("claude", ClaudeAdapter), ("codex", CodexAdapter), ("antigravity", AntigravityAdapter),
                          ("gemini", GeminiAdapter), ("grok", GrokAdapter)]:
        assert isinstance(get_adapter(AgentConfig(name="x", provider=provider, host="h")), cls)
    assert isinstance(get_adapter(AgentConfig(name="x", provider="generic", host="h",
                                              command_template="x {prompt_file}")), GenericAdapter)


def test_run_with_fake_cli_and_timeout(tmp_path):
    script = tmp_path / "fake.sh"
    script.write_text(
        '#!/bin/bash\ncat > /dev/null\necho \'{"type":"result","subtype":"success","is_error":false,'
        '"result":"ok","structured_output":{"status":"done"},"usage":{"input_tokens":1,"output_tokens":1},'
        '"total_cost_usd":0.0}\'\n')
    script.chmod(script.stat().st_mode | stat.S_IEXEC)

    class FakeClaude(ClaudeAdapter):
        def build_command(self, spec):
            argv, stdin = super().build_command(spec)
            return ["bash", str(script)], stdin

    r = FakeClaude().run(spec(tmp_path))
    assert r.ok and r.structured_output == {"status": "done"}

    slow = tmp_path / "slow.sh"
    slow.write_text("#!/bin/bash\nsleep 5\n")
    gen = GenericAdapter(AgentConfig(name="g", provider="generic", host="h",
                                     command_template=f"bash {slow} {{prompt_file}}"))
    r2 = gen.run(spec(tmp_path, timeout_s=1))
    assert r2.timed_out and not r2.ok


def test_missing_cli_is_reported(tmp_path):
    gen = GenericAdapter(AgentConfig(name="g", provider="generic", host="h",
                                     command_template="definitely-not-a-cli-xyz {prompt_file}"))
    r = gen.run(spec(tmp_path))
    assert not r.ok and "not found" in r.error


def test_rate_limit_regex_and_reset():
    assert RATE_LIMIT_RE.search("Error: 429 Too Many Requests")
    assert RATE_LIMIT_RE.search("You've hit your weekly limit")
    assert not RATE_LIMIT_RE.search("all good")
    assert parse_reset_at("resets at 2026-10-10T18:00:00Z").isoformat() == "2026-10-10T18:00:00+00:00"
    assert parse_reset_at("nothing here") is None


def test_codex_adds_git_common_dir_as_writable(git_repo, tmp_path):
    """Inside a git worktree the index and locks live under the main repo's .git; Codex's sandbox must be told."""
    import subprocess
    subprocess.run(["git", "-C", str(git_repo), "worktree", "add", "-q", str(tmp_path / "wt"), "-b", "x"], check=True)
    wt = tmp_path / "wt"
    (wt / ".swarm-run").mkdir()
    pf = wt / "prompt.md"
    pf.write_text("go")
    argv, _ = CodexAdapter().build_command(RunSpec(prompt_file=pf, model="m", effort=None, max_turns=5,
                                                   budget_usd=None, timeout_s=10, cwd=wt))
    assert "--add-dir" in argv
    added = argv[argv.index("--add-dir") + 1]
    assert added.endswith("/.git") and (git_repo / ".git").resolve() == __import__("pathlib").Path(added).resolve()


def test_claude_workers_skip_user_mcp_servers(tmp_path):
    argv, _ = ClaudeAdapter().build_command(spec(tmp_path))
    assert "--strict-mcp-config" in argv
    assert json.loads(argv[argv.index("--mcp-config") + 1]) == {"mcpServers": {}}


def test_claude_loads_only_requested_mcp_servers(tmp_path):
    known = {"magic": {"type": "http", "url": "https://magic.example/mcp"},
             "motion": {"command": "npx", "args": ["motion-mcp"]}}
    a = ClaudeAdapter(mcp_lookup=lambda: known)
    argv, _ = a.build_command(spec(tmp_path, mcp=["magic", "nope"]))
    cfg = json.loads(argv[argv.index("--mcp-config") + 1])
    assert cfg == {"mcpServers": {"magic": known["magic"]}} and "--strict-mcp-config" in argv


def test_claude_loads_task_plugins_with_plugin_dir(tmp_path):
    argv, _ = ClaudeAdapter(mcp_lookup=dict).build_command(
        spec(tmp_path, plugin_dirs=["/cache/hf/1.0", "/cache/mwg/2"]))
    assert argv[argv.index("--plugin-dir") + 1] == "/cache/hf/1.0"
    assert argv.count("--plugin-dir") == 2 and "/cache/mwg/2" in argv
    argv2, _ = ClaudeAdapter(mcp_lookup=dict).build_command(spec(tmp_path))
    assert "--plugin-dir" not in argv2


def test_claude_passes_settings_and_inline_mcp_definitions(tmp_path):
    inline = {"playwright": {"command": "npx", "args": ["@playwright/mcp@latest", "--headless"]}}
    a = ClaudeAdapter(mcp_lookup=lambda: {"playwright": {"command": "old"}, "context7": {"type": "http", "url": "u"}})
    argv, _ = a.build_command(spec(tmp_path, mcp=["playwright", "context7"], mcp_servers=inline,
                                   settings={"enabledPlugins": {"x@m": False}}))
    cfg = json.loads(argv[argv.index("--mcp-config") + 1])
    assert cfg["mcpServers"]["playwright"] == inline["playwright"] and "context7" in cfg["mcpServers"]
    assert json.loads(argv[argv.index("--settings") + 1]) == {"enabledPlugins": {"x@m": False}}
    argv2, _ = ClaudeAdapter(mcp_lookup=dict).build_command(spec(tmp_path))
    assert "--settings" not in argv2


def test_codex_enables_only_registered_mcp_servers_per_run(tmp_path):
    a = CodexAdapter(mcp_lookup=lambda: {"context7"})
    argv, _ = a.build_command(spec(tmp_path, mcp=["context7", "playwright"]))
    assert "mcp_servers.context7.enabled=true" in argv
    assert argv[argv.index("mcp_servers.context7.enabled=true") - 1] == "-c"
    assert not any("playwright" in x for x in argv)   # not registered on this laptop: never break the run
    argv2, _ = CodexAdapter(mcp_lookup=lambda: set()).build_command(spec(tmp_path, mcp=["context7"]))
    assert not any(x.startswith("mcp_servers.") for x in argv2)
    argv3, _ = CodexAdapter(mcp_lookup=lambda: {"context7"}).build_command(spec(tmp_path))
    assert not any(x.startswith("mcp_servers.") for x in argv3)


def test_codex_registered_servers_parse_mcp_list(tmp_path):
    from swarm.adapters.codex import registered_mcp_servers
    from swarm.workspace import CmdResult
    good = lambda args, cwd=None, timeout=60: CmdResult(0, json.dumps({"servers": [{"name": "context7"}, {"name": "pw"}]}), "")  # noqa: E731
    assert registered_mcp_servers(run=good) == {"context7", "pw"}
    flat = lambda args, cwd=None, timeout=60: CmdResult(0, json.dumps([{"name": "context7"}]), "")  # noqa: E731
    assert registered_mcp_servers(run=flat) == {"context7"}
    assert registered_mcp_servers(run=lambda a, cwd=None, timeout=60: CmdResult(1, "", "boom")) == set()
    assert registered_mcp_servers(run=lambda a, cwd=None, timeout=60: CmdResult(0, "not json", "")) == set()


def test_claude_parse_keeps_cache_tokens(tmp_path):
    out = json.dumps({"type": "result", "result": "{\"status\":\"done\"}", "is_error": False, "num_turns": 3,
                      "total_cost_usd": 0.02, "structured_output": {"status": "done"},
                      "usage": {"input_tokens": 19, "output_tokens": 300, "cache_creation_input_tokens": 10755,
                                "cache_read_input_tokens": 36675}})
    r = ClaudeAdapter(mcp_lookup=dict).parse_output(0, out, "")
    assert r.usage.cache_write_tokens == 10755 and r.usage.cache_read_tokens == 36675


def test_run_retries_once_when_the_cli_binary_is_briefly_missing(tmp_path):
    """Claude Code replaces its own binary during auto-update; a spawn that hits that window is retried once."""
    import subprocess
    from swarm.adapters.generic import GenericAdapter
    from swarm.config import AgentConfig
    calls = []

    def flaky(argv, **kw):
        calls.append(argv)
        if len(calls) == 1:
            raise FileNotFoundError(2, "No such file or directory", argv[0])
        return subprocess.CompletedProcess(argv, 0, stdout=b"", stderr=b"")

    a = GenericAdapter(AgentConfig(name="g", provider="generic", host="h", command_template="mycli {prompt_file}"))
    slept = []
    r = a.run(spec(tmp_path, schema=None), runner=flaky, sleep=slept.append)
    assert r.ok and len(calls) == 2 and slept == [5]

    def gone(argv, **kw):
        raise FileNotFoundError(2, "No such file or directory", argv[0])

    r2 = a.run(spec(tmp_path, schema=None), runner=gone, sleep=lambda s: None)
    assert not r2.ok and r2.exit_code == -2 and "cli not found" in r2.error


def test_gemini_headless_skips_the_trust_prompt(tmp_path):
    """Gemini CLI exits 55 ('not running in a trusted directory') in headless mode; every worktree is a new
    directory, so the run must skip the trust check."""
    from swarm.adapters.gemini import GeminiAdapter
    argv, _ = GeminiAdapter().build_command(spec(tmp_path, model="gemini-3.1-pro"))
    assert "--skip-trust" in argv and argv[argv.index("-m") + 1] == "gemini-3.1-pro"
    assert "--approval-mode" in argv and argv[argv.index("--approval-mode") + 1] == "yolo"
    ro, _ = GeminiAdapter().build_command(spec(tmp_path, model="gemini-3.1-pro", read_only=True))
    assert "--skip-trust" in ro and "--approval-mode" not in ro


def test_gemini_reads_a_json_report_out_of_the_response_text():
    """Gemini has no --json-schema; when the model answers with the report JSON as text, that is the report."""
    from swarm.adapters.gemini import GeminiAdapter
    out = json.dumps({"session_id": "s", "response": 'Here it is:\n{"status":"done","summary":"smoke ok"}',
                      "stats": {"models": {}}})
    r = GeminiAdapter().parse_output(0, out, "")
    assert r.ok and r.structured_output == {"status": "done", "summary": "smoke ok"}
    r2 = GeminiAdapter().parse_output(0, json.dumps({"session_id": "s", "response": "OK"}), "")
    assert r2.ok and r2.structured_output is None
    r3 = GeminiAdapter().parse_output(1, json.dumps({"session_id": "s", "error": {"message": "quota"}}), "")
    assert not r3.ok and "quota" in r3.error


def test_gemini_parses_token_stats_into_usage():
    from swarm.adapters.gemini import GeminiAdapter
    out = json.dumps({"session_id": "s", "response": '{"status":"done","summary":"ok"}', "stats": {"models": {
        "gemini-3-flash-preview": {"api": {"totalRequests": 11}, "tokens": {"input": 57730, "prompt": 212048,
                                                                            "candidates": 2422, "total": 215962,
                                                                            "cached": 154318, "thoughts": 1492, "tool": 0}}}}})
    r = GeminiAdapter().parse_output(0, out, "")
    assert r.usage.input_tokens == 212048 and r.usage.output_tokens == 2422 + 1492
    assert r.usage.cache_read_tokens == 154318 and r.usage.cost_usd is None   # subscription: no dollar figure


def test_antigravity_command_matches_the_real_cli(tmp_path):
    """agy 1.2.12 (Sep 2026): -p/--print, --output-format json, --json-schema, --model, --effort,
    --dangerously-skip-permissions, --print-timeout, --add-dir. Prompt is passed as the -p argument."""
    from swarm.adapters.antigravity import AntigravityAdapter
    s = spec(tmp_path, model="gemini-3-pro", effort="high", timeout_s=600)
    argv, stdin = AntigravityAdapter().build_command(s)
    assert argv[0] == "agy" and argv[argv.index("-p") + 1] == "do the thing"
    assert argv[argv.index("--output-format") + 1] == "json"
    assert argv[argv.index("--model") + 1] == "gemini-3-pro" and argv[argv.index("--effort") + 1] == "high"
    assert "--dangerously-skip-permissions" in argv and argv[argv.index("--print-timeout") + 1] == "600s"
    assert "--json-schema" in argv and stdin is None
    ro, _ = AntigravityAdapter().build_command(spec(tmp_path, model="m", read_only=True, schema=None))
    assert "--dangerously-skip-permissions" not in ro and "--json-schema" not in ro
    assert "--effort" not in AntigravityAdapter().build_command(spec(tmp_path, model="m", effort=None))[0]


def test_run_stops_the_cli_when_asked(tmp_path):
    """Claim watchdog: when the board hands a task to someone else (agent looked offline), the running CLI is
    killed instead of burning tokens on work that will be thrown away at publish time."""
    from swarm.adapters.generic import GenericAdapter
    from swarm.config import AgentConfig
    script = tmp_path / "slow.sh"
    script.write_text("#!/bin/sh\nsleep 30\necho done\n")
    script.chmod(0o755)
    a = GenericAdapter(AgentConfig(name="g", provider="generic", host="h", command_template=f"{script} {{prompt_file}}"))
    polls = {"n": 0}

    def should_stop():
        polls["n"] += 1
        return polls["n"] >= 2
    import time
    t0 = time.time()
    r = a.run(spec(tmp_path, schema=None, timeout_s=60, should_stop=should_stop, stop_poll_s=0.2))
    assert not r.ok and r.exit_code == -3 and "stopped" in r.error and time.time() - t0 < 10


def test_agent_env_reaches_the_cli_process(project_dir, sample_config_dict, tmp_path):
    """A second Claude account on the same laptop runs under its own CLAUDE_CONFIG_DIR (Oct 4 2026)."""
    import subprocess, yaml
    from swarm.adapters import get_adapter
    from swarm.adapters.base import RunSpec
    from swarm.config import load_config
    sample_config_dict["agents"]["claude-a"]["env"] = {"CLAUDE_CONFIG_DIR": "/tmp/claude-pro"}
    (project_dir / ".swarm" / "config.yaml").write_text(yaml.safe_dump(sample_config_dict))
    cfg = load_config(project_dir / ".swarm" / "config.yaml")
    assert cfg.agents["claude-a"].env == {"CLAUDE_CONFIG_DIR": "/tmp/claude-pro"}
    seen = {}

    def fake_runner(argv, **kw):
        seen.update(kw)
        return subprocess.CompletedProcess(argv, 0, b'{"result": "{\\"status\\":\\"done\\",\\"summary\\":\\"x\\"}"}', b"")
    pf = tmp_path / "p.md"; pf.write_text("hi")
    (tmp_path / ".swarm-run").mkdir(exist_ok=True)
    get_adapter(cfg.agents["claude-a"]).run(RunSpec(prompt_file=pf, model="sonnet", effort=None, max_turns=1, budget_usd=1.0, timeout_s=60, cwd=tmp_path), runner=fake_runner)
    assert seen["env"]["CLAUDE_CONFIG_DIR"] == "/tmp/claude-pro" and "PATH" in seen["env"]


def test_antigravity_skips_effort_when_the_model_id_carries_it(cfg, tmp_path):
    """agy rejects `--model gemini-3.1-pro-high --effort medium` (T-022, Oct 5 2026)."""
    from swarm.adapters.antigravity import AntigravityAdapter
    from swarm.adapters.base import RunSpec
    from swarm.config import AgentConfig
    a = AgentConfig(name="agy", provider="antigravity", host="host-b", models={"best": "x", "high": "x", "mid": "x", "low": "x"})
    pf = tmp_path / "p.md"; pf.write_text("hi")
    argv, _ = AntigravityAdapter(a).build_command(RunSpec(prompt_file=pf, model="gemini-3.1-pro-high", effort="medium",
                                                           max_turns=3, budget_usd=1.0, timeout_s=60, cwd=tmp_path))
    assert "--effort" not in argv
    argv, _ = AntigravityAdapter(a).build_command(RunSpec(prompt_file=pf, model="gemini-3.1-pro", effort="medium",
                                                           max_turns=3, budget_usd=1.0, timeout_s=60, cwd=tmp_path))
    assert "--effort" in argv


CODEX_PLAN_MSG = ("You've hit your usage limit. Upgrade to Pro (https://chatgpt.com/explore/pro), visit "
                  "https://chatgpt.com/codex/settings/usage to purchase more credits or try again at 5:03 AM.")


def test_codex_usage_limit_is_a_long_cooldown_with_the_wall_clock_reset():
    """Oct 6 2026: codex-b's plan ran out; "try again at 5:03 AM" was not parsed, so it cooled down for 15 minutes
    and was handed two more tasks that failed the same way."""
    a = CodexAdapter(AgentConfig(name="c", provider="codex", host="h"))
    out = "\n".join(json.dumps(e) for e in ({"type": "error", "message": CODEX_PLAN_MSG},
                                            {"type": "turn.failed", "error": {"message": CODEX_PLAN_MSG}}))
    r = a.parse_output(1, out, "")
    assert r.rate_limited and r.usage_limited
    local = r.reset_at.astimezone()
    assert (local.hour, local.minute) == (5, 3) and r.reset_at > datetime.now(timezone.utc)
    plain = a.parse_output(1, json.dumps({"type": "error", "message": "Rate limit reached for gpt-6-sol"}), "")
    assert plain.rate_limited and not plain.usage_limited


def test_claude_usage_limit_phrases():
    a = ClaudeAdapter(AgentConfig(name="c", provider="claude", host="h"), mcp_lookup=lambda: {})
    for text in ("Claude AI usage limit reached|1760000000", "You've hit your usage limit", "Quota exceeded for plan",
                 "5-hour limit reached · resets 3am (America/New_York)", "plan limit reached"):
        r = a.parse_output(1, json.dumps({"type": "result", "subtype": "error_during_execution", "is_error": True,
                                          "result": text}), "")
        assert r.rate_limited and r.usage_limited, text
    epoch = a.parse_output(1, json.dumps({"type": "result", "subtype": "x", "is_error": True,
                                          "result": "Claude AI usage limit reached|1760000000"}), "")
    assert epoch.reset_at == datetime.fromtimestamp(1760000000, tz=timezone.utc)


def test_parse_reset_at_wall_clock_zone_and_duration():
    now = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)                     # 08:00 in New York
    assert parse_reset_at("resets 3am (America/New_York)", now) == datetime(2026, 10, 7, 7, 0, tzinfo=timezone.utc)
    assert parse_reset_at("resets 11:30 AM (America/New_York)", now) == datetime(2026, 10, 6, 15, 30, tzinfo=timezone.utc)
    assert parse_reset_at("try again in 2 hours 15 minutes", now) == now + timedelta(hours=2, minutes=15)
    assert parse_reset_at("try again at 5:03 AM", now) > now
    assert parse_reset_at("wait until 3 then go", now) is None
