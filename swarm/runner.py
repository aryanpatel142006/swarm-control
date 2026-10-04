"""Worker loop: claim → worktree → prompt → run CLI → verify → push/PR → publish. One process per laptop."""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Callable

from .adapters import get_adapter
from .adapters.base import RunSpec
from .board.base import Board, claim_task
from .config import Config
from .models import TIERS, AgentRow, Question, Report, RunResult, Status, Task, utcnow
from .policy import in_scope, needs_review
from .prompt import compile_prompt, load_rules
from .tools import ensure_plugins, installed_plugins, plugin_dirs, plugin_settings
from .report import (REPORT_SCHEMA, debts_markdown, decisions_markdown, harness_feedback_question, parse_report,
                     report_to_markdown)
from .usage import Ledger
from .workspace import Workspace

STRUCTURED_PROVIDERS = {"claude", "codex"}
ALWAYS_REVIEWED_DOCS = ("docs/CONTRACTS.md", "docs/DESIGN.md")
HARNESS_PATHS = ("docs/decisions/", "docs/debt/")   # written by the runner itself, never by the model
RATE_LIMIT_COOLDOWN_MIN = 15
IDLE_AFTER_S = 300
TRANSIENT_FLAGS = ("resume", "report_missing", "out_of_scope", "docs_touched", "timeout")
PUBLISH_FIELDS = ["status", "attempts", "flags", "pr_url", "claim_nonce", "feedback", "last_error", "review_rounds", "model", "effort"]


def next_tier_model(agent_cfg, current: str) -> tuple[str, str | None]:
    """The agent's model (and effort) one tier above `current`; the best tier stays where it is."""
    tiers = [t for t in reversed(TIERS) if agent_cfg.models.get(t)]   # low → mid → high → best
    idx = next((i for i, t in enumerate(tiers) if agent_cfg.models[t] == current), None)
    if idx is None:
        return current, agent_cfg.effort.get("mid")
    for t in tiers[idx + 1:]:
        if agent_cfg.models[t] != current:
            return agent_cfg.models[t], agent_cfg.effort.get(t)
    return current, agent_cfg.effort.get(tiers[idx])


def _append_log(path: Path, section: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text() if path.exists() else ""
    path.write_text((existing.rstrip() + "\n\n" if existing.strip() else "") + section)


class _Done:
    def __init__(self, value):
        self._v = value

    def result(self):
        return self._v


class SyncExecutor:
    """Runs submitted callables immediately; used in tests and --once."""

    def submit(self, fn, *args, **kwargs):
        return _Done(fn(*args, **kwargs))

    def shutdown(self, wait: bool = True) -> None:
        pass


@dataclass
class Outcome:
    task: Task
    report: Report | None
    result: RunResult | None
    verify_ok: bool | None
    status: Status


class Runner:
    def __init__(self, cfg: Config, board: Board, host: str, ws: Workspace, *, ledger: Ledger,
                 adapter_factory=get_adapter, sleep: Callable[[float], None] = time.sleep, now=utcnow,
                 log=print, executor=None, rules_text: str | None = None, log_dir: Path | None = None):
        if host not in cfg.hosts:
            raise ValueError(f"host '{host}' is not in config.hosts")
        self.cfg, self.board, self.host, self.ws, self.ledger = cfg, board, host, ws, ledger
        self.adapter_factory, self.sleep, self.now, self.log = adapter_factory, sleep, now, log
        self.agents = {a.name: a for a in cfg.agents_on_host(host)}
        self.rules_text = rules_text if rules_text is not None else load_rules()
        self.ensure_plugins = lambda names, enable=True: ensure_plugins(names, log=self.log, enable=enable)
        self.installed_plugins = installed_plugins
        self._plugins_cache: dict | None = None      # `claude plugin list --json` once per process
        self._plugins_ensured: set[str] = set()
        self.claim_check_s = 30                       # how often a running task re-reads its claim from the board
        self.log_dir = Path(log_dir) if log_dir else Path.home() / ".swarm" / cfg.project / "runs"
        self.lock = threading.Lock()
        self.active: dict[str, set[str]] = {name: set() for name in self.agents}
        workers = max(1, sum(a.parallel for a in self.agents.values()))
        self.executor = executor or ThreadPoolExecutor(max_workers=workers)
        self._last_heartbeat = None
        self._idle_since = None
        self._stopping = False

    def stop(self) -> None:
        """Ask in-flight runs to requeue their task instead of publishing (Ctrl-C / kill path)."""
        self._stopping = True

    def install_signal_handlers(self, signal_fn=None) -> None:
        """SIGINT and SIGTERM park in-flight work. Explicit handlers also work when SIGINT was inherited as ignored."""
        import signal
        signal_fn = signal_fn or signal.signal

        def handler(signum, frame):
            self.log(f"signal {signum}: stopping after in-flight runs park their work")
            self.stop()

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal_fn(sig, handler)
            except (ValueError, OSError):  # not the main thread, or unsupported platform
                pass

    # ----- capacity -----
    def free_slots(self, agent_name: str) -> int:
        a = self.agents[agent_name]
        host = self.cfg.hosts[self.host]
        with self.lock:
            used_agent = len(self.active[agent_name])
            used_provider = sum(len(self.active[n]) for n, c in self.agents.items() if c.provider == a.provider)
        cap_provider = host.max_parallel.get(a.provider, 1)
        return max(0, min(a.parallel - used_agent, cap_provider - used_provider))

    # ----- heartbeat -----
    def heartbeat(self, force: bool = False) -> None:
        now = self.now()
        if (not force and self._last_heartbeat
                and (now - self._last_heartbeat).total_seconds() < self.cfg.heartbeat_seconds):
            return
        self._last_heartbeat = now
        for name, a in self.agents.items():
            row = self.board.get_agent(name) or AgentRow(name=name)
            row.provider, row.host, row.last_heartbeat = a.provider, self.host, now
            with self.lock:
                current = sorted(self.active[name])
            if row.cooldown_until and row.cooldown_until > now:
                row.status = "cooldown"
            else:
                row.status = "running" if current else "idle"
                row.cooldown_until = None
            row.current_task = ", ".join(current)
            row.cost_5h_usd = self.ledger.window(name, 5, now).cost_usd or 0.0
            self.board.upsert_agent(row)

    # ----- recovery -----
    def _requeue(self, task: Task, why: str) -> None:
        task.status, task.claim_nonce = Status.READY, ""
        task.flags = list(dict.fromkeys(task.flags + ["resume"]))
        task.last_error = why[:1900]
        self.board.update_task(task, ["status", "claim_nonce", "flags", "last_error"])

    def recover_orphans(self) -> int:
        """On start (and after Ctrl-C): tasks still Running for my agents belong to a process that is gone."""
        n = 0
        with self.lock:
            active = {tid for ids in self.active.values() for tid in ids}
        for t in self.board.list_tasks(status=[Status.RUNNING], agent=list(self.agents)):
            if t.id in active:
                continue
            self._requeue(t, "runner restarted; requeued to resume on its branch")
            self.log(f"[{t.id}] recovered orphaned run → Ready")
            n += 1
        return n

    # ----- polling -----
    def pending_tasks(self) -> list[Task]:
        tasks = self.board.list_tasks(status=[Status.READY, Status.CHANGES_REQUESTED], agent=list(self.agents))
        return [t for t in tasks if t.agent in self.agents]

    def tick(self) -> int:
        self.heartbeat()
        dispatched = 0
        now = self.now()
        rows = {a.name: a for a in self.board.list_agents()}
        for task in self.pending_tasks():
            row = rows.get(task.agent)
            if row and row.cooldown_until and row.cooldown_until > now:
                continue  # a rate-limited provider cannot run anything, whatever the importance
            if self.free_slots(task.agent) <= 0:
                continue
            with self.lock:
                if task.id in self.active[task.agent]:
                    continue
                self.active[task.agent].add(task.id)
            self.executor.submit(self._guarded_run, task)
            dispatched += 1
        return dispatched

    def _guarded_run(self, task: Task) -> None:
        try:
            self.run_task(task)
        except Exception as e:  # noqa: BLE001 - a worker crash must never kill the loop
            self.log(f"[{task.id}] runner crashed: {e!r}")
            try:
                fresh = self.board.get_task(task.id)
                if fresh and fresh.status is Status.RUNNING and fresh.claim_nonce == task.claim_nonce:
                    if self._stopping:
                        self._requeue(fresh, "runner stopped mid-task")
                    else:
                        fresh.status, fresh.claim_nonce = Status.FAILED, ""
                        fresh.last_error = f"runner crash: {e!r}"[:1900]
                        fresh.attempts += 1
                        self.board.update_task(fresh, ["status", "last_error", "claim_nonce", "attempts"])
            except Exception as e2:  # noqa: BLE001
                self.log(f"[{task.id}] could not record crash: {e2!r}")
        finally:
            with self.lock:
                self.active.get(task.agent, set()).discard(task.id)

    def loop(self, *, once: bool = False, stop: Callable[[], bool] = lambda: False) -> None:
        self.log(f"swarm run · host={self.host} · agents={', '.join(self.agents)}")
        self.install_signal_handlers()
        self.recover_orphans()
        failures = 0
        try:
            while not stop() and not self._stopping:
                try:
                    n = self.tick()
                    failures = 0
                except KeyboardInterrupt:
                    raise
                except Exception as e:   # board or network outage: keep the agent alive, back off, retry
                    failures += 1
                    wait = min(300, 15 * 2 ** (failures - 1))
                    self.log(f"tick failed ({type(e).__name__}: {str(e)[:160]}); retrying in {wait}s")
                    if once:
                        break
                    self.sleep(wait)
                    continue
                if once:
                    break
                now = self.now()
                if n == 0:
                    self._idle_since = self._idle_since or now
                    idle_for = (now - self._idle_since).total_seconds()
                    self.sleep(self.cfg.idle_poll_seconds if idle_for > IDLE_AFTER_S else self.cfg.poll_seconds)
                else:
                    self._idle_since = None
                    self.sleep(self.cfg.poll_seconds)
        except KeyboardInterrupt:
            self.log("stopping: waiting for in-flight runs to park their work (Ctrl-C again to abandon)")
            self.stop()
        self.executor.shutdown(wait=True)
        if self._stopping:
            self.recover_orphans()

    # ----- one task -----
    def _claim_watch(self, task: Task):
        """A callable the adapter polls: True once the board no longer shows this run's claim, so a run that was
        reaped and handed to another agent stops burning tokens (Roomcast T-008 ran twice for that reason)."""
        nonce = task.claim_nonce
        state = {"last": 0.0, "lost": False}

        def lost() -> bool:
            if state["lost"]:
                return True
            if time.monotonic() - state["last"] < self.claim_check_s:
                return False
            state["last"] = time.monotonic()
            try:
                fresh = self.board.get_task(task.id)
            except Exception:
                return False   # a board hiccup must not kill a good run
            state["lost"] = fresh is None or fresh.status is not Status.RUNNING or fresh.claim_nonce != nonce
            if state["lost"]:
                self.log(f"[{task.id}] claim lost while running; stopping the CLI")
            return state["lost"]
        return lost

    def _setup_plugins(self, task: Task) -> tuple[list[str], dict | None]:
        """Required plugins: installed + enabled. Task-type plugins: installed, loaded for this run only.
        Every other plugin enabled on this laptop is switched off for the run (context stays lean)."""
        required = list(self.cfg.plugins_required)
        by_type = [p for p in self.cfg.plugins_by_type.get(task.type, []) if p not in required]
        new_required = [p for p in required if p not in self._plugins_ensured]
        new_by_type = [p for p in by_type if p not in self._plugins_ensured]
        unavailable = []
        if new_required:
            unavailable += self.ensure_plugins(new_required, True)
        if new_by_type:
            unavailable += self.ensure_plugins(new_by_type, False)
        if new_required or new_by_type:
            self._plugins_ensured.update(new_required + new_by_type)
            self._plugins_cache = None   # an install may have changed the listing
        if unavailable:
            self.log(f"[{task.id}] plugins unavailable on this host: {', '.join(unavailable)}")
        if self._plugins_cache is None:
            self._plugins_cache = self.installed_plugins()
        installed = self._plugins_cache
        dirs = plugin_dirs(by_type, installed) if by_type else []
        if dirs:
            self.log(f"[{task.id}] loading task plugins: {', '.join(Path(d).parent.name for d in dirs)}")
        return dirs, plugin_settings(installed, required + by_type)

    def run_task(self, task: Task) -> Outcome:
        agent_cfg = self.agents[task.agent]
        prev_status = task.status
        if not claim_task(self.board, task, task.agent, sleep=self.sleep):
            self.log(f"[{task.id}] claim lost")
            return Outcome(task, None, None, None, task.status)
        attempt = task.attempts + 1
        reuse = prev_status is Status.CHANGES_REQUESTED or "resume" in task.flags
        self.log(f"[{task.id}] {task.agent} attempt {attempt} model={task.model} reuse={reuse}")
        started = self.now()
        wt = self.ws.provision(task.id, reuse_branch=reuse)
        try:
            if reuse:   # a parked branch drifts from main; start the resume from current main, or say why not
                ok, conflicts = self.ws.rebase_onto_main(wt)
                if not ok:
                    note = ("Rebase onto main conflicted in: " + ", ".join(conflicts)
                            + f". First run `git fetch {self.ws.remote} && git rebase {self.ws.remote}/{self.cfg.main_branch}`, "
                            "resolve every conflict, `git rebase --continue`, then do the task.")
                    task.feedback = (task.feedback.rstrip() + "\n\n" + note).strip()
                    self.log(f"[{task.id}] resume: rebase conflicted ({', '.join(conflicts)})")
            self.ws.run_script(wt, self.cfg.verify.setup_worktree, 600)
            deps = {}
            for d in task.depends_on:
                dep = self.board.get_task(d)
                deps[d] = dep.title if dep else ""
            structured = agent_cfg.provider in STRUCTURED_PROVIDERS
            mcp = self.cfg.mcp_for(task.type, agent_cfg, task.importance)
            skills = self.cfg.skills_for(task.type, task.importance)
            dirs: list[str] = []
            settings = None
            if agent_cfg.provider == "claude":
                dirs, settings = self._setup_plugins(task)
            inline = {n: self.cfg.mcp_servers[n] for n in mcp if n in self.cfg.mcp_servers}
            prompt = compile_prompt(task, self.cfg, rules_text=self.rules_text, deps_summaries=deps,
                                    structured_output_supported=structured, mcp=mcp, skills=skills)
            pf = wt / ".swarm-run" / "prompt.md"
            pf.write_text(prompt)
            limit = self.cfg.limit_for(task.size)
            model = task.model or agent_cfg.models["mid"]
            effort = task.effort or agent_cfg.effort.get("mid")
            spec = RunSpec(prompt_file=pf, model=model, effort=effort, max_turns=limit.turns,
                           budget_usd=limit.budget_usd, timeout_s=limit.minutes * 60, cwd=wt,
                           schema=REPORT_SCHEMA if structured else None, sandbox=agent_cfg.sandbox,
                           extra_args=list(agent_cfg.extra_args), mcp=mcp, plugin_dirs=dirs,
                           mcp_servers=inline, settings=settings, should_stop=self._claim_watch(task),
                           claim_nonce=task.claim_nonce)
            result = self.adapter_factory(agent_cfg).run(spec)
            duration = (self.now() - started).total_seconds()
            self.ledger.append(agent=task.agent, model=model, task_id=task.id, usage=result.usage,
                               duration_s=duration, ok=result.ok)
            self._save_logs(wt, task.id, attempt, result)
            if self._stopping:
                return self._park(task, wt, result)
            if result.rate_limited:
                return self._rate_limited(task, result)
            return self._publish(task, wt, attempt, result)
        finally:
            self.ws.dispose(wt)
            self._bump_agent_row(task.agent, result_usage=None)

    def _bump_agent_row(self, agent: str, result_usage=None) -> None:
        """Refresh runs/spend on the Agents row so the status page shows real numbers after each task."""
        try:
            row = self.board.get_agent(agent) or AgentRow(name=agent, provider=self.agents[agent].provider, host=self.host)
            totals = self.ledger.totals(agent)
            row.runs, row.tokens_in, row.tokens_out = row.runs + 1, totals.input_tokens, totals.output_tokens
            row.cost_usd = totals.cost_usd or 0.0
            row.cost_5h_usd = self.ledger.window(agent, 5, self.now()).cost_usd or 0.0
            self.board.upsert_agent(row)
        except Exception as e:  # noqa: BLE001 - bookkeeping must never fail a task
            self.log(f"could not update agent row: {e!r}")

    def _save_logs(self, wt: Path, task_id: str, attempt: int, result: RunResult) -> None:
        logdir = self.log_dir / task_id / f"attempt-{attempt}"
        try:
            logdir.mkdir(parents=True, exist_ok=True)
            (logdir / "stdout.txt").write_text(result.stdout)
            (logdir / "stderr.txt").write_text(result.stderr)
            prompt = wt / ".swarm-run" / "prompt.md"
            if prompt.exists():
                (logdir / "prompt.md").write_text(prompt.read_text())
        except OSError as e:
            self.log(f"could not save logs: {e}")

    def _park(self, task: Task, wt: Path, result: RunResult) -> Outcome:
        """Runner is stopping: keep whatever the model left, push it, and requeue for a resume."""
        if self.ws.changed_files(wt):
            self.ws.commit_all(wt, f"{task.id}: parked by runner stop")
            self.ws.push(wt, task.branch, force_with_lease=True)
        self._requeue(task, "runner stopped mid-task; work parked on the branch")
        self.log(f"[{task.id}] parked → Ready (resume)")
        return Outcome(task, None, result, None, Status.READY)

    def _rate_limited(self, task: Task, result: RunResult) -> Outcome:
        now = self.now()
        task.status, task.claim_nonce = Status.READY, ""
        task.last_error = f"rate limited: {result.error[:300]}"
        self.board.update_task(task, ["status", "claim_nonce", "last_error"])
        row = self.board.get_agent(task.agent) or AgentRow(
            name=task.agent, provider=self.agents[task.agent].provider, host=self.host)
        row.status = "cooldown"
        row.cooldown_until = result.reset_at or (now + timedelta(minutes=RATE_LIMIT_COOLDOWN_MIN))
        row.note = f"rate limited at {now.isoformat(timespec='minutes')}"
        self.board.upsert_agent(row)
        self.log(f"[{task.id}] rate limited; {task.agent} cooling down until {row.cooldown_until}")
        return Outcome(task, None, result, None, Status.READY)

    def _publish(self, task: Task, wt: Path, attempt: int, result: RunResult) -> Outcome:
        fresh = self.board.get_task(task.id)
        if fresh is None or fresh.claim_nonce != task.claim_nonce or fresh.status is not Status.RUNNING:
            # we were reaped (laptop slept) and someone else owns the task now; never overwrite their state
            self.log(f"[{task.id}] claim no longer ours; abandoning publish")
            return Outcome(task, None, result, None, fresh.status if fresh else Status.READY)

        changed = self.ws.changed_files(wt)
        report = parse_report(result.structured_output, wt, changed_files=changed, error=result.error)
        flags = [f for f in task.flags if f not in TRANSIENT_FLAGS]
        if report.synthesized:
            flags.append("report_missing")
        if any(not in_scope(f, task.scope) for f in changed if not f.startswith(HARNESS_PATHS)):
            flags.append("out_of_scope")
        if any(f in ALWAYS_REVIEWED_DOCS for f in changed):
            flags.append("docs_touched")
        if result.timed_out:
            flags.append("timeout")
        task.flags, task.attempts, task.claim_nonce = flags, attempt, ""

        # decision / debt logs live on the branch, one file per task, so parallel tasks never conflict
        dec, debt = decisions_markdown(task, report), debts_markdown(task, report)
        for sub, text in (("decisions", dec), ("debt", debt)):
            if text:
                _append_log(wt / "docs" / sub / f"{task.id}.md", text)   # every attempt adds a section
        if dec or debt:
            changed = self.ws.changed_files(wt)

        verify = self.ws.run_script(wt, self.cfg.verify.fast, 900) if changed else None
        verify_ok = verify.ok if verify is not None else None
        verify_tail = verify.tail(1500) if verify is not None else ""

        pr_url, push_error = task.pr_url, ""
        if changed:
            self.ws.commit_all(wt, f"{task.id}: {(report.summary or 'work in progress')[:60]}")
            # only the claim holder pushes this branch, so a lease against the fetched remote ref is safe
            push = self.ws.push(wt, task.branch, force_with_lease=True)
            if push.ok:
                body = report_to_markdown(report, attempt=attempt, verify_ok=verify_ok, verify_tail=verify_tail,
                                          pr_url="", flags=flags)
                try:
                    pr_url = self.ws.pr_create_or_update(task.branch, task.title_with_id(), body) or pr_url
                except RuntimeError as e:
                    self.log(f"[{task.id}] PR failed: {e}")
            else:
                push_error = push.err.strip()[:600] or f"exit {push.code}"
                self.log(f"[{task.id}] push failed: {push_error[:200]}")
        task.pr_url = pr_url

        md = report_to_markdown(report, attempt=attempt, verify_ok=verify_ok, verify_tail=verify_tail,
                                pr_url=pr_url, flags=flags)
        self.board.append_task_report(task, f"Report — attempt {attempt}", md)
        if report.status == "blocked" and not report.question:
            report.question = {"kind": "blocking", "options": [], "proceeding_with": "",
                               "text": (report.summary or f"{task.id} reported blocked without saying why")[:190]}
        if report.question:
            self._file_question(task, report, report.question)
        hf = harness_feedback_question(task, report)
        if hf:
            self._file_question(task, report, hf, context=hf.pop("context"))

        status = self._decide(task, report, result, changed, verify_ok, verify_tail, push_error)
        task.status = status
        self.board.update_task(task, PUBLISH_FIELDS)
        self.log(f"[{task.id}] → {status.value}")
        return Outcome(task, report, result, verify_ok, status)

    def _decide(self, task: Task, report: Report, result: RunResult, changed: list[str],
                verify_ok: bool | None, verify_tail: str, push_error: str) -> Status:
        if report.status == "blocked":
            return Status.BLOCKED
        if report.status == "failed":
            task.last_error = (report.summary or "model reported failure")[:1900]
            return Status.FAILED
        if not changed:
            task.last_error = "timeout" if result.timed_out else ("no changes" if result.ok else result.error[:1900])
            return Status.FAILED
        if push_error:
            task.last_error = f"push failed: {push_error}"[:1900]
            return Status.FAILED
        if not result.ok and report.synthesized:
            # the CLI ended abnormally (max turns, timeout, crash) but left work behind: continue on the branch
            if task.attempts >= self.cfg.max_attempts:
                task.last_error = result.error[:1900] or "abnormal end"
                return Status.FAILED
            task.feedback = (f"The previous attempt ended with: {result.error or 'unknown error'}. "
                             "Continue from the current branch state, finish the task, and produce the report.")
            task.flags = list(dict.fromkeys(task.flags + ["resume"]))
            # the same model at the same budget usually ends the same way: resume one tier up
            agent_cfg = self.cfg.agents.get(task.agent)
            if agent_cfg:
                task.model, task.effort = next_tier_model(agent_cfg, task.model)
            return Status.CHANGES_REQUESTED
        if verify_ok is False:
            task.review_rounds += 1
            if task.review_rounds > self.cfg.max_review_rounds:
                self._file_question(task, report, {
                    "kind": "blocking",
                    "text": f"{task.id} keeps failing verify after {task.review_rounds} rounds. Cut, split, or fix by hand?",
                    "options": ["cut", "split", "human fix"], "proceeding_with": ""}, context=verify_tail)
                return Status.BLOCKED
            task.feedback = "scripts/verify_fast.sh failed. Fix it:\n" + verify_tail[-1800:]
            task.flags = list(dict.fromkeys(task.flags + ["resume"]))
            return Status.CHANGES_REQUESTED
        task.feedback = ""
        return Status.REVIEW if needs_review(task, self.cfg) else Status.MERGE_READY

    def _file_question(self, task: Task, report: Report, q: dict, *, context: str = "") -> Question:
        question = Question(
            id="", text=q["text"][:190], kind=q.get("kind", "blocking"),
            context=(context or report.summary)[:1900], options=list(q.get("options") or []),
            proceeding_with=q.get("proceeding_with", ""),
            impact="high" if q.get("kind") == "blocking" else "medium",
            task_id=task.id, asked_by=task.agent or "", status="Open",
        )
        return self.board.create_question(question)
