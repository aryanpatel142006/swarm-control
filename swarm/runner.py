"""Worker loop: claim → worktree → prompt → run CLI → verify → push/PR → publish. One process per laptop."""
from __future__ import annotations

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Callable

from .adapters import get_adapter
from .adapters.base import RunSpec
from .feedback import (DEFAULT_PLACEHOLDER_FILES, DEFAULT_PLACEHOLDER_PATTERNS, fenced_lines, placeholder_feedback,
                       placeholder_hits, reconcile_sync_feedback, verify_feedback)
from .board.base import Board, claim_task
from .config import Config
from .models import QUESTION_TEXT_CAP, TIERS, USAGE_LIMIT_NOTE, AgentRow, Question, Report, RunResult, Status, Task, utcnow
from .policy import in_scope, needs_review
from .prompt import compile_prompt, load_rules
from .tools import ensure_plugins, installed_plugins, plugin_dirs, plugin_settings
from .report import (REPORT_SCHEMA, debts_markdown, decisions_markdown, harness_feedback_question, parse_report,
                     report_to_markdown)
from .usage import Ledger
from .workspace import Workspace, merge_conflict_instructions

STRUCTURED_PROVIDERS = {"claude", "codex"}
ALWAYS_REVIEWED_DOCS = ("docs/CONTRACTS.md", "docs/DESIGN.md")
HARNESS_PATHS = ("docs/decisions/", "docs/debt/")   # written by the runner itself, never by the model
RATE_LIMIT_COOLDOWN_MIN = 15
USAGE_LIMIT_COOLDOWN_H = 3       # an exhausted plan with no reset time in the message (Oct 6 2026, codex-b)
IDLE_AFTER_S = 300
TRANSIENT_FLAGS = ("resume", "report_missing", "out_of_scope", "docs_touched", "timeout")
SELF_UPDATE_EVERY_S = 600
# A runner that is never idle never self-updated: laptop-a's claude-a ran pre-6d5d2b3 code for a day while serve and
# the merger ran current code, and their instructions mixed in one prompt (Q-164, Q-166, Q-167, Oct 6 2026). Once
# its loaded code has been behind for this long, a busy runner drains (claims nothing new) and restarts.
STALE_DRAIN_AFTER_S = 1800
CARRY_FILES = ("notes.md", "report.json")      # .swarm-run files handed to the next attempt (Q-160, Q-162)
CARRY_CAP = 6000


# Turn floors by reasoning effort for worker runs. task_limits (turns by size: 30/60/120 in selective-hearing) cut
# S/M runs off with error_max_turns right before they finished (Q-096, Q-103, Q-126, Q-142, Q-143); deeper
# effort spends more turns per step. Codex exec has no turn limit (only the size's time limit applies).
EFFORT_TURN_FLOOR = {"medium": 60, "high": 100, "xhigh": 150, "max": 150}
MAX_TURNS_NOTE = ("The previous attempt ran out of turns right before finishing; its work is on the branch — finish and "
                  "report, do not start over. `git log origin/main..HEAD` and `git diff origin/main...HEAD --stat` show "
                  "what it did; run verify, fix what is left, and produce the report.")


def worker_turns(size_turns: int, effort: str | None) -> int:
    return max(size_turns, EFFORT_TURN_FLOOR.get((effort or "").lower(), 0))


def backoff_seconds(failures: int) -> int:
    """Retry delay after a failed tick: 15, 30, then 60 s at most. Longer gaps let the heartbeat go stale and a
    healthy worker's task gets reaped during an ordinary Wi-Fi blip (Oct 5 2026)."""
    return min(60, 15 * 2 ** (max(1, failures) - 1))          # idle runners look for a newer harness every 10 minutes
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


def worker_env(wt: Path, task: Task) -> dict:
    """Environment for the worker CLI. PYTHONPATH puts the worktree first, so a script run from /tmp imports the
    worktree's package and not the main checkout's editable install (six notes: Q-086 … Q-126)."""
    paths = [str(wt)] + ([str(wt / "src")] if (wt / "src").is_dir() else [])
    old = os.environ.get("PYTHONPATH", "")
    return {"PYTHONPATH": os.pathsep.join(paths + ([old] if old else [])),
            "SWARM_TASK_ID": task.id, "SWARM_WORKTREE": str(wt)}


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
        self._last_update_check = None
        self.auto_update = True
        self._loaded_head: str | None = None     # harness commit this process imported (set by loop())
        self._stale_since = None
        self._idle_since = None
        self._stopping = False
        self.draining = False

    def stop(self) -> None:
        """Ask in-flight runs to requeue their task instead of publishing (Ctrl-C / kill path)."""
        self._stopping = True

    def request_restart(self) -> None:
        """SIGUSR1 / `swarm restart`: claim nothing new, let in-flight runs finish, then re-exec on the current code.
        Killing a busy runner parks half-done work (T-006 lost Fable minutes on Oct 4 2026); draining does not."""
        self.draining = True
        self.log("restart requested: finishing in-flight runs, claiming nothing new")

    def finish_drain_if_idle(self) -> bool:
        if not self.draining or any(self.active.values()):
            return False
        from . import selfupdate
        if self.auto_update:
            selfupdate.check_and_update()     # restart on current origin/main, not on what happened to be on disk
        self.log("drained; restarting runner")
        self.heartbeat(force=True)
        selfupdate.restart_self()
        return True

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
        usr1 = getattr(signal, "SIGUSR1", None)
        if usr1 is not None:
            try:
                signal_fn(usr1, lambda signum, frame: self.request_restart())
            except (ValueError, OSError):
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
        first = self._last_heartbeat is None
        self._last_heartbeat = now
        for name, a in self.agents.items():
            row = self.board.get_agent(name) or AgentRow(name=name)
            row.provider, row.host, row.last_heartbeat = a.provider, self.host, now
            limited = bool(row.cooldown_until and row.cooldown_until > now)
            if first and not row.note.startswith("models ok:") and not limited:   # keep a probe result / limit note
                from .doctor import cli_version, models_note
                row.note = models_note(a, cli_version(a, cwd=self.cfg.repo_root))
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
        if self.draining:
            self.finish_drain_if_idle()
            return 0
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
        if self.auto_update and self._loaded_head is None:
            from .selfupdate import current_head
            self._loaded_head = current_head()
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
                    wait = backoff_seconds(failures)
                    self.log(f"tick failed ({type(e).__name__}: {str(e)[:160]}); retrying in {wait}s")
                    if once:
                        break
                    self.sleep(wait)
                    continue
                if once:
                    break
                now = self.now()
                self.maybe_self_update(now)          # busy or idle: a busy runner drains once its code is stale
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

    def maybe_self_update(self, now) -> None:
        """Nothing in flight: pull swarm-control if main moved and restart on the new code. Runs in flight: never
        touch the checkout under them, but once the loaded code has been behind (upstream moved, or another process
        on this laptop already pulled) for STALE_DRAIN_AFTER_S, drain and restart. A runner that always had work
        never updated before Oct 6 and ran day-old code (Q-164, Q-166, Q-167)."""
        if not self.auto_update or self.draining:
            return
        if self._last_update_check and (now - self._last_update_check).total_seconds() < SELF_UPDATE_EVERY_S:
            return
        self._last_update_check = now
        from . import selfupdate
        if not any(self.active.values()):
            new = selfupdate.check_and_update()
            disk = selfupdate.current_head()
            if new or (self._loaded_head and disk and disk != self._loaded_head):
                self.log(f"swarm-control updated to {new or disk[:9]}; restarting this runner")
                self.heartbeat(force=True)
                selfupdate.restart_self()
            return
        disk = selfupdate.current_head()
        behind = bool(self._loaded_head and disk and disk != self._loaded_head) or selfupdate.upstream_ahead()
        if not behind:
            self._stale_since = None
            return
        self._stale_since = self._stale_since or now
        stale_for = (now - self._stale_since).total_seconds()
        if stale_for >= STALE_DRAIN_AFTER_S:
            self.log(f"harness code {str(self._loaded_head or '?')[:9]} is behind for {int(stale_for // 60)} min "
                     "while busy; draining to restart on the new code")
            self.request_restart()

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
        carried = False
        if not reuse:
            # pushed commits on origin/task/<id> are work, not litter: a retry after a failure (Q-082), a reaped
            # or requeued run whose flags lost "resume" (Q-167). A fresh worktree from main would also be
            # force-pushed over them at the end of the run. Without an attempt, a PR or a commit naming the task the
            # branch is a leftover of an earlier project in the same repo (branch names repeat) and is replaced.
            try:
                self.ws.fetch()
                if self.ws.unmerged_commits(task.id) > 0:
                    carried = reuse = (task.attempts > 0 or bool(task.pr_url)
                                       or self.ws.branch_mentions(task.id))
            except RuntimeError as e:
                self.log(f"[{task.id}] could not check the old branch: {e}")
        self.log(f"[{task.id}] {task.agent} attempt {attempt} model={task.model} reuse={reuse}")
        started = self.now()
        wt = self.ws.provision(task.id, reuse_branch=reuse)
        try:
            if carried:
                note = (f"This worktree continues from the previous attempt's commits on "
                        f"{self.ws.remote}/{task.branch} (current main merged in). Read "
                        f"`git log {self.ws.remote}/{self.cfg.main_branch}..HEAD` before you continue; do not redo them.")
                if note not in task.feedback:
                    task.feedback = (task.feedback.rstrip() + "\n\n" + note).strip()
            conflicts_note, synced, conflicts = self._sync_before_run(task, wt) if reuse else ("", False, [])
            # feedback written for an earlier round (a merger's "conflicts will be left", an old runner's "git
            # rebase origin/main") must not contradict what the harness just did to this worktree (Q-164, Q-166)
            task.feedback = reconcile_sync_feedback(
                task.feedback, synced=synced, conflicts=conflicts,
                markers=self.ws.conflict_marker_files(wt) if reuse else [],
                main_ref=f"{self.ws.remote}/{self.cfg.main_branch}")
            setup = self.ws.run_script(wt, self.cfg.verify.setup_worktree, 600)
            if setup is not None and not setup.ok:   # a broken environment is not the model's job to debug
                task.last_error = f"{self.cfg.verify.setup_worktree} failed: {setup.tail(600)}"[:1900]
                task.flags = list(dict.fromkeys(task.flags + ["env"]))
                task.status = Status.FAILED
                self.board.update_task(task, PUBLISH_FIELDS)
                self.log(f"[{task.id}] setup_worktree failed: {setup.tail(200)!r}")
                return Outcome(task, None, None, None, Status.FAILED)
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
            limit = self.cfg.limit_for(task.size)
            model = task.model or agent_cfg.models["mid"]
            effort = task.effort or agent_cfg.effort.get("mid")
            turns = worker_turns(limit.turns, effort)
            limits_line = (f"Limits for this run: {limit.minutes} minutes wall-clock"
                           + (f", {turns} turns" if agent_cfg.provider == "claude" else "")
                           + ". A background job still running when you stop is lost: bound long evaluations to fit, "
                           "start them early, and record their command, PID and output path in `.swarm-run/notes.md`.")
            prompt = compile_prompt(task, self.cfg, rules_text=self.rules_text, deps_summaries=deps,
                                    structured_output_supported=structured, mcp=mcp, skills=skills,
                                    skill_tool=agent_cfg.provider == "claude", conflicts_note=conflicts_note,
                                    previous_notes=self._previous_carry(task.id, attempt), limits_line=limits_line)
            pf = wt / ".swarm-run" / "prompt.md"
            pf.write_text(prompt)
            spec = RunSpec(prompt_file=pf, model=model, effort=effort, max_turns=turns,
                           budget_usd=limit.budget_usd, timeout_s=limit.minutes * 60, cwd=wt,
                           schema=REPORT_SCHEMA if structured else None, sandbox=agent_cfg.sandbox,
                           extra_args=list(agent_cfg.extra_args), mcp=mcp, plugin_dirs=dirs,
                           mcp_servers=inline, settings=settings, should_stop=self._claim_watch(task),
                           claim_nonce=task.claim_nonce, env=worker_env(wt, task))
            result = self.adapter_factory(agent_cfg).run(spec)
            duration = (self.now() - started).total_seconds()
            self.ledger.append(agent=task.agent, model=model, task_id=task.id, usage=result.usage,
                               duration_s=duration, ok=result.ok)
            self._save_logs(wt, task.id, attempt, result)
            self._save_carry(wt, task.id, attempt, final_report=result.ok and result.structured_output is not None)
            if self._stopping:
                return self._park(task, wt, result)
            if result.rate_limited:
                return self._rate_limited(task, result)
            return self._publish(task, wt, attempt, result)
        finally:
            self.ws.dispose(wt)
            self._bump_agent_row(task.agent, result_usage=None)

    def _sync_before_run(self, task: Task, wt: Path) -> tuple[str, bool, list[str]]:
        """A parked branch drifts from main: merge current main in before the CLI starts. On a conflict the markers
        and MERGE_HEAD stay in the worktree and the prompt lists the files; the worker resolves them with edits,
        `git add` and `git commit`. No worker ever rebases or fetches: the Codex sandbox cannot (Q-140, Q-144,
        Q-146) and Claude workers got rebases wrong under deadline (Q-137).
        Returns (prompt section or "", whether main was merged in, conflicting files)."""
        try:
            ok, conflicts, causes = self.ws.merge_main(wt, keep_conflicts=True)
        except RuntimeError as e:   # a failed fetch must not stop the run; it works on the branch as it is
            self.log(f"[{task.id}] could not merge main before the run: {e}")
            return "", False, []
        if ok:
            return "", True, []
        self.log(f"[{task.id}] merged main with conflicts left for the worker ({', '.join(conflicts)})")
        return merge_conflict_instructions(conflicts, causes, f"{self.ws.remote}/{self.cfg.main_branch}"), True, conflicts

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

    def _save_carry(self, wt: Path, task_id: str, attempt: int, *, final_report: bool) -> None:
        """Keep the worker's running notes (and its draft report when the run produced no final one) beside the
        attempt's logs: the worktree, and .swarm-run with it, is deleted when the run ends. Before Oct 6 a
        max-turns attempt lost both and the retry re-derived measurements it had already taken (Q-160, Q-162)."""
        logdir = self.log_dir / task_id / f"attempt-{attempt}"
        for name in CARRY_FILES:
            if name == "report.json" and final_report:
                continue
            src = wt / ".swarm-run" / name
            try:
                if src.is_file() and src.stat().st_size:
                    logdir.mkdir(parents=True, exist_ok=True)
                    (logdir / name).write_text(src.read_text(errors="replace")[:CARRY_CAP])
            except OSError as e:
                self.log(f"[{task_id}] could not keep .swarm-run/{name}: {e}")

    def _previous_carry(self, task_id: str, attempt: int) -> str:
        """The newest earlier attempt's notes and draft report, verbatim, for the prompt ("" if none on this host)."""
        base = self.log_dir / task_id
        for k in range(attempt - 1, 0, -1):
            d = base / f"attempt-{k}"
            parts = []
            for name, label in (("notes.md", "`.swarm-run/notes.md`"), ("report.json", "draft `.swarm-run/report.json`")):
                f = d / name
                try:
                    text = f.read_text(errors="replace").strip() if f.is_file() else ""
                except OSError:
                    text = ""
                if text:
                    parts.append(f"From attempt {k}, {label}:\n\n```\n{text[:CARRY_CAP]}\n```")
            if parts:
                return "\n\n".join(parts)
        return ""

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
        limited = task.agent
        row = self.board.get_agent(limited) or AgentRow(
            name=limited, provider=self.agents[limited].provider, host=self.host)
        usage = result.usage_limited
        kind = USAGE_LIMIT_NOTE if usage else "rate limited"
        default = timedelta(hours=USAGE_LIMIT_COOLDOWN_H) if usage else timedelta(minutes=RATE_LIMIT_COOLDOWN_MIN)
        reset = result.reset_at if result.reset_at and result.reset_at > now else None
        # Before Oct 6 a "try again at 5:03 AM" reset was not parsed, codex-b cooled down for 15 minutes, came back
        # and was handed two more tasks that failed the same way.
        row.status = "cooldown"
        row.cooldown_until = reset or (now + default)
        row.note = (f"{kind} until {row.cooldown_until.strftime('%H:%M')} UTC "
                    f"(hit at {now.strftime('%H:%M')}{'' if reset else ', no reset time given'})")
        self.board.upsert_agent(row)
        # hand the task to someone else now; otherwise it bounces back to this agent at every cooldown end
        # (T-043 lost an hour that way on Oct 5 2026 while two agents idled)
        try:
            from .router import context_from_board, route
            agent, model, effort = route(task, self.cfg, context_from_board(self.board, self.cfg, now), exclude={limited})
            if agent != limited:
                task.agent, task.model, task.effort = agent, model, effort
        except Exception as e:  # noqa: BLE001 - routing must never block the requeue
            self.log(f"[{task.id}] reroute after rate limit failed: {e!r}")
        task.status, task.claim_nonce = Status.READY, ""
        task.last_error = f"{kind}: {result.error[:300]}"
        self.board.update_task(task, ["status", "claim_nonce", "last_error", "agent", "model", "effort"])
        self.log(f"[{task.id}] {kind}; {limited} cooling down until {row.cooldown_until}"
                 + (f"; task rerouted → {task.agent}" if task.agent != limited else ""))
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

        markers: list[str] = []
        if changed:
            # main often moves while a worker runs; verify and review the branch on top of current main (Q-097,
            # Q-121). Merged, not rebased (see Workspace.merge_main). A conflict here is aborted and left to the
            # merger, which sends the task back; the next run starts with the markers in place.
            # commit_all also concludes a merge the worker resolved but could not commit (sandbox).
            self.ws.commit_all(wt, f"{task.id}: {(report.summary or 'work in progress')[:60]}")
            markers = self.ws.conflict_marker_files(wt)
            try:
                if not markers and not self.ws.rebase_in_progress(wt):
                    ok, conflicts, _ = self.ws.merge_main(wt, keep_conflicts=False)
                    if ok:
                        changed = self.ws.changed_files(wt)
                    else:
                        self.log(f"[{task.id}] pre-verify merge of main conflicted ({', '.join(conflicts)}); the merger will ask")
            except RuntimeError as e:   # a failed fetch must not lose the run's result
                self.log(f"[{task.id}] pre-verify merge skipped: {e}")

        verify = self.ws.run_script(wt, self.cfg.verify.fast, 900) if changed else None
        verify_ok = verify.ok if verify is not None else None
        if verify is not None and not verify.ok:
            # the failing step first, each section capped on its own (Q-168: the ruff errors were cut off)
            verify_tail = verify_feedback(verify.out + ("\n" + verify.err if verify.err else ""),
                                          script=self.cfg.verify.fast or "verify", code=verify.code)
        else:
            verify_tail = verify.tail(1500) if verify is not None else ""
        placeholders = self._placeholder_hits(wt) if changed and verify_ok is not False and not markers else []

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
        try:
            notes_file = wt / ".swarm-run" / "notes.md"
            notes = notes_file.read_text(errors="replace").strip() if notes_file.is_file() else ""
        except OSError:
            notes = ""
        if notes:   # on the board too, so a retry on another laptop (or the orchestrator) can read them
            md += "\n\n### Worker notes (.swarm-run/notes.md)\n\n" + notes[:3000]
        self.board.append_task_report(task, f"Report — attempt {attempt}", md)
        if report.status == "blocked" and not report.question:
            report.question = {"kind": "blocking", "options": [], "proceeding_with": "",
                               "text": report.summary or f"{task.id} reported blocked without saying why"}
        if report.question:
            self._file_question(task, report, report.question)
        hf = harness_feedback_question(task, report)
        if hf:
            self._file_question(task, report, hf, context=hf.pop("context"))
        from .relay import deliver_messages
        for note in deliver_messages(self.board, task, report):
            self.log(f"[{task.id}] {note.text[:120]}")

        status = self._decide(task, report, result, changed, verify_ok, verify_tail, push_error, markers, placeholders)
        if not self.publish_outcome(task, status):
            return Outcome(task, report, result, verify_ok, self.board.get_task(task.id).status)
        self.log(f"[{task.id}] → {status.value}")
        return Outcome(task, report, result, verify_ok, status)

    def publish_outcome(self, task: Task, status: Status) -> bool:
        """Write the run's result unless the board closed the task meanwhile (human merge, `swarm cut`) or another
        runner holds the claim now; a late publish must never reopen finished work (T-001 was redone, Oct 4 2026)."""
        fresh = self.board.get_task(task.id)
        if fresh is not None:
            if fresh.status in (Status.DONE, Status.CUT):
                self.log(f"[{task.id}] result discarded: task is {fresh.status.value} on the board")
                return False
            if fresh.claim_nonce and task.claim_nonce and fresh.claim_nonce != task.claim_nonce:
                self.log(f"[{task.id}] result discarded: claim now belongs to another run")
                return False
        task.status = status
        self.board.update_task(task, PUBLISH_FIELDS)
        return True

    def _placeholder_hits(self, wt: Path) -> list[tuple[str, int, str]]:
        patterns = self.cfg.verify.placeholders
        files = self.cfg.verify.placeholder_files
        patterns = DEFAULT_PLACEHOLDER_PATTERNS if patterns is None else patterns
        files = DEFAULT_PLACEHOLDER_FILES if files is None else files
        if not patterns or not files:
            return []
        try:
            added = self.ws.added_lines(wt)
        except RuntimeError:
            return []
        added = {f: lines for f, lines in added.items() if not f.startswith(HARNESS_PATHS)}   # the runner's own logs
        fenced = {}
        for rel in added:
            if rel.endswith((".md", ".markdown")):
                try:
                    fenced[rel] = fenced_lines((wt / rel).read_text(errors="ignore"))
                except OSError:
                    pass
        return placeholder_hits(added, patterns=patterns, files=files, fenced=fenced)

    def _decide(self, task: Task, report: Report, result: RunResult, changed: list[str],
                verify_ok: bool | None, verify_tail: str, push_error: str, markers: list[str] = (),
                placeholders: list[tuple[str, int, str]] = ()) -> Status:
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
        if markers:
            # conflict markers left in place (committed so the work is kept): never send that to review or merge
            task.review_rounds += 1
            if task.review_rounds > self.cfg.max_review_rounds:
                self._file_question(task, report, {
                    "kind": "blocking", "text": f"{task.id} still has conflict markers in {', '.join(markers)}",
                    "options": ["resolve by hand", "cut", "split"], "proceeding_with": ""})
                return Status.BLOCKED
            task.feedback = ("Conflict markers are still in: " + ", ".join(markers) + ". Edit each file so it keeps "
                             "main's intent and this task's change, remove every `<<<<<<<`/`=======`/`>>>>>>>` line, "
                             "then `git add` the files and `git commit`. Do not run `git rebase`, `git fetch` or "
                             "`git merge`; the harness handles main.")
            task.flags = list(dict.fromkeys(task.flags + ["resume"]))
            return Status.CHANGES_REQUESTED
        if not result.ok and (report.synthesized or result.structured_output is None):
            # the CLI ended abnormally (max turns, timeout, crash) but left work behind: continue on the branch.
            # A draft .swarm-run/report.json written early counts as left-behind work too (Q-096, Q-103, Q-126).
            if task.attempts >= self.cfg.max_attempts:
                task.last_error = result.error[:1900] or "abnormal end"
                return Status.FAILED
            if "max_turns" in result.error:
                note = MAX_TURNS_NOTE
            else:
                note = (f"The previous attempt ended with: {result.error or 'unknown error'}. "
                        "Continue from the current branch state, finish the task, and produce the report.")
            # keep what the worker was addressing (reviewer findings, messages); drop older resume notes
            kept = [] if task.feedback.startswith(("The previous attempt", "Previous attempt")) else [task.feedback.strip()]
            task.feedback = "\n\n".join([note] + [k for k in kept if k])[:6000]
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
            task.feedback = verify_tail
            task.flags = list(dict.fromkeys(task.flags + ["resume"]))
            return Status.CHANGES_REQUESTED
        if placeholders:
            text = placeholder_feedback(list(placeholders))
            task.review_rounds += 1
            if task.review_rounds > self.cfg.max_review_rounds:
                self._file_question(task, report, {
                    "kind": "blocking", "text": f"{task.id} still leaves placeholder tokens after "
                    f"{task.review_rounds} rounds. Fill them by hand, relax verify.placeholders, or cut?",
                    "options": ["human fix", "relax the check", "cut"], "proceeding_with": ""}, context=text)
                return Status.BLOCKED
            task.feedback = text
            task.flags = list(dict.fromkeys(task.flags + ["resume"]))
            return Status.CHANGES_REQUESTED
        task.feedback = ""
        return Status.REVIEW if needs_review(task, self.cfg) else Status.MERGE_READY

    def _file_question(self, task: Task, report: Report, q: dict, *, context: str = "") -> Question:
        question = Question(
            id="", text=q["text"][:QUESTION_TEXT_CAP], kind=q.get("kind", "blocking"),
            context=(context or report.summary)[:1900], options=list(q.get("options") or []),
            proceeding_with=q.get("proceeding_with", ""),
            impact="high" if q.get("kind") == "blocking" else "medium",
            task_id=task.id, asked_by=task.agent or "", status="Open",
        )
        return self.board.create_question(question)
