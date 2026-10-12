"""`swarm dispatch`: one headless agent job outside the board, done safely.

What it replaces (HackRU, Oct 10-12 2026): when the Max account hit its limit, the orchestrator ran extra
`claude -p` jobs on the second account (CLAUDE_CONFIG_DIR=~/.claude-pro) through an ad-hoc script that passed the
prompt on the command line (`nohup claude -p "$*"`), so backticks and `$(...)` in a prompt were executed by the
shell. All those jobs worked in the one main checkout: they swept each other's staged files into their commits,
collided on `git pull --rebase` (rebase markers, refused pulls) and stashed each other's work. When an account hit
"You've hit your session limit · resets 3:45pm" the job died and was restarted by hand after the reset.

A dispatch job instead:
- reads its prompt from a file or stdin and hands it to the CLI on stdin (never through a shell or argv);
- runs in its own worktree on branch `job/<name>` made from origin/main (the commit-msg hook strips AI trailers);
- logs every attempt to ~/.swarm/<project>/jobs/<name>/ (prompt.md, attempt-N.out/.err, report.md, job.json);
- on an account or model limit, records it (swarm/limits.py) and continues on a fallback account or model, or waits
  for the reset time named in the message and retries (`dispatch.max_limit_waits` times at most);
- when the agent is done: commits what it left, strips AI trailers, and either pushes `job/<name>` (default), or
  rebases onto current origin/main, runs the fast verify and pushes to main (`--push main`; a conflict or a red
  verify pushes the branch instead and fails the job), or leaves the worktree alone (`--push none`).
`swarm jobs` lists running and finished jobs with their final reports.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from .adapters import get_adapter
from .adapters.base import RunSpec, last_json_object
from .config import AgentConfig, Config
from . import limits as limits_mod
from .limits import LimitStore, fallback_chain, limited_model
from .models import TIERS, RunResult

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
LIMIT_MARGIN_S = 90            # wait this long past a named reset before retrying
FINAL = ("done", "failed", "cancelled")


def jobs_root(project: str) -> Path:
    return Path.home() / ".swarm" / project / "jobs"


def _iso(t: datetime | None) -> str:
    return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if t else ""


def _parse(v) -> datetime | None:
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).astimezone(timezone.utc) if v else None
    except (TypeError, ValueError):
        return None


def pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


@dataclass
class Job:
    name: str
    account: str
    model: str
    effort: str | None = None
    push: str = "branch"            # branch | main | none
    verify: bool = True
    cwd: str = ""                   # run here instead of a worktree (no commits, no push)
    status: str = "queued"          # queued, running, waiting_limit, finishing, done, failed, cancelled
    created: str = ""
    started: str = ""
    finished: str = ""
    worktree: str = ""
    branch: str = ""
    supervisor_pid: int = 0
    cli_pid: int = 0
    retry_at: str = ""
    limit_waits: int = 0
    attempts: list[dict] = field(default_factory=list)
    commits: list[str] = field(default_factory=list)
    pushed_to: str = ""
    error: str = ""
    report: str = ""

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=1)

    @classmethod
    def load(cls, path: Path) -> "Job":
        data = json.loads(Path(path).read_text())
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**known)

    def live_status(self) -> str:
        """The status, or 'died' when the job claims to run but its supervisor is gone."""
        if self.status not in FINAL and self.supervisor_pid and not pid_alive(self.supervisor_pid):
            return "died"
        return self.status


def final_text(result: RunResult, cap: int = 20000) -> str:
    """The agent's final message: Claude's `result` field, Codex's last agent message, else the stdout tail."""
    data = last_json_object(result.stdout or "")
    if isinstance(data, dict):
        for key in ("result", "last_agent_message", "message", "text"):
            if isinstance(data.get(key), str) and data[key].strip():
                return data[key].strip()[:cap]
    for line in reversed((result.stdout or "").splitlines()):
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        item = ev.get("item") if isinstance(ev, dict) else None
        if isinstance(item, dict) and item.get("type") in ("agent_message", "assistant_message") and item.get("text"):
            return str(item["text"]).strip()[:cap]
    return (result.stdout or result.stderr or "").strip()[-cap:]


JOB_RULES = """

---
## Job rules (swarm dispatch; these override anything above that conflicts)
- You are job `{name}` working in `{cwd}`{branch_line}. Other agents work at the same time in other checkouts:
  never `cd` into the main checkout or another worktree, never `git stash`, and never run `git pull`, `git fetch`,
  `git rebase`, `git merge` or `git push`. The harness rebases and pushes when you finish.
- Commit as you go: `git add -A && git commit -m "<what>"`. No `Co-Authored-By:`, `Claude-Session:` or
  "Generated with" lines in commit messages (a hook strips them anyway).
{outputs_line}- End with a short final report: what you did, the files you changed, the commands you ran with their results,
  and anything left undone.
"""


class Dispatcher:
    def __init__(self, cfg: Config, ws, *, adapter_factory: Callable = get_adapter, limits: LimitStore | None = None,
                 now: Callable[[], datetime] | None = None, sleep: Callable[[float], None] = time.sleep,
                 log: Callable[[str], None] = print, root: Path | None = None):
        self.cfg, self.ws = cfg, ws
        self.adapter_factory = adapter_factory
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.limits = limits or LimitStore(limits_mod.default_limits_path(cfg.project), now=self.now)
        self.sleep, self.log = sleep, log
        self.root = Path(root) if root else jobs_root(cfg.project)
        self._stop = False
        self._cli_pid = 0

    # ----- job files -----
    def job_dir(self, name: str) -> Path:
        return self.root / name

    def save(self, job: Job) -> None:
        d = self.job_dir(job.name)
        d.mkdir(parents=True, exist_ok=True)
        tmp = d / f".job.json.{os.getpid()}"
        tmp.write_text(job.to_json())
        tmp.replace(d / "job.json")

    def load(self, name: str) -> Job:
        return Job.load(self.job_dir(name) / "job.json")

    def list_jobs(self) -> list[Job]:
        out = []
        if not self.root.exists():
            return out
        for p in sorted(self.root.glob("*/job.json")):
            try:
                out.append(Job.load(p))
            except (OSError, ValueError, TypeError):
                continue
        return sorted(out, key=lambda j: j.created, reverse=True)

    def create(self, name: str, prompt: str, *, account: str | None = None, model: str | None = None,
               effort: str | None = None, push: str | None = None, verify: bool | None = None,
               cwd: str | None = None, replace: bool = False) -> Job:
        if not NAME_RE.match(name or ""):
            raise ValueError(f"job name {name!r}: letters, digits, '.', '_' and '-' only (max 64)")
        if not (prompt or "").strip():
            raise ValueError("the prompt is empty")
        account = account or self.cfg.dispatch.account or next(iter(self.cfg.accounts), "")
        if not account or self.cfg.account(account) is None:
            known = ", ".join(list(self.cfg.accounts) + list(self.cfg.agents))
            raise ValueError(f"unknown account {account!r}; use one of: {known}")
        d = self.job_dir(name)
        if (d / "job.json").exists():
            old = self.load(name)
            if old.live_status() not in FINAL + ("died",) or not replace:
                raise ValueError(f"job {name} already exists ({old.live_status()}); pick another name"
                                 + ("" if old.live_status() not in FINAL + ("died",) else " or pass --replace"))
            shutil.rmtree(d)
        acct = self.cfg.account(account)
        push = push or self.cfg.dispatch.push
        if push not in ("branch", "main", "none"):
            raise ValueError("push must be branch, main or none")
        job = Job(name=name, account=account, model=model or acct.model or self.cfg.dispatch.model,
                  effort=effort or self.cfg.dispatch.effort, push="none" if cwd else push,
                  verify=self.cfg.dispatch.verify if verify is None else verify, cwd=cwd or "",
                  created=_iso(self.now()), branch="" if cwd else f"job/{name}")
        d.mkdir(parents=True, exist_ok=True)
        (d / "prompt.md").write_text(prompt)
        self.save(job)
        return job

    def start_background(self, job: Job, *, python: str | None = None, host: str | None = None) -> int:
        """Run the job's supervisor detached (own session, survives the terminal), under caffeinate on macOS."""
        d = self.job_dir(job.name)
        argv = [python or sys.executable, "-m", "swarm.dispatch", "run", str(d), "--config", str(self.cfg.path)]
        if host:
            argv += ["--host", host]
        if shutil.which("caffeinate"):
            argv = ["caffeinate", "-i", *argv]
        with open(d / "supervisor.log", "ab") as logf:
            p = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=logf, stderr=subprocess.STDOUT,
                                 start_new_session=True, cwd=str(self.cfg.repo_root))
        return p.pid

    def stop(self, name: str) -> bool:
        job = self.load(name)
        if job.live_status() in FINAL + ("died",):
            return False
        pid = job.supervisor_pid
        try:
            os.kill(pid, signal.SIGTERM)
            return True
        except OSError:
            return False

    # ----- the supervisor -----
    def _agent_cfg(self, account: str, model: str) -> AgentConfig:
        acct = self.cfg.account(account)
        agent = self.cfg.agents.get(account)
        if agent is not None and agent.provider == acct.provider:
            base = agent
            return AgentConfig(name=account, provider=base.provider, host=base.host, models={t: model for t in TIERS},
                               effort=dict(base.effort), sandbox=base.sandbox, git_writable=base.git_writable,
                               cli=base.cli, args_template=base.args_template, approve_args=base.approve_args,
                               extra_args=list(base.extra_args), env={**base.env, **acct.env},
                               command_template=base.command_template)
        return AgentConfig(name=account, provider=acct.provider, host="", models={t: model for t in TIERS},
                           env=dict(acct.env))

    def candidates(self, job: Job) -> list[tuple[str, str]]:
        """(account, model) pairs in order of preference: the job's model on its account, then on the fallback
        accounts (quality first), then each fallback model the same way."""
        acct = self.cfg.account(job.account)
        accounts = [job.account] + [a for a in (acct.fallback if acct else []) if a != job.account]
        return [(a, m) for m in fallback_chain(job.model, self.cfg.model_fallbacks) for a in accounts]

    def choose(self, job: Job) -> tuple[tuple[str, str] | None, datetime | None]:
        """The first usable (account, model), or (None, earliest reset)."""
        resets = []
        for a, m in self.candidates(job):
            until = self.limits.until(a, m)
            if until is None:
                return (a, m), None
            resets.append(until)
        return None, (min(resets) if resets else None)

    def _provision(self, job: Job) -> Path:
        if job.cwd:
            return Path(job.cwd).expanduser().resolve()
        self.ws.install_hooks()
        self.ws.fetch()
        path = self.ws.worktree_root / "jobs" / job.name
        if path.exists():
            self.ws.dispose(path)
        self.ws.git(self.ws.repo_root, "worktree", "prune")
        path.parent.mkdir(parents=True, exist_ok=True)
        self.ws._worktree_add(job.branch, path, f"{self.ws.remote}/{self.ws.main_branch}")
        (path / ".swarm-run").mkdir(exist_ok=True)
        self.ws._ensure_excluded(".swarm-run/")
        if self.cfg.worktree_links:
            try:
                self.ws.link_ignored(path, self.cfg.worktree_links)
            except Exception as e:  # noqa: BLE001
                self.log(f"[{job.name}] could not link shared files: {e!r}")
        setup = self.ws.run_script(path, self.cfg.verify.setup_worktree, 600, slot=False)
        if setup is not None and not setup.ok:
            raise RuntimeError(f"{self.cfg.verify.setup_worktree} failed: {setup.tail(400)}")
        return path

    def _prompt_file(self, job: Job, wt: Path) -> Path:
        base = (self.job_dir(job.name) / "prompt.md").read_text()
        outputs = self.cfg.outputs_path()
        outputs_line = (f"- Files meant for people (exports, audio, PDFs, screenshots) go under "
                        f"`{outputs}/<kind>/` (also `$SWARM_OUTPUTS_DIR`), never ~/Desktop or the home folder.\n"
                        if outputs else "")
        text = base + JOB_RULES.format(name=job.name, cwd=wt, outputs_line=outputs_line,
                                       branch_line=f" on branch `{job.branch}`, made from origin/main for this job "
                                                   "only" if job.branch else "")
        d = (wt / ".swarm-run") if job.branch else self.job_dir(job.name)
        d.mkdir(parents=True, exist_ok=True)
        pf = d / "prompt.compiled.md"
        pf.write_text(text)
        return pf

    def _env(self, job: Job, wt: Path) -> dict:
        env = {"SWARM_JOB": job.name, **self.cfg.project_env()}
        if self.cfg.outputs_path():
            env["SWARM_OUTPUTS_DIR"] = self.cfg.outputs_path()
        if job.branch:
            env["PYTHONPATH"] = str(wt) + (os.pathsep + os.environ["PYTHONPATH"] if os.environ.get("PYTHONPATH") else "")
        return env

    def _on_term(self, signum, frame) -> None:   # noqa: ARG002
        self._stop = True
        if self._cli_pid:
            try:
                os.killpg(self._cli_pid, signal.SIGTERM)
            except OSError:
                pass

    def run(self, job: Job, *, install_signals: bool = False) -> Job:
        if install_signals:
            signal.signal(signal.SIGTERM, self._on_term)
            signal.signal(signal.SIGINT, self._on_term)
            signal.signal(signal.SIGHUP, signal.SIG_IGN)
        d = self.job_dir(job.name)
        job.status, job.started, job.supervisor_pid = "running", _iso(self.now()), os.getpid()
        self.save(job)
        try:
            wt = self._provision(job)
        except Exception as e:  # noqa: BLE001
            return self._finish(job, None, "failed", f"could not make the job's worktree: {e}")
        job.worktree = str(wt)
        self.save(job)
        result: RunResult | None = None
        while not self._stop:
            pick, reset = self.choose(job)
            if pick is None:
                if job.limit_waits >= self.cfg.dispatch.max_limit_waits or reset is None:
                    return self._finish(job, wt, "failed", f"every account/model is limited (waited "
                                                            f"{job.limit_waits} times); last error: {job.error}")
                wake = reset + timedelta(seconds=LIMIT_MARGIN_S)
                job.status, job.retry_at, job.limit_waits = "waiting_limit", _iso(wake), job.limit_waits + 1
                self.save(job)
                self.log(f"[{job.name}] every account is limited; retrying at {_iso(wake)}")
                self._sleep_until(wake)
                continue
            account, model = pick
            n = len(job.attempts) + 1
            job.status, job.retry_at = "running", ""
            att = {"n": n, "account": account, "model": model, "started": _iso(self.now())}
            job.attempts.append(att)
            self.save(job)
            self.log(f"[{job.name}] attempt {n} on {account} / {model}")
            spec = RunSpec(prompt_file=self._prompt_file(job, wt), model=model, effort=job.effort,
                           max_turns=self.cfg.dispatch.turns, budget_usd=None,
                           timeout_s=self.cfg.dispatch.minutes * 60, cwd=wt, env=self._env(job, wt),
                           should_stop=lambda: self._stop, stop_poll_s=2.0)

            def on_start(pid: int) -> None:
                self._cli_pid = job.cli_pid = pid
                self.save(job)
            spec.on_start = on_start
            result = self.adapter_factory(self._agent_cfg(account, model)).run(spec)
            self._cli_pid = job.cli_pid = 0
            (d / f"attempt-{n}.out").write_text(result.stdout or "")
            (d / f"attempt-{n}.err").write_text(result.stderr or "")
            att.update({"finished": _iso(self.now()), "exit": result.exit_code, "error": result.error[:300]})
            if self._stop:
                break
            if result.rate_limited or result.auth_lost:
                text = "\n".join([result.error, (result.stderr or "")[-3000:], (result.stdout or "")[-3000:]])
                fam = limited_model(text) if result.rate_limited else ""
                has_fallback = len(fallback_chain(model, self.cfg.model_fallbacks)) > 1
                reason = ("login lost: " if result.auth_lost else "") + (result.error or "limit")[:160]
                until = self.limits.mark(account, result.reset_at, reason, model=fam if fam and has_fallback else "")
                att["limited_until"] = _iso(until)
                job.error = reason
                self.log(f"[{job.name}] {account}{'/' + fam if fam and has_fallback else ''} limited until "
                         f"{_iso(until)}: {reason}")
                self.save(job)
                continue
            break
        if self._stop:
            return self._finish(job, wt, "cancelled", "stopped by `swarm jobs --stop` or a signal", result)
        ok = bool(result and result.ok)
        return self._finish(job, wt, "done" if ok else "failed", "" if ok else (result.error if result else ""),
                            result)

    def _sleep_until(self, wake: datetime) -> None:
        while not self._stop:
            left = (wake - self.now()).total_seconds()
            if left <= 0:
                return
            self.sleep(min(left, 60.0))

    # ----- after the agent -----
    def _finish(self, job: Job, wt: Path | None, status: str, error: str, result: RunResult | None = None) -> Job:
        d = self.job_dir(job.name)
        job.status = "finishing"
        self.save(job)
        report = final_text(result) if result is not None else ""
        notes: list[str] = []
        if wt is not None and job.branch:
            try:
                notes += self._land(job, wt, status)
                if any(n.startswith("FAILED:") for n in notes):
                    status = "failed"
                    error = error or next(n for n in notes if n.startswith("FAILED:"))[8:]
            except Exception as e:  # noqa: BLE001 - never lose the report over a git problem
                notes.append(f"FAILED: landing the work: {e}")
                status, error = "failed", error or str(e)
        job.status, job.error, job.finished = status, (error or "")[:500], _iso(self.now())
        job.report = report[:4000]
        (d / "report.md").write_text((report or "(no final message)") + ("\n\n## Harness\n" + "\n".join(
            f"- {n}" for n in notes) if notes else "") + "\n")
        self.save(job)
        self.log(f"[{job.name}] {status}" + (f": {error}" if error else "") + (f" · {job.pushed_to}" if job.pushed_to
                                                                               else ""))
        return job

    def _land(self, job: Job, wt: Path, status: str) -> list[str]:
        ws, notes = self.ws, []
        main_ref = f"{ws.remote}/{ws.main_branch}"
        if ws.merge_in_progress(wt) or ws.rebase_in_progress(wt):
            ws.git(wt, "merge", "--abort", check=False)
            ws.git(wt, "rebase", "--abort", check=False)
            notes.append("the agent left a merge/rebase in progress; it was aborted")
        if ws.commit_all(wt, f"{job.name}: work the job left uncommitted"):
            notes.append("committed the files the agent left uncommitted")
        ahead = ws.git(wt, "rev-list", "--count", f"{main_ref}..HEAD", check=False).out.strip()
        if not ahead or ahead == "0":
            notes.append("no commits: nothing to push")
            self._dispose(job, wt)
            return notes
        if ws.scrub_attribution(wt):
            notes.append("removed AI attribution lines from the job's commits")
        if job.push == "none":
            notes.append(f"push none: the work stays in {wt} on {job.branch}")
            job.commits = self._commits(wt, main_ref)
            return notes
        target_main = job.push == "main" and status == "done"
        if target_main:
            for attempt in range(3):
                ws.fetch()
                r = ws.git(wt, "-c", "user.email=swarm@local", "-c", "user.name=swarm", "rebase", main_ref,
                           check=False)
                if not r.ok:
                    files = ws.unmerged_files(wt)
                    ws.git(wt, "rebase", "--abort", check=False)
                    notes.append(f"FAILED: rebase onto {main_ref} conflicts in {', '.join(files) or '?'}; pushed "
                                 f"the branch {job.branch} for a hand merge")
                    break
                if job.verify and self.cfg.verify.fast:
                    v = ws.run_script(wt, self.cfg.verify.fast, 1800)
                    if v is not None and not v.ok:
                        (self.job_dir(job.name) / "verify.log").write_text(v.out + "\n" + v.err)
                        notes.append(f"FAILED: {self.cfg.verify.fast} failed after the rebase (verify.log); pushed "
                                     f"the branch {job.branch} instead of main")
                        break
                env = {"SWARM_SKIP_PREPUSH": "1"} if job.verify else {}   # verified just now
                commits = self._commits(wt, main_ref)
                p = subprocess.run(["git", "-C", str(wt), "push", "-q", ws.remote, f"HEAD:refs/heads/{ws.main_branch}"],
                                   capture_output=True, text=True, env={**os.environ, **env}, timeout=180)
                if p.returncode == 0:
                    job.commits = commits
                    job.pushed_to = f"{ws.remote}/{ws.main_branch}"
                    notes.append(f"rebased onto {main_ref} and pushed to {ws.main_branch}")
                    self._dispose(job, wt)
                    return notes
                notes.append(f"push to {ws.main_branch} refused (attempt {attempt + 1}): "
                             f"{(p.stderr or p.stdout).strip()[:200]}")
            else:
                notes.append(f"FAILED: main kept moving; pushed the branch {job.branch} instead")
        job.commits = self._commits(wt, main_ref)
        r = ws.push(wt, job.branch, force_with_lease=True)
        if r.ok:
            job.pushed_to = f"{ws.remote}/{job.branch}"
            notes.append(f"pushed {job.branch} ({len(job.commits)} commit(s)); open a PR or merge it by hand")
            self._dispose(job, wt)
        else:
            notes.append(f"FAILED: push of {job.branch} failed ({r.tail(200)}); the work stays in {wt}")
        return notes

    def _commits(self, wt: Path, base: str) -> list[str]:
        r = self.ws.git(wt, "log", "--format=%h %s", f"{base}..HEAD", check=False)
        return [ln for ln in r.out.splitlines() if ln.strip()][:50]

    def _dispose(self, job: Job, wt: Path) -> None:
        try:
            self.ws.dispose(wt)
        except Exception as e:  # noqa: BLE001
            self.log(f"[{job.name}] could not remove the worktree {wt}: {e!r}")


def main(argv: list[str] | None = None) -> int:
    """`python -m swarm.dispatch run <job dir> --config <path>`: the detached supervisor of one job."""
    import argparse
    ap = argparse.ArgumentParser(prog="swarm.dispatch")
    ap.add_argument("cmd", choices=["run"])
    ap.add_argument("job_dir")
    ap.add_argument("--config", required=True)
    ap.add_argument("--host", default=None)
    a = ap.parse_args(argv)
    from .cli import _workspace, state
    from .config import load_config
    from .env import load_env_file
    load_env_file()
    state.cfg = load_config(a.config)
    state.host = a.host or os.environ.get("SWARM_HOST")
    cfg = state.cfg
    d = Path(a.job_dir)
    disp = Dispatcher(cfg, _workspace(cfg), root=d.parent)
    job = disp.load(d.name)
    disp.run(job, install_signals=True)
    return 0 if disp.load(d.name).status == "done" else 1


if __name__ == "__main__":
    raise SystemExit(main())
