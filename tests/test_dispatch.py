"""`swarm dispatch` / `swarm jobs` (HackRU Oct 10-12 2026: tools/a2.sh passed prompts on argv so backticks ran in the
shell; jobs shared the main checkout and swept each other's staged files; a session limit killed jobs that were
restarted by hand)."""
import json
import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

from swarm.config import load_config
from swarm.dispatch import Dispatcher
from swarm.limits import LimitStore
from swarm.models import RunResult
from swarm.workspace import Workspace


def _git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True).stdout


@pytest.fixture
def dcfg(git_repo, sample_config_dict):
    d = dict(sample_config_dict)
    d.update({"worktree_root": "../wt", "verify": {"fast": "scripts/verify_fast.sh"},
              "accounts": {"max": {}, "pro": {"env": {"CLAUDE_CONFIG_DIR": "/tmp/pro"}}},
              "dispatch": {"account": "max", "model": "opus"}, "model_fallbacks": {"opus": "sonnet"}})
    (git_repo / ".swarm").mkdir(exist_ok=True)
    (git_repo / ".swarm" / "config.yaml").write_text(yaml.safe_dump(d))
    return load_config(git_repo / ".swarm" / "config.yaml")


class Clock:
    def __init__(self):
        self.t = datetime(2026, 10, 11, 14, 0, tzinfo=timezone.utc)
        self.slept = []

    def now(self):
        return self.t

    def sleep(self, s):
        self.slept.append(s)
        self.t += timedelta(seconds=s)


class FakeAdapter:
    """Plays the agent: each call pops the next behaviour; records what it was given."""
    calls: list = []

    def __init__(self, behaviours):
        self.behaviours = behaviours

    def factory(self, agent_cfg):
        outer = self

        class A:
            def run(self, spec):
                FakeAdapter.calls.append({"account": agent_cfg.name, "model": spec.model, "env": dict(agent_cfg.env),
                                          "prompt": spec.prompt_file.read_text(), "cwd": spec.cwd,
                                          "job_env": dict(spec.env)})
                return outer.behaviours.pop(0)(spec)
        return A()


def ok_writes(name="out.txt", msg="add out", trailer=True):
    def run(spec):
        (Path(spec.cwd) / name).write_text("hello\n")
        _git(spec.cwd, "add", "-A")
        m = msg + ("\n\nCo-Authored-By: Claude <noreply@anthropic.com>\nClaude-Session: https://claude.ai/code/x"
                   if trailer else "")
        subprocess.run(["git", "-C", str(spec.cwd), "-c", "user.email=a@b", "-c", "user.name=a", "commit", "-qm", m],
                       check=True, env={**os.environ, "SWARM_KEEP_TRAILERS": "1"})
        return RunResult(ok=True, exit_code=0, stdout=json.dumps({"result": "did the thing"}), stderr="")
    return run


def limited(text, reset=None):
    def run(spec):
        return RunResult(ok=False, exit_code=1, stdout="", stderr=text, rate_limited=True, usage_limited=True,
                         reset_at=reset, error=text)
    return run


def make(dcfg, behaviours, tmp_path):
    FakeAdapter.calls = []
    clock = Clock()
    ws = Workspace(dcfg.repo_root, dcfg.worktree_root)
    ws.hook_options = {"main": None}
    d = Dispatcher(dcfg, ws, adapter_factory=FakeAdapter(behaviours).factory, now=clock.now, sleep=clock.sleep,
                   limits=LimitStore(tmp_path / "limits.json", now=clock.now), log=lambda m: None,
                   root=tmp_path / "jobs")
    return d, clock


def test_prompt_with_backticks_reaches_the_real_cli_on_stdin_untouched(dcfg, tmp_path):
    """End to end with the real Claude adapter and a fake `claude` on PATH: nothing in the prompt is run."""
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    got = tmp_path / "got.txt"
    (fakebin / "claude").write_text(f"#!/bin/sh\ncat > {got}\necho \"$@\" > {got}.argv\n"
                                    "echo '{\"type\":\"result\",\"subtype\":\"success\",\"is_error\":false,"
                                    "\"result\":\"report ok\"}'\n")
    (fakebin / "claude").chmod(0o755)
    dcfg.accounts["max"].env["PATH"] = f"{fakebin}:{os.environ['PATH']}"
    ws = Workspace(dcfg.repo_root, dcfg.worktree_root)
    d = Dispatcher(dcfg, ws, log=lambda m: None, root=tmp_path / "jobs",
                   limits=LimitStore(tmp_path / "limits.json"))
    pwned = tmp_path / "pwned"
    prompt = f"Check `touch {pwned}` and $(touch {pwned}) and \"quotes\" 'single' \\ backslash"
    job = d.run(d.create("bt-job", prompt))
    assert job.status == "done" and job.report == "report ok"
    assert got.read_text().startswith(prompt) and "## Job rules" in got.read_text()
    assert not pwned.exists() and prompt not in Path(f"{got}.argv").read_text()
    assert (tmp_path / "jobs" / "bt-job" / "prompt.md").read_text() == prompt


def test_each_job_gets_its_own_worktree_and_never_touches_the_main_checkout(dcfg, tmp_path):
    repo = dcfg.repo_root
    (repo / "staged.txt").write_text("someone else's staged work\n")
    _git(repo, "add", "staged.txt")
    d, _ = make(dcfg, [ok_writes("a.txt", "job a"), ok_writes("b.txt", "job b")], tmp_path)
    a = d.run(d.create("job-a", "do a"))
    b = d.run(d.create("job-b", "do b"))
    assert a.status == b.status == "done"
    cwds = {str(c["cwd"]) for c in FakeAdapter.calls}
    assert len(cwds) == 2 and all("/jobs/" in c for c in cwds) and str(repo) not in cwds
    assert "staged.txt" in _git(repo, "diff", "--cached", "--name-only")          # still staged, still ours
    assert _git(repo, "ls-tree", "-r", "--name-only", "origin/job/job-a").count("staged.txt") == 0
    assert a.pushed_to == "origin/job/job-a" and b.pushed_to == "origin/job/job-b"
    log = _git(repo, "log", "--format=%B", "origin/job/job-a")
    assert "Claude-Session" not in log and "anthropic" not in log and "job a" in log
    assert not Path(a.worktree).exists()        # landed and cleaned up
    assert "did the thing" in (tmp_path / "jobs" / "job-a" / "report.md").read_text()


def test_session_limit_waits_for_the_reset_and_retries(dcfg, tmp_path):
    d, clock = make(dcfg, [], tmp_path)
    reset = clock.t + timedelta(minutes=105)
    dcfg.accounts["max"].fallback = []
    dcfg.model_fallbacks.clear()
    FakeAdapter.calls = []
    d.adapter_factory = FakeAdapter([limited("You've hit your session limit · resets 3:45pm", reset),
                                     ok_writes(trailer=False)]).factory
    job = d.run(d.create("limited", "work"))
    assert job.status == "done" and len(job.attempts) == 2 and job.limit_waits == 1
    assert sum(clock.slept) >= 105 * 60 and clock.t >= reset
    assert job.attempts[0]["limited_until"].startswith("2026-10-11T15:45")


def test_a_limited_account_falls_over_to_its_fallback_account_at_once(dcfg, tmp_path):
    dcfg.accounts["max"].fallback = ["pro"]
    d, clock = make(dcfg, [limited("You've hit your session limit · resets 3:45pm"), ok_writes(trailer=False)],
                    tmp_path)
    job = d.run(d.create("fall", "work"))
    assert job.status == "done" and [c["account"] for c in FakeAdapter.calls] == ["max", "pro"]
    assert FakeAdapter.calls[1]["env"]["CLAUDE_CONFIG_DIR"] == "/tmp/pro" and clock.slept == []
    assert "max limited until" in __import__("swarm.limits", fromlist=["status_line"]).status_line(d.limits)


def test_a_model_limit_falls_back_to_the_next_model_on_the_same_account(dcfg, tmp_path):
    d, clock = make(dcfg, [limited("Opus weekly limit reached ∙ resets Oct 14, 9am"), ok_writes(trailer=False)],
                    tmp_path)
    job = d.run(d.create("model", "work"))
    assert job.status == "done"
    assert [(c["account"], c["model"]) for c in FakeAdapter.calls] == [("max", "opus"), ("max", "sonnet")]
    assert d.limits.until("max", "opus") is not None and d.limits.until("max", "sonnet") is None


def test_push_main_rebases_onto_a_moved_main_and_pushes(dcfg, tmp_path):
    repo = dcfg.repo_root
    d, _ = make(dcfg, [], tmp_path)

    def agent(spec):
        (repo / "other.txt").write_text("main moved\n")                 # someone pushes to main meanwhile
        _git(repo, "add", "other.txt")
        _git(repo, "commit", "-qm", "main moves")
        _git(repo, "push", "-q", "origin", "main")
        return ok_writes("feature.txt", "feature")(spec)
    d.adapter_factory = FakeAdapter([agent]).factory
    job = d.run(d.create("tomain", "work", push="main"))
    assert job.status == "done" and job.pushed_to == "origin/main", job.error
    files = _git(repo, "ls-tree", "-r", "--name-only", "origin/main")
    assert "feature.txt" in files and "other.txt" in files
    assert "Claude-Session" not in _git(repo, "log", "-3", "--format=%B", "origin/main")
    assert job.commits and "feature" in job.commits[0]


def test_push_main_with_a_red_verify_pushes_the_branch_and_fails(dcfg, tmp_path):
    repo = dcfg.repo_root
    before = _git(repo, "rev-parse", "origin/main").strip()

    def agent(spec):
        (Path(spec.cwd) / "scripts" / "verify_fast.sh").write_text("#!/bin/sh\necho 'E999 broken'\nexit 1\n")
        return ok_writes("x.txt", "break verify", trailer=False)(spec)
    d, _ = make(dcfg, [agent], tmp_path)
    job = d.run(d.create("red", "work", push="main"))
    assert job.status == "failed" and job.pushed_to == "origin/job/red" and "verify" in job.error
    _git(repo, "fetch", "-q", "origin")
    assert _git(repo, "rev-parse", "origin/main").strip() == before


def test_names_are_validated_and_jobs_are_listed(dcfg, tmp_path):
    d, _ = make(dcfg, [ok_writes(trailer=False)], tmp_path)
    with pytest.raises(ValueError):
        d.create("bad name; rm -rf /", "x")
    with pytest.raises(ValueError, match="unknown account"):
        d.create("ok", "x", account="nobody")
    d.run(d.create("listed", "x"))
    with pytest.raises(ValueError, match="already exists"):
        d.create("listed", "again")
    jobs = d.list_jobs()
    assert [j.name for j in jobs] == ["listed"] and jobs[0].live_status() == "done"
    d.create("listed", "again", replace=True)


def test_cli_dispatch_foreground_and_jobs(dcfg, tmp_path, monkeypatch):
    from typer.testing import CliRunner
    import swarm.dispatch as dispatch
    from swarm.cli import app
    behaviours = [ok_writes(trailer=False)]
    monkeypatch.setattr(dispatch, "get_adapter", FakeAdapter(behaviours).factory)
    pf = tmp_path / "p.md"
    pf.write_text("Use `rg` to find $HOME things")
    cfgp = str(dcfg.path)
    r = CliRunner().invoke(app, ["--config", cfgp, "dispatch", "--name", "cli-job", "--prompt-file", str(pf),
                                 "--foreground"])
    assert r.exit_code == 0, r.output
    assert FakeAdapter.calls[-1]["prompt"].startswith("Use `rg` to find $HOME things")
    r = CliRunner().invoke(app, ["--config", cfgp, "jobs"])
    assert r.exit_code == 0 and "cli-job" in r.output and "done" in r.output
    r = CliRunner().invoke(app, ["--config", cfgp, "jobs", "cli-job"])
    assert "did the thing" in r.output
    # a prompt on stdin works too
    behaviours.append(ok_writes("y.txt", trailer=False))
    r = CliRunner().invoke(app, ["--config", cfgp, "dispatch", "--name", "stdin-job", "--prompt-file", "-",
                                 "--foreground"], input="from `stdin`")
    assert r.exit_code == 0, r.output
    assert FakeAdapter.calls[-1]["prompt"].startswith("from `stdin`")


def test_background_supervisor_runs_the_job_detached(dcfg, tmp_path, monkeypatch):
    import time
    import swarm.dispatch as dispatch
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    (fakebin / "claude").write_text("#!/bin/sh\ncat > /dev/null\necho done > bg.txt\n"
                                    "echo '{\"type\":\"result\",\"subtype\":\"success\",\"is_error\":false,"
                                    "\"result\":\"bg report\"}'\n")
    (fakebin / "claude").chmod(0o755)
    cfg_raw = yaml.safe_load(dcfg.path.read_text())
    cfg_raw["accounts"]["max"]["env"] = {"PATH": f"{fakebin}:{os.environ['PATH']}"}
    dcfg.path.write_text(yaml.safe_dump(cfg_raw))
    cfg = load_config(dcfg.path)
    root = tmp_path / "jobs"
    # the detached supervisor is a new process: it must find the same jobs dir, so use the real default under HOME
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(dispatch, "jobs_root", lambda project: root)
    d = Dispatcher(cfg, Workspace(cfg.repo_root, cfg.worktree_root), log=lambda m: None, root=root)
    job = d.create("bg", "background work")
    d.start_background(job)
    for _ in range(150):
        if d.load("bg").status in ("done", "failed"):
            break
        time.sleep(0.2)
    job = d.load("bg")
    assert job.status == "done", (root / "bg" / "supervisor.log").read_text()
    assert job.report == "bg report" and job.pushed_to == "origin/job/bg"
