"""Perplexity provider: configurable CLI/argv, rate-limit detection, config load, doctor binary lookup."""
import json
import stat
from pathlib import Path

import pytest
import yaml

from swarm.adapters import get_adapter
from swarm.adapters.base import RunSpec
from swarm.adapters.perplexity import DEFAULT_APPROVE_ARGS, DEFAULT_CLI, PerplexityAdapter
from swarm.config import AgentConfig, ConfigError, load_config
from swarm.doctor import cli_binary, cli_version, run_checks
from swarm.models import PROVIDERS
from swarm.router import PROVIDER_COST_RANK


def spec(tmp_path, **kw):
    pf = tmp_path / "prompt.md"
    pf.write_text("do the thing")
    (tmp_path / ".swarm-run").mkdir(exist_ok=True)
    base = dict(prompt_file=pf, model="sonar-pro", effort=None, max_turns=30, budget_usd=None, timeout_s=60,
                cwd=tmp_path)
    base.update(kw)
    return RunSpec(**base)


def agent(**kw):
    return AgentConfig(name="perplexity-b", provider="perplexity", host="laptop-b", **kw)


def test_registered_and_ranked_between_gemini_and_codex():
    assert "perplexity" in PROVIDERS
    assert isinstance(get_adapter(agent()), PerplexityAdapter)
    assert PROVIDER_COST_RANK["gemini"] < PROVIDER_COST_RANK["perplexity"] < PROVIDER_COST_RANK["codex"]


def test_default_argv_uses_prompt_file_and_approves(tmp_path):
    argv, stdin = PerplexityAdapter(agent()).build_command(spec(tmp_path))
    assert argv[0] == DEFAULT_CLI and stdin is None
    assert argv[argv.index("--prompt-file") + 1] == str(tmp_path / "prompt.md")
    assert argv[argv.index("--model") + 1] == "sonar-pro" and argv[argv.index("--cwd") + 1] == str(tmp_path)
    assert argv[-len(DEFAULT_APPROVE_ARGS):] == DEFAULT_APPROVE_ARGS
    ro, _ = PerplexityAdapter(agent()).build_command(spec(tmp_path, read_only=True))
    assert not set(DEFAULT_APPROVE_ARGS) & set(ro)


def test_configured_cli_and_template(tmp_path):
    a = agent(cli="/opt/pplx/bin/pplx agent", args_template="run --file={prompt_file} -m {model} --dir {cwd}",
              approve_args=["--auto-approve", "all"], extra_args=["--quiet"])
    argv, stdin = PerplexityAdapter(a).build_command(spec(tmp_path, extra_args=["--x"]))
    assert argv == ["/opt/pplx/bin/pplx", "agent", "run", f"--file={tmp_path / 'prompt.md'}", "-m", "sonar-pro",
                    "--dir", str(tmp_path), "--auto-approve", "all", "--x"] and stdin is None
    argv2, _ = PerplexityAdapter(agent(approve_args=[])).build_command(spec(tmp_path))
    assert argv2[-1] == "json"   # approve_args: [] adds nothing


def test_template_without_prompt_file_pipes_stdin(tmp_path):
    argv, stdin = PerplexityAdapter(agent(args_template="--model {model}")).build_command(spec(tmp_path))
    assert argv[:3] == ["perplexity", "--model", "sonar-pro"] and stdin == b"do the thing"


def test_parse_output_usage_report_and_rate_limits():
    a = PerplexityAdapter(agent())
    out = json.dumps({"result": 'done.\n{"status":"done","summary":"ok"}',
                      "usage": {"prompt_tokens": 12, "completion_tokens": 5}, "cost_usd": 0.02, "session_id": "s9"})
    r = a.parse_output(0, out, "")
    assert r.ok and r.structured_output == {"status": "done", "summary": "ok"} and r.session_id == "s9"
    assert r.usage.input_tokens == 12 and r.usage.output_tokens == 5 and r.usage.cost_usd == 0.02
    r2 = a.parse_output(1, json.dumps({"error": {"message": "429 Too Many Requests"}}), "")
    assert not r2.ok and r2.rate_limited and "429" in r2.error
    r3 = a.parse_output(1, "Error: rate_limit_exceeded, try again in 20 minutes", "")
    assert r3.rate_limited
    r4 = a.parse_output(2, "", "SyntaxError: bad flag")
    assert not r4.ok and not r4.rate_limited
    # a successful run that talks about capacity is not a rate limit
    assert not a.parse_output(0, "raised server capacity", "").rate_limited


def test_run_with_fake_cli_rate_limited_on_stderr(tmp_path):
    script = tmp_path / "pplx.sh"
    script.write_text("#!/bin/bash\necho 'quota exceeded: try again in 2 hours' >&2\nexit 1\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    r = PerplexityAdapter(agent(cli=str(script), args_template="{prompt_file}")).run(spec(tmp_path))
    assert not r.ok and r.rate_limited and r.usage_limited and r.reset_at is not None


def test_run_with_fake_cli_ok(tmp_path):
    script = tmp_path / "pplx.sh"
    script.write_text('#!/bin/bash\n[ "$PWD" = "$3" ] || exit 9\ncp /dev/null .swarm-run/report.json\n'
                      'echo \'{"result":"ok"}\'\n')
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    r = PerplexityAdapter(agent(cli=str(script), args_template="{prompt_file} {model} {cwd}", approve_args=[])
                          ).run(spec(tmp_path))
    assert r.ok, r.stderr + r.error
    assert (tmp_path / ".swarm-run" / "report.json").exists()


def _with_perplexity(sample_config_dict, **extra):
    sample_config_dict["hosts"]["laptop-b"] = {"max_parallel": {"perplexity": 1}}
    sample_config_dict["agents"]["perplexity-b"] = {
        "provider": "perplexity", "host": "laptop-b",
        "models": {"best": "sonar-pro", "high": "sonar-pro", "mid": "sonar", "low": "sonar"},
        "strengths": {"research": 5, "docs": 4}, **extra}
    return sample_config_dict


def _load(project_dir, d):
    (project_dir / ".swarm" / "config.yaml").write_text(yaml.safe_dump(d))
    return load_config(project_dir / ".swarm" / "config.yaml")


def test_config_loads_perplexity_agent(project_dir, sample_config_dict):
    cfg = _load(project_dir, _with_perplexity(sample_config_dict, cli="pplx", approve_args=["--yes"],
                                              args_template="-p --prompt-file {prompt_file} --model {model}"))
    a = cfg.agents["perplexity-b"]
    assert a.provider == "perplexity" and a.cli == "pplx" and a.approve_args == ["--yes"]
    assert a.args_template.endswith("{model}") and cfg.hosts["laptop-b"].max_parallel == {"perplexity": 1}
    d = _load(project_dir, _with_perplexity(sample_config_dict)).agents["perplexity-b"]
    assert d.cli is None and d.args_template is None and d.approve_args is None   # adapter defaults apply


def test_config_rejects_unknown_placeholder_and_bad_values(project_dir, sample_config_dict):
    with pytest.raises(ConfigError, match="unknown placeholder"):
        _load(project_dir, _with_perplexity(sample_config_dict, args_template="--in {prompt} --model {model}"))
    with pytest.raises(ConfigError, match="approve_args"):
        _load(project_dir, _with_perplexity(sample_config_dict, approve_args="--yes"))
    with pytest.raises(ConfigError, match="cli"):
        _load(project_dir, _with_perplexity(sample_config_dict, cli="  "))


def test_doctor_looks_up_the_configured_cli(project_dir, sample_config_dict):
    cfg = _load(project_dir, _with_perplexity(sample_config_dict, cli="pplx-agent --profile x"))
    assert cli_binary(cfg.agents["perplexity-b"]) == "pplx-agent"
    assert cli_binary(agent()) == "perplexity"
    seen = []

    def which(name):
        seen.append(name)
        return f"/usr/local/bin/{name}" if name == "pplx-agent" else None

    class R:
        ok, out, err = True, "pplx 0.1", ""
    checks = {c.name: c for c in run_checks(cfg, "laptop-b", offline=True, notion_token="", which=which,
                                              run=lambda *a, **k: R(), platform="linux")}
    c = checks["cli:perplexity-b (perplexity)"]
    assert c.ok and "pplx-agent" in seen and "unverified" in c.detail
    assert cli_version(cfg.agents["perplexity-b"], which=which, run=lambda *a, **k: R()) == "pplx 0.1"
    checks = {c.name: c for c in run_checks(cfg, "laptop-b", offline=True, notion_token="", which=lambda n: None,
                                              run=lambda *a, **k: R(), platform="linux")}
    assert not checks["cli:perplexity-b (perplexity)"].ok


def test_template_config_still_loads():
    import shutil
    import tempfile
    src = Path(__file__).resolve().parent.parent / "swarm" / "template" / ".swarm" / "config.yaml"
    raw = yaml.safe_load(src.read_text())
    assert raw["hosts"]["laptop-b"]["max_parallel"]["perplexity"] == 1
    with tempfile.TemporaryDirectory() as d:
        (Path(d) / ".swarm").mkdir()
        shutil.copy(src, Path(d) / ".swarm" / "config.yaml")
        load_config(Path(d) / ".swarm" / "config.yaml")
