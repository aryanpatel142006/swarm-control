"""Worker loop: claim → worktree → prompt → run CLI → verify → push/PR → publish. One process per laptop."""
from __future__ import annotations

import contextlib
import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from .adapters import get_adapter
from .adapters.base import RunSpec
from .feedback import (DEFAULT_PLACEHOLDER_FILES, DEFAULT_PLACEHOLDER_PATTERNS, DEFAULT_SCRATCH_PATTERNS,
                       DEFAULT_STRAY_PATTERNS, fenced_lines,
                       placeholder_feedback, placeholder_hits, scope_lint_feedback, scope_lint_lines,
                       scratch_files, out_of_scope_edits, reconcile_sync_feedback, stray_feedback, stray_files,
                       verify_feedback)
from .board.base import Board, claim_task
from .config import Config, config_signature
from .models import QUESTION_TEXT_CAP, TIERS, USAGE_LIMIT_NOTE, AgentRow, Question, Report, RunResult, Status, Task, utcnow
from .policy import in_scope, needs_review
from .prompt import compile_prompt, load_rules
from .tools import ensure_plugins, installed_plugins, plugin_dirs, plugin_settings
from .report import (REPORT_SCHEMA, debts_markdown, decisions_markdown, earlier_feedback_lines, earlier_questions,
                     earlier_questions_note, harness_feedback_question, parse_report, repeated_feedback,
                     repeated_question, report_to_markdown)
from .usage import Ledger
from .workspace import Workspace, automerge_note, merge_conflict_instructions

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
# A drain logs what it is still waiting on this often, and gives up after one size limit (field note 90).
DRAIN_LOG_EVERY_S = 60
# A task whose worktree still has a live worker CLI that this runner cannot stop is not claimed again for this long.
CLI_BUSY_RETRY_S = 300
WORKTREE_PIDFILE = ".swarm-run/cli.pid"
CARRY_FILES = ("notes.md", "report.json")      # .swarm-run files handed to the next attempt (Q-160, Q-162)
CARRY_CAP = 6000            # per file, in the prompt only: the files themselves are kept and restored whole
CARRY_FILE_MAX = 1_000_000  # a carried file bigger than this is cut (never JSON: an invalid report helps nobody)
# Other files the worker left in .swarm-run (helper scripts, partial results) are kept for the next attempt too: a
# resumed T-095 had to rewrite enr_phase.py and a3runs.sh that its notes still pointed to (Q-250, Q-257, Q-274).
CARRY_EXTRA_MAX_FILE = 256_000
CARRY_EXTRA_MAX_TOTAL = 2_000_000
CARRY_SKIP = {"prompt.md", "notes.md", "report.json", "previous_report.json", "verify.log", "cli.pid"}


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


def worker_env(wt: Path, task: Task, cfg: Config | None = None, host: str | None = None) -> dict:
    """Environment for the worker CLI. PYTHONPATH puts the worktree first, so a script run from /tmp imports the
    worktree's package and not the main checkout's editable install (six notes: Q-086 … Q-126). With a config: the
    project's `env:` block (shared data paths, Q-198) and the per-host verify lock (SWARM_VERIFY_LOCK_DIR, and
    `swarm-lock` on PATH) so the worker's own verify runs share the machine's slots with the harness's."""
    paths = [str(wt)] + ([str(wt / "src")] if (wt / "src").is_dir() else [])
    old = os.environ.get("PYTHONPATH", "")
    env = {}
    if cfg is not None:
        env.update(cfg.project_env())
        from .hostlock import worker_lock_env
        slots = cfg.hosts[host].max_parallel_verify if host in cfg.hosts else 2
        env.update(worker_lock_env(cfg.project, slots, cfg.verify.exclusive_max_minutes))
    env.update({"PYTHONPATH": os.pathsep.join(paths + ([old] if old else [])),
                "SWARM_TASK_ID": task.id, "SWARM_WORKTREE": str(wt)})
    return env


def lock_wait_cap(timeout_s: int) -> int:
    """At most this much wall-clock is added back for swarm-lock waits: half the size's limit."""
    return int(timeout_s) // 2


def _kill_group(pid: int, *, hard: bool = False) -> None:
    import signal
    sig = signal.SIGKILL if hard else signal.SIGTERM
    try:
        os.killpg(pid, sig)     # the adapter starts the CLI in its own session: pgid == pid
    except (ProcessLookupError, PermissionError, OSError):
        try:
            os.kill(pid, sig)
        except OSError:
            pass


def _session_leader(pid: int) -> bool:
    """The adapter starts every CLI in a session of its own (pgid == pid). A recycled pid almost never is one,
    so this guards a stale pid file from signalling an unrelated process."""
    try:
        return os.getpgid(pid) == pid
    except OSError:
        return False


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


def pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:      # exists, owned by someone else
        return True
    except OSError:
        return False
    return True


@dataclass
class InFlight:
    """One dispatched run, for the drain: which agent slot it holds, its worker thread and its CLI pid."""
    task_id: str
    agent: str
    size: str
    started: datetime
    thread: threading.Thread | None = None     # None until the executor starts it
    pid: int | None = None                     # the worker CLI, once launched
    phase: str = "queued"                      # queued → setup → cli → publish

    def alive(self) -> bool:
        """A run is in flight while its CLI is alive, or its thread is still setting up or publishing. A run whose
        CLI is gone and whose thread has ended is finished, whatever the bookkeeping says."""
        if pid_alive(self.pid):
            return True
        if self.thread is None:
            return self.phase == "queued"
        return self.thread.is_alive()

    def describe(self, now: datetime) -> str:
        mins = int(max(0.0, (now - self.started).total_seconds()) // 60)
        if self.pid:
            proc = f"pid {self.pid} {'alive' if pid_alive(self.pid) else 'exited'}"
        else:
            proc = "no CLI yet"
        return f"{self.task_id} ({self.agent}, {self.phase}, {proc}, {mins} min)"


def _has_feedback(report_text: str) -> bool:
    """A carried report (JSON) that holds at least one harness_feedback entry."""
    if '"harness_feedback"' not in report_text:
        return False
    try:
        data = json.loads(report_text)
    except ValueError:
        return False
    return isinstance(data, dict) and bool(data.get("harness_feedback"))


def requeue_status(prev_status: Status | None) -> Status:
    """Where a run that ended without a result (rate limit, runner stop) puts its task back: the status it was
    claimed from when that was Changes Requested (the reviewer's or merger's round is still open), else Ready."""
    return Status.CHANGES_REQUESTED if prev_status is Status.CHANGES_REQUESTED else Status.READY


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
        self._hb_lock = threading.Lock()          # one agent-row write at a time (heartbeat thread, tick, bump)
        self._hb_stop = threading.Event()
        self._hb_kick = threading.Event()         # set to beat now (a run was just claimed)
        self._hb_thread: threading.Thread | None = None
        self._hb_error: str | None = None         # last heartbeat failure, logged once per streak
        self._last_tick = None                    # wall clock of the main loop's last tick (heartbeat thread watches)
        self._loop_stall_logged = False
        self._last_update_check = None
        self.auto_update = True
        self._loaded_head: str | None = None     # harness commit this process imported (set by loop())
        self._config_sig: str | None = config_signature(getattr(cfg, "path", None))   # config files as loaded
        self._config_bad_sig: str | None = None  # an edit that did not load, logged once
        self._stale_since = None
        self._idle_since = None
        self._stopping = False
        self.draining = False
        self.runs: dict[str, InFlight] = {}       # task id → the run holding a slot in self.active
        self._drain_started = None
        self._drain_deadline_s: int | None = None
        self._drain_last_log = None
        self._cli_busy: dict[str, datetime] = {}  # task id → when a live foreign CLI in its worktree blocked a start
        self._atexit_registered = False

    def stop(self) -> None:
        """Ask in-flight runs to park: their CLI is stopped at the next poll (seconds), the work on disk is committed
        and pushed, and the task is requeued to resume. Before Oct 7 a SIGTERM waited for every CLI to finish on its
        own (up to a size limit), so a restart by hand ended with `kill -9` and the CLI, in its own session, lived
        on as an orphan next to the resumed attempt (T-117)."""
        self._stopping = True

    def _live_cli_pids(self) -> list[tuple[str, int]]:
        """(task id, pid) of every worker CLI this process started that is still alive. Safe from a signal handler:
        no lock (the loop may hold it), and a dict changing under the copy is retried."""
        for _ in range(5):
            try:
                runs = list(self.runs.values())
                break
            except RuntimeError:
                continue
        else:
            return []
        return [(r.task_id, r.pid) for r in runs if r.pid and pid_alive(r.pid)]

    def kill_live_clis(self) -> list[int]:
        """SIGTERM the process group of every live worker CLI (each runs in a session of its own, so killing the
        runner, or the runner's process group, never reaches them: T-117's CLI outlived its runner, Oct 7)."""
        killed = []
        for task_id, pid in self._live_cli_pids():
            _kill_group(pid)
            killed.append(pid)
        return killed

    def _kill_clis_at_exit(self) -> None:
        pids = self.kill_live_clis()
        if pids:
            try:
                self.log(f"exiting: stopped worker CLI pid(s) {', '.join(map(str, pids))}")
            except Exception:   # noqa: BLE001 - the log may be closed at interpreter exit
                pass

    def request_restart(self) -> None:
        """SIGUSR1 / `swarm restart`: claim nothing new, let in-flight runs finish, then re-exec on the current code.
        Killing a busy runner parks half-done work (T-006 lost Fable minutes on Oct 4 2026); draining does not."""
        if self.draining:
            self.log("restart requested again: still draining")
            return
        # runs from a signal handler on the main thread: never take self.lock here (the loop may hold it)
        self.draining = True
        self._drain_started = self.now()
        self._drain_deadline_s = None          # set by the first drain check: one size limit of what is in flight
        self._drain_last_log = None
        self.log("restart requested: finishing in-flight runs, claiming nothing new")

    def drain_timeout_s(self, sizes: list[str]) -> int:
        """One size limit: the longest limit among the runs in flight (the largest configured one when a size is
        unknown). A run still going after that is stuck, not busy."""
        limits = self.cfg.task_limits
        biggest = max((lim.minutes for lim in limits.values()), default=60)
        return int(max((limits[s].minutes if s in limits else biggest for s in sizes), default=0) * 60)

    def reap_finished_runs(self) -> list[str]:
        """Drop slots held by runs that are no longer running: no live CLI and no live thread, or no run record at
        all. Before Oct 6 a usage-limit reroute changed task.agent mid-run and the run's `finally` released the new
        agent's slot, not its own; the phantom kept the drain waiting with no CLI alive (field note 90)."""
        dropped = []
        with self.lock:
            for agent, ids in self.active.items():
                for tid in sorted(ids):
                    run = self.runs.get(tid)
                    if run is not None and run.agent == agent and run.alive():
                        continue
                    ids.discard(tid)
                    if run is not None and run.agent == agent:
                        self.runs.pop(tid, None)
                    dropped.append(f"{tid} ({agent}{', ' + run.describe(self.now()) if run else ', no run record'})")
        for d in dropped:
            self.log(f"in-flight run {d} has no live worker; counting it as finished")
        return dropped

    def finish_drain_if_idle(self) -> bool:
        if not self.draining:
            return False
        self.reap_finished_runs()
        now = self.now()
        with self.lock:
            waiting = [r for agent, ids in self.active.items() for tid in sorted(ids)
                       if (r := self.runs.get(tid)) is not None]
        if self._drain_deadline_s is None:
            self._drain_deadline_s = self.drain_timeout_s([r.size for r in waiting])
        from . import selfupdate
        if waiting:
            elapsed = (now - (self._drain_started or now)).total_seconds()
            if self._drain_deadline_s and elapsed >= self._drain_deadline_s:
                self.log(f"drain timed out after {int(elapsed // 60)} min, still waiting on "
                         + "; ".join(r.describe(now) for r in waiting)
                         + ". Restarting anyway, parking nothing: the restarted runner requeues these tasks to "
                         "resume from their branches")
                for r in waiting:      # an orphaned CLI would keep writing the worktree the resumed attempt reuses
                    if pid_alive(r.pid):
                        self.log(f"[{r.task_id}] stopping worker CLI pid {r.pid}")
                        _kill_group(r.pid)
            else:
                if (self._drain_last_log is None
                        or (now - self._drain_last_log).total_seconds() >= DRAIN_LOG_EVERY_S):
                    self._drain_last_log = now
                    left = max(0, int(self._drain_deadline_s - elapsed)) // 60
                    self.log("draining: waiting on " + "; ".join(r.describe(now) for r in waiting)
                             + f" · gives up in {left} min")
                return False
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
            self.log(f"signal {signum}: stopping the worker CLIs; in-flight runs park their work and requeue")
            self.stop()
            self.kill_live_clis()

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
        with self._hb_lock:
            self._heartbeat_locked(force)

    def _heartbeat_locked(self, force: bool) -> None:
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

    HEARTBEAT_STEP_S = 5.0

    def start_heartbeat(self) -> None:
        """Beat on a thread of its own, every heartbeat_seconds of wall clock, whatever the main loop is doing.
        Until Oct 7 the beat ran at the top of each tick: anything that held the loop (a board call retrying for
        minutes, a git fetch, a slow drain check) silenced it, and serve reaps a silent agent's Running tasks. The
        thread wakes every few seconds and compares wall-clock time, so after the laptop sleeps it beats within
        seconds of waking (Event.wait counts monotonic time, which stops during sleep)."""
        if self._hb_thread is not None and self._hb_thread.is_alive():
            return
        self._hb_stop.clear()
        self._hb_thread = threading.Thread(target=self._heartbeat_loop, name="swarm-heartbeat", daemon=True)
        self._hb_thread.start()

    def stop_heartbeat(self) -> None:
        self._hb_stop.set()
        self._hb_kick.set()
        t = self._hb_thread
        if t is not None and t is not threading.current_thread():
            t.join(timeout=10)
        self._hb_thread = None

    def heartbeat_running(self) -> bool:
        return self._hb_thread is not None and self._hb_thread.is_alive()

    def kick_heartbeat(self) -> None:
        """Write the agent rows now (a run was just claimed): `swarm status` showed the agent idle for up to a
        minute after its runner had claimed a task and started the CLI (T-117, Oct 7)."""
        if self.heartbeat_running():
            self._hb_kick.set()
        else:
            try:
                self.heartbeat(force=True)
            except Exception as e:  # noqa: BLE001 - bookkeeping never fails a run
                self.log(f"heartbeat failed: {e!r}")

    def _heartbeat_loop(self) -> None:
        while not self._hb_stop.is_set():
            force = self._hb_kick.is_set()
            self._hb_kick.clear()
            try:
                self.heartbeat(force=force)
                if self._hb_error:
                    self.log("heartbeat writes again")
                self._hb_error = None
            except Exception as e:  # noqa: BLE001 - a board outage: keep trying, log once per streak
                msg = f"{type(e).__name__}: {str(e)[:160]}"
                if self._hb_error is None:
                    self.log(f"heartbeat failed ({msg}); retrying every {int(self.HEARTBEAT_STEP_S)} s")
                self._hb_error = msg
            self._watch_loop_stall()
            self._hb_kick.wait(self.HEARTBEAT_STEP_S)

    def _watch_loop_stall(self) -> None:
        """The heartbeat no longer proves the main loop is ticking: say so when it has not ticked for a stale window
        (runs in flight keep going; nothing new is claimed until it returns)."""
        if self._last_tick is None:
            return
        quiet = (self.now() - self._last_tick).total_seconds()
        if quiet >= self.cfg.heartbeat_stale_minutes * 60:
            if not self._loop_stall_logged:
                self._loop_stall_logged = True
                self.log(f"main loop has not ticked for {int(quiet // 60)} min (runs in flight continue; nothing new "
                         "is claimed until it does)")
        elif self._loop_stall_logged:
            self._loop_stall_logged = False
            self.log("main loop ticking again")

    # ----- recovery -----
    def _requeue(self, task: Task, why: str, *, prev_status: Status | None = None) -> None:
        task.status, task.claim_nonce = requeue_status(prev_status), ""
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
        self._last_tick = self.now()
        if not self.heartbeat_running():      # tests and --once: no heartbeat thread
            self.heartbeat()
        self.reap_finished_runs()
        if self.draining:
            self.finish_drain_if_idle()
            return 0
        dispatched = 0
        now = self.now()
        rows = {a.name: a for a in self.board.list_agents()}
        for task in self.pending_tasks():
            busy = self._cli_busy.get(task.id)
            if busy is not None and (now - busy).total_seconds() < CLI_BUSY_RETRY_S:
                continue      # its worktree still has a worker CLI this runner could not stop
            row = rows.get(task.agent)
            if row and row.cooldown_until and row.cooldown_until > now:
                continue  # a rate-limited provider cannot run anything, whatever the importance
            if self.free_slots(task.agent) <= 0:
                continue
            with self.lock:
                if task.id in self.active[task.agent]:
                    continue
                self.active[task.agent].add(task.id)
                self.runs[task.id] = InFlight(task.id, task.agent, task.size, now)
            self.executor.submit(self._guarded_run, task)
            dispatched += 1
        return dispatched

    def _guarded_run(self, task: Task) -> None:
        agent = task.agent        # run_task may reroute the task (task.agent changes); the slot is this agent's
        with self.lock:
            run = self.runs.get(task.id)
            if run is not None:
                run.thread, run.phase = threading.current_thread(), "setup"
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
                self.active.get(agent, set()).discard(task.id)
                if task.agent != agent:
                    self.active.get(task.agent, set()).discard(task.id)
                if self.runs.get(task.id) is run:
                    self.runs.pop(task.id, None)

    def loop(self, *, once: bool = False, stop: Callable[[], bool] = lambda: False) -> None:
        self.log(f"swarm run · host={self.host} · agents={', '.join(self.agents)}")
        if self.auto_update and self._loaded_head is None:
            from .selfupdate import current_head
            self._loaded_head = current_head()
        self.install_signal_handlers()
        if not self._atexit_registered:      # any exit path (an uncaught error, a second Ctrl-C) stops the CLIs too
            import atexit
            atexit.register(self._kill_clis_at_exit)
            self._atexit_registered = True
        self.stop_orphan_clis()
        self.recover_orphans()
        self.start_heartbeat()
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
                self.maybe_reload_config()           # an edited .swarm/config.yaml or tuning.yaml applies to the next run
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
        self.executor.shutdown(wait=True)      # the heartbeat thread keeps the rows fresh while runs park
        if self._stopping:
            self.recover_orphans()
        self.stop_heartbeat()

    def maybe_reload_config(self) -> bool:
        """Re-read the config when config.yaml / tuning.yaml / notion.yaml changed on disk. The runner loaded it once at
        start: `worktree_links` (project bacc228, 21:04) never reached runners that had re-exec'd at 20:02, so T-110 and
        T-112 ran without the demo clips (Q-286, Q-288, Q-289), and retro's tuning.yaml waited for a restart too.
        Fields that only shape the next run are swapped in place; a change to agents, hosts, the board or the repo
        restarts the runner (at once when idle, by draining when busy). A config that does not load is ignored
        (logged once) and the old one stays."""
        from .config import RESTART_KEYS, config_changes, config_signature, load_config
        path = getattr(self.cfg, "path", None)
        sig = config_signature(path)
        if sig is None:
            return False
        if self._config_sig is None:
            self._config_sig = sig
            return False
        if sig == self._config_sig or sig == self._config_bad_sig:
            return False
        try:
            new = load_config(path)
        except Exception as e:  # noqa: BLE001 - ConfigError, a YAML syntax error, a half-written file
            self._config_bad_sig = sig
            self.log(f"config changed but does not load ({type(e).__name__}: {str(e)[:160]}); keeping the old one")
            return False
        self._config_sig, self._config_bad_sig = sig, None
        changed = config_changes(self.cfg, new)
        if not changed:
            return False
        structural = [k for k in changed if k in RESTART_KEYS]
        if structural:
            if self.draining:
                return False
            self.log(f"config changed ({', '.join(structural)}): restarting this runner to apply it")
            if not any(self.active.values()):
                self.heartbeat(force=True)
                from . import selfupdate
                selfupdate.restart_self()
            else:
                self.request_restart()
            return True
        self.cfg = new
        self.log(f"config reloaded ({', '.join(changed)}); the next runs use it")
        return True

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
    def _semantic_merge_note(self, wt: Path, before: str) -> str:
        """verify failed right after the harness merged main into the branch without a textual conflict. The usual
        cause is semantic: main changed something this branch's code reads, or the reverse (Q-228: one task changed a
        constant another task's code read implicitly; neither branch was wrong alone)."""
        commits = [c for c in self.ws.git(wt, "log", "--no-merges", "--format=%h %s", f"{before}..HEAD",
                                          check=False).out.splitlines() if c.strip()]
        main_files = self.ws.git(wt, "diff", "--name-only", before, "HEAD", check=False).out.split()
        lines = [f"Note from the harness: this verify ran right after the harness merged current main into the branch "
                 f"({len(commits)} commit(s) from main, no textual conflict). If the failure is in code this task did "
                 "not change, or in a test of it, it is probably a semantic merge conflict: main changed a constant, "
                 "default, signature or file this branch relies on (or the reverse). Fix it on this branch; both "
                 "sides are right on their own."]
        if commits:
            lines.append("Main commits merged: " + "; ".join(commits[:8]) + (" …" if len(commits) > 8 else ""))
        if main_files:
            lines.append("Files main changed: " + ", ".join(main_files[:15]) + (" …" if len(main_files) > 15 else ""))
        return "\n".join(lines)

    def _unmerged_references(self, task: Task) -> str:
        """Tasks the text cites ("the mixer floor from T-096", "target agreement from T-100") that are not Done: their
        work is not on main, so whatever the text attributes to them is not in this worktree (Q-236, Q-238)."""
        ids = dict.fromkeys(re.findall(r"\bT-\d{2,}\b", f"{task.description}\n{task.acceptance}\n{task.feedback}"))
        lines = []
        for tid in ids:
            if tid == task.id:
                continue
            try:
                ref = self.board.get_task(tid)
            except Exception:   # noqa: BLE001 - advice only
                continue
            if ref is None or ref.status in (Status.DONE, Status.CUT):
                continue
            dep = " (a dependency of this task)" if tid in task.depends_on else ""
            lines.append(f"- {tid} \"{ref.title}\" is {ref.status.value}{dep}: its changes are NOT on main or in this "
                         "worktree. Do not build on what the text says it provides; if the task needs it, say so in "
                         "the report (blocked or a stub), and never copy its branch.")
            paths = self._generated_paths(tid)
            if paths:
                lines.append(f"  Generated by {tid} (not merged): usable as data, do not build on its code. Paths in "
                             "the project's shared env dirs:")
                lines += [f"  - {p}" for p in paths]
        return "\n".join(lines)

    def _generated_paths(self, tid: str, cap: int = 10) -> list[str]:
        """Absolute paths under the config `env:` directories whose path names the task (`t141` or `T-141`, any
        case): fixtures and results a task generated before it merged (Q-444). A matching directory is listed, not
        descended into."""
        num = tid.split("-", 1)[-1]
        needles = (f"t{num}".lower(), f"t-{num}".lower())
        found: list[str] = []
        seen_roots: set[str] = set()
        try:
            env = self.cfg.project_env()
        except Exception:   # noqa: BLE001 - advice only
            return []
        for value in env.values():
            root = Path(os.path.expandvars(os.path.expanduser(str(value))))
            if not root.is_dir() or str(root.resolve()) in seen_roots:
                continue
            seen_roots.add(str(root.resolve()))
            base_depth = len(root.parts)
            for dirpath, dirnames, filenames in os.walk(root):
                if len(Path(dirpath).parts) - base_depth >= 5:
                    dirnames[:] = []
                keep = []
                for d in sorted(dirnames):
                    full = Path(dirpath) / d
                    if any(n in str(full).lower() for n in needles):
                        found.append(str(full))
                    else:
                        keep.append(d)
                dirnames[:] = keep
                found += [str(Path(dirpath) / f) for f in sorted(filenames)
                          if any(n in str(Path(dirpath) / f).lower() for n in needles)]
                if len(found) >= cap * 4:
                    break
        return sorted(dict.fromkeys(found))[:cap]

    UNPUSHED_FILE = "unpushed.json"

    def _remember_unpushed(self, task: Task, wt: Path, attempt: int) -> str:
        """A push failed: keep the attempt's local HEAD so the next attempt on this host re-applies it (Q-506: the
        retry's `worktree add -B` reset the branch to the older remote commit and left the new one dangling)."""
        head = self.ws.git(wt, "rev-parse", "HEAD", check=False).out.strip()
        if not head:
            return ""
        try:
            d = self.log_dir / task.id
            d.mkdir(parents=True, exist_ok=True)
            (d / self.UNPUSHED_FILE).write_text(json.dumps({"head": head, "attempt": attempt}))
        except OSError as e:
            self.log(f"[{task.id}] could not record the unpushed commit {head[:12]}: {e!r}")
        return head

    def _reapply_unpushed(self, task: Task, wt: Path) -> str:
        """Put an earlier attempt's unpushed commit back on the freshly provisioned branch: nothing to do when it is
        already an ancestor, a fast-forward when the branch is behind it, a merge otherwise. Returns the prompt note
        (expected commit and base SHA), or "" when there was no unpushed commit. Never raises."""
        rec = self.log_dir / task.id / self.UNPUSHED_FILE
        try:
            head = str(json.loads(rec.read_text()).get("head") or "")
        except (OSError, ValueError, AttributeError):
            return ""
        git = lambda *a: self.ws.git(wt, *a, check=False)   # noqa: E731
        base = git("rev-parse", f"{self.ws.remote}/{self.cfg.main_branch}").out.strip()
        check = f"`git merge-base --is-ancestor {head[:12]} HEAD` must succeed"
        try:
            if not git("cat-file", "-e", f"{head}^{{commit}}").ok:
                how = None
            elif git("merge-base", "--is-ancestor", head, "HEAD").ok:
                how = "already on the branch"
            elif git("merge-base", "--is-ancestor", "HEAD", head).ok and git("merge", "--ff-only", "-q", head).ok:
                how = "fast-forwarded"
            else:
                r = git("-c", "user.email=swarm@local", "-c", "user.name=swarm", "merge", "--no-edit", "-q", head)
                if not r.ok:
                    git("merge", "--abort")
                how = "merged in" if r.ok else None
        except Exception as e:  # noqa: BLE001
            self.log(f"[{task.id}] could not re-apply the unpushed commit {head[:12]}: {e!r}")
            how = None
        if how is None:
            self.log(f"[{task.id}] unpushed commit {head[:12]} could not be re-applied")
            return (f"The previous attempt's commit {head} was never pushed (its push failed) and the harness could "
                    f"not re-apply it here. Base: {self.ws.remote}/{self.cfg.main_branch} at {base}. Check "
                    f"`git show {head[:12]}` (it may exist only on the laptop that ran that attempt) and redo what is "
                    "missing.")
        with contextlib.suppress(OSError):
            rec.unlink()
        self.log(f"[{task.id}] unpushed commit {head[:12]} from the previous attempt: {how}")
        return (f"The previous attempt's commit {head} was not pushed (its push failed); the harness {how} it on "
                f"this branch, so {check}. Base: {self.ws.remote}/{self.cfg.main_branch} at {base} (diff against "
                "that SHA, not a local `main`).")

    def _stop_previous_lock_jobs(self, task: Task) -> list[str]:
        """Before a new attempt: stop `swarm-lock` jobs an earlier attempt of this task left running (Q-521).
        The worker CLI of that attempt is gone (checked just before); its detached measurement is not. Never raises."""
        try:
            from .hostlock import lock_dir, stop_task_lock_jobs
            lines = stop_task_lock_jobs(lock_dir(self.cfg.project), task.id)
        except Exception as e:  # noqa: BLE001 - a cleanup must not stop the run
            self.log(f"[{task.id}] could not check earlier swarm-lock jobs: {e!r}")
            return []
        for line in lines:
            self.log(f"[{task.id}] stopped an earlier attempt's swarm-lock job: {line}")
        return lines

    def _lock_wait_credit(self, task: Task, timeout_s: int):
        """Seconds the worker's processes have waited in `swarm-lock` this run, capped: the adapter extends the
        run's deadline by that much, so a queue behind other worktrees' verify_full does not eat the budget
        (Q-238, Q-241: 5 of 20 minutes lost waiting for the exclusive measurement lock)."""
        from .hostlock import clear_task_waits, lock_dir, task_wait_seconds
        directory = lock_dir(self.cfg.project)
        clear_task_waits(directory, task.id)
        cap = lock_wait_cap(timeout_s)
        return lambda: min(cap, task_wait_seconds(directory, task.id))

    def _claim_watch(self, task: Task):
        """A callable the adapter polls: True once the board no longer shows this run's claim, so a run that was
        reaped and handed to another agent stops burning tokens (Roomcast T-008 ran twice for that reason)."""
        nonce = task.claim_nonce
        state = {"last": 0.0, "lost": False}

        def lost() -> bool:
            if state["lost"]:
                return True
            if self._stopping:      # SIGTERM / Ctrl-C: stop the CLI now and park (see stop())
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
        self.kick_heartbeat()     # the agent row says running T-x now, not at the next beat
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
        nonce = task.claim_nonce
        if self._claim_moved(task, nonce, on_error=False):
            # `swarm assign --force` (or a reap) handed the task on between our claim and here: provisioning now
            # would delete the new owner's worktree at the same path (Q-200, T-093)
            self.log(f"[{task.id}] claim moved before provisioning; leaving the task to its new owner")
            return Outcome(task, None, None, None, task.status)
        blocker = self._live_cli_in_worktree(task)
        if blocker:
            self._cli_busy[task.id] = self.now()
            task.status, task.claim_nonce = requeue_status(prev_status), ""
            task.flags = list(dict.fromkeys(task.flags + ["resume"]))
            task.last_error = (f"a worker CLI from an earlier run (pid {blocker}) is still running in this task's "
                               "worktree; not starting a second one")[:1900]
            self.board.update_task(task, ["status", "claim_nonce", "flags", "last_error"])
            self.log(f"[{task.id}] worker CLI pid {blocker} from an earlier run is still alive in the worktree; not "
                     f"starting a second worker (retry in {CLI_BUSY_RETRY_S // 60} min)")
            return Outcome(task, None, None, None, task.status)
        self._cli_busy.pop(task.id, None)
        stopped_jobs = self._stop_previous_lock_jobs(task)
        wt = self.ws.provision(task.id, reuse_branch=reuse)
        try:
            reapplied = self._reapply_unpushed(task, wt)
            if reapplied:
                task.feedback = (task.feedback.rstrip() + "\n\n" + reapplied).strip()
            if carried:
                note = (f"This worktree continues from the previous attempt's commits on "
                        f"{self.ws.remote}/{task.branch} (current main merged in). Read "
                        f"`git log {self.ws.remote}/{self.cfg.main_branch}..HEAD` before you continue; do not redo them.")
                if note not in task.feedback:
                    task.feedback = (task.feedback.rstrip() + "\n\n" + note).strip()
            conflicts_note, synced, conflicts, automerged_note = (self._sync_before_run(task, wt) if reuse
                                                                  else ("", False, [], ""))
            # feedback written for an earlier round (a merger's "conflicts will be left", an old runner's "git
            # rebase origin/main") must not contradict what the harness just did to this worktree (Q-164, Q-166)
            task.feedback = reconcile_sync_feedback(
                task.feedback, synced=synced, conflicts=conflicts,
                markers=self.ws.conflict_marker_files(wt) if reuse else [],
                main_ref=f"{self.ws.remote}/{self.cfg.main_branch}")
            if stopped_jobs:
                note = ("The harness stopped background jobs an earlier attempt of this task left running under "
                        "`swarm-lock` (they would have held the measurement lock next to this run): "
                        + "; ".join(stopped_jobs) + ". Their output is partial; rerun what you need.")
                task.feedback = (task.feedback.rstrip() + "\n\n" + note).strip()
            if self.cfg.worktree_links:
                try:
                    linked = self.ws.link_ignored(wt, self.cfg.worktree_links)
                    if linked:
                        self.log(f"[{task.id}] linked {len(linked)} gitignored file(s) from the main checkout "
                                 f"({', '.join(self.cfg.worktree_links)})")
                except Exception as e:  # noqa: BLE001 - shared inputs are a convenience, never a reason to fail
                    self.log(f"[{task.id}] could not link shared files: {e!r}")
            setup = self.ws.run_script(wt, self.cfg.verify.setup_worktree, 600, slot=False)
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
            references_note = self._unmerged_references(task)
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
                           + f" (time you spend waiting in `swarm-lock` for a verify slot or the exclusive measurement "
                           f"lock is added back, up to {lock_wait_cap(limit.minutes * 60) // 60} more minutes)"
                           + ". A background job still running when you stop is lost: bound long evaluations to fit, "
                           "start them early, and record their command, PID and output path in `.swarm-run/notes.md`.")
            self._restore_carry(task.id, attempt, wt)
            try:
                questions_note = earlier_questions_note(earlier_questions(self.board.list_questions(), task.id))
            except Exception as e:  # noqa: BLE001 - the run goes ahead without the earlier answers
                questions_note = ""
                self.log(f"[{task.id}] could not read the task's earlier questions: {e!r}")
            prompt = compile_prompt(task, self.cfg, rules_text=self.rules_text, deps_summaries=deps,
                                    structured_output_supported=structured, mcp=mcp, skills=skills,
                                    skill_tool=agent_cfg.provider == "claude", conflicts_note=conflicts_note,
                                    previous_notes=self._previous_carry(task.id, attempt, wt), limits_line=limits_line,
                                    doc_root=wt, branch_log=self._branch_log(wt) if reuse else "",
                                    references_note=references_note, automerged_note=automerged_note,
                                    questions_note=questions_note)
            pf = wt / ".swarm-run" / "prompt.md"
            pf.write_text(prompt)
            spec = RunSpec(prompt_file=pf, model=model, effort=effort, max_turns=turns,
                           budget_usd=limit.budget_usd, timeout_s=limit.minutes * 60, cwd=wt,
                           schema=REPORT_SCHEMA if structured else None, sandbox=agent_cfg.sandbox,
                           extra_args=list(agent_cfg.extra_args), mcp=mcp, plugin_dirs=dirs,
                           mcp_servers=inline, settings=settings, should_stop=self._claim_watch(task),
                           claim_nonce=task.claim_nonce, env=worker_env(wt, task, self.cfg, self.host),
                           extra_time_s=self._lock_wait_credit(task, limit.minutes * 60))
            run_agent = task.agent

            def on_start(pid: int) -> None:
                self._run_phase(task.id, "cli", pid, agent=run_agent)
                self._write_worktree_pidfile(wt, task.id, run_agent, pid)
            spec.on_start = on_start
            self._mark_started(task.id, attempt)
            try:
                result = self.adapter_factory(agent_cfg).run(spec)
            finally:
                self._run_phase(task.id, "publish", agent=run_agent)
                try:
                    (wt / WORKTREE_PIDFILE).unlink()
                except OSError:
                    pass
            duration = (self.now() - started).total_seconds()
            self.ledger.append(agent=task.agent, model=model, task_id=task.id, usage=result.usage,
                               duration_s=duration, ok=result.ok)
            self._save_logs(wt, task.id, attempt, result)
            self._save_carry(wt, task.id, attempt, final_report=result.ok and result.structured_output is not None,
                             result=result, agent_env=agent_cfg.env)
            if self._stopping:
                return self._park(task, wt, result, prev_status=prev_status)
            if result.rate_limited:
                return self._rate_limited(task, result, prev_status=prev_status)
            return self._publish(task, wt, attempt, result)
        finally:
            if self._claim_moved(task, nonce):
                # the worktree path is per task, not per run: the new owner's attempt may already be working in it
                # (Q-200: claude-a's cleanup deleted claude-a2's cwd a few minutes into T-093). Its provision
                # replaces whatever this run left there.
                self.log(f"[{task.id}] claim moved to another run; leaving the worktree to it")
            else:
                self.ws.dispose(wt)
            self._bump_agent_row(task.agent, result_usage=None)

    def _run_phase(self, task_id: str, phase: str, pid: int | None = None, agent: str | None = None) -> None:
        with self.lock:
            run = self.runs.get(task_id)
            if run is not None:
                run.phase = phase
                if pid:
                    run.pid = pid
                agent = agent or run.agent
        if agent and pid:
            self._write_cli_pidfile(task_id, agent, pid)
        elif agent and phase != "cli":
            self._clear_cli_pidfile(task_id, agent)

    # ----- worker CLIs left behind by a previous runner process -----
    def _cli_pidfile(self, task_id: str, agent: str) -> Path:
        return self.log_dir / task_id / f"cli-{agent}.pid"

    def _write_cli_pidfile(self, task_id: str, agent: str, pid: int) -> None:
        """Record the worker CLI so the next runner process on this laptop can stop it. The adapter starts it in its
        own session, so a runner killed with SIGKILL leaves it running: T-117's first CLI (pid 39470) kept going for
        its whole budget after the runner was restarted by hand, next to the resumed attempt's CLI (Oct 7)."""
        path = self._cli_pidfile(task_id, agent)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"pid": pid, "runner_pid": os.getpid(), "agent": agent, "task": task_id,
                                        "started": self.now().isoformat()}))
        except OSError:
            pass

    def _clear_cli_pidfile(self, task_id: str, agent: str) -> None:
        try:
            self._cli_pidfile(task_id, agent).unlink()
        except OSError:
            pass

    def _write_worktree_pidfile(self, wt: Path, task_id: str, agent: str, pid: int) -> None:
        """The same record inside the worktree (`.swarm-run/cli.pid`, never committed or carried): whichever runner
        provisions this task next checks it before reusing the path (Q-302)."""
        try:
            f = wt / WORKTREE_PIDFILE
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(json.dumps({"pid": pid, "runner_pid": os.getpid(), "agent": agent, "task": task_id,
                                     "started": self.now().isoformat()}))
        except OSError:
            pass

    def _live_cli_in_worktree(self, task: Task, *, grace_s: float = 10.0) -> int | None:
        """Before a run provisions (and so replaces) the task's worktree: a worker CLI recorded there that is still
        alive and is not one of this process's runs means an earlier runner's attempt is still writing it (T-117:
        the relaunched runner started a second worker next to the killed runner's CLI, Q-302). When that runner is
        gone (or was this process before a re-exec) nothing will read the CLI's result, so it is stopped; when its
        runner is alive, or it will not die, the pid is returned and no second worker starts."""
        f = self.ws.worktree_path(task.id) / WORKTREE_PIDFILE
        try:
            rec = json.loads(f.read_text())
            pid, owner = int(rec.get("pid") or 0), int(rec.get("runner_pid") or 0)
        except (OSError, ValueError, TypeError):
            return None
        mine = {p for _, p in self._live_cli_pids()}
        if not pid or pid in mine or not pid_alive(pid) or not _session_leader(pid):
            return None
        if owner and owner != os.getpid() and pid_alive(owner):
            return pid           # another live runner owns that CLI
        self.log(f"[{task.id}] stopping worker CLI pid {pid} left in the worktree by a previous runner process")
        _kill_group(pid)
        deadline = time.monotonic() + grace_s
        while pid_alive(pid) and time.monotonic() < deadline:
            time.sleep(0.2)
        if pid_alive(pid):
            _kill_group(pid, hard=True)
            time.sleep(0.5)
        return pid if pid_alive(pid) else None

    def stop_orphan_clis(self, *, grace_s: float = 10.0) -> list[str]:
        """On start: a recorded worker CLI of one of this runner's agents that is still alive belongs to a runner
        process that is gone (or to this process before it re-exec'd); nothing will ever read its result, and the
        resumed attempt reuses its worktree. Stop its process group. A record of another live runner on this laptop
        (claude-a and claude-a2 share the runs directory) is left alone."""
        stopped = []
        me = os.getpid()
        for name in self.agents:
            for path in sorted(self.log_dir.glob(f"*/cli-{name}.pid")):
                try:
                    rec = json.loads(path.read_text())
                    pid, owner = int(rec.get("pid") or 0), int(rec.get("runner_pid") or 0)
                except (OSError, ValueError, TypeError):
                    rec, pid, owner = {}, 0, 0
                if owner and owner != me and pid_alive(owner):
                    continue          # another runner process that is still alive owns it
                if pid and pid_alive(pid) and _session_leader(pid):
                    task_id = rec.get("task") or path.parent.name
                    self.log(f"[{task_id}] stopping worker CLI pid {pid} left by a previous runner process "
                             f"({name}); its attempt is requeued to resume on its branch")
                    _kill_group(pid)
                    deadline = time.monotonic() + grace_s
                    while pid_alive(pid) and time.monotonic() < deadline:
                        time.sleep(0.2)
                    if pid_alive(pid):
                        _kill_group(pid, hard=True)
                    stopped.append(task_id)
                try:
                    path.unlink()
                except OSError:
                    pass
        return stopped

    def _claim_moved(self, task: Task, nonce: str, *, on_error: bool = True) -> bool:
        """True when the board shows another run owning this task: a different claim nonce on a Running task, or
        another agent than the one this run last wrote (an `assign --force` clears the claim and changes the agent
        before the new runner claims). A board read failure counts as moved: leaving a worktree behind is harmless
        (the next provision replaces it), deleting someone's live worktree is not. Before provisioning a read
        failure must not abandon a claimed task, so the caller passes on_error=False there."""
        try:
            fresh = self.board.get_task(task.id)
        except Exception:   # noqa: BLE001
            return on_error
        if fresh is None:
            return False
        if fresh.claim_nonce and fresh.claim_nonce != nonce and fresh.status is Status.RUNNING:
            return True
        return bool(fresh.agent and task.agent and fresh.agent != task.agent)

    def _branch_log(self, wt: Path, cap: int = 20) -> str:
        """`git log --oneline main..HEAD` without merge commits (the harness's own syncs), newest first."""
        r = self.ws.git(wt, "log", "--oneline", "--no-merges", f"{self.ws.remote}/{self.cfg.main_branch}..HEAD",
                        check=False)
        lines = [ln for ln in (r.out if r.ok else "").splitlines() if ln.strip()]
        return "\n".join(lines[:cap] + ([f"… and {len(lines) - cap} older"] if len(lines) > cap else []))

    def _sync_before_run(self, task: Task, wt: Path) -> tuple[str, bool, list[str], str]:
        """A parked branch drifts from main: merge current main in before the CLI starts. On a conflict the markers
        and MERGE_HEAD stay in the worktree and the prompt lists the files; the worker resolves them with edits,
        `git add` and `git commit`. No worker ever rebases or fetches: the Codex sandbox cannot (Q-140, Q-144,
        Q-146) and Claude workers got rebases wrong under deadline (Q-137).
        Returns (prompt section or "", whether main was merged in, conflicting files, auto-merge section or "")."""
        before = self.ws.git(wt, "rev-parse", "HEAD", check=False).out.strip()
        try:
            ok, conflicts, causes = self.ws.merge_main(wt, keep_conflicts=True)
        except RuntimeError as e:   # a failed fetch must not stop the run; it works on the branch as it is
            self.log(f"[{task.id}] could not merge main before the run: {e}")
            return "", False, [], ""
        main_ref = f"{self.ws.remote}/{self.cfg.main_branch}"
        automerged = automerge_note(self.ws.both_changed(wt, before, main_ref, exclude=conflicts), main_ref)
        if ok:
            return "", True, [], automerged
        self.log(f"[{task.id}] merged main with conflicts left for the worker ({', '.join(conflicts)})")
        return merge_conflict_instructions(conflicts, causes, main_ref), True, conflicts, automerged

    def _bump_agent_row(self, agent: str, result_usage=None) -> None:
        """Refresh runs/spend on the Agents row so the status page shows real numbers after each task."""
        try:
            with self._hb_lock:   # never interleave with a heartbeat's read-modify-write of the same row
                row = (self.board.get_agent(agent)
                       or AgentRow(name=agent, provider=self.agents[agent].provider, host=self.host))
                totals = self.ledger.totals(agent)
                row.runs, row.tokens_in, row.tokens_out = row.runs + 1, totals.input_tokens, totals.output_tokens
                row.cost_usd = totals.cost_usd or 0.0
                row.cost_5h_usd = self.ledger.window(agent, 5, self.now()).cost_usd or 0.0
                if self._last_heartbeat and (row.last_heartbeat is None or row.last_heartbeat < self._last_heartbeat):
                    row.last_heartbeat = self._last_heartbeat   # a lagging read must not move the beat back
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

    def _save_carry(self, wt: Path, task_id: str, attempt: int, *, final_report: bool,
                    result: RunResult | None = None, agent_env: dict | None = None) -> None:
        """Keep the worker's running notes (and its draft report when the run produced no final one) beside the
        attempt's logs: the worktree, and .swarm-run with it, is deleted when the run ends. Before Oct 6 a
        max-turns attempt lost both and the retry re-derived measurements it had already taken (Q-160, Q-162).
        A run that ended without a final report and without notes gets the tail of its Claude transcript instead
        (each shell command with the end of its output) as commands.md (Q-183)."""
        logdir = self.log_dir / task_id / f"attempt-{attempt}"
        notes = wt / ".swarm-run" / "notes.md"
        try:
            has_notes = notes.is_file() and bool(notes.read_text(errors="replace").strip())
        except OSError:
            has_notes = False
        if not final_report and not has_notes and result is not None and result.session_id:
            from .transcript import bash_digest, find_transcript
            extra = [Path(agent_env["CLAUDE_CONFIG_DIR"]).expanduser()] if (agent_env or {}).get("CLAUDE_CONFIG_DIR") else []
            path = find_transcript(result.session_id, wt, extra)
            digest = bash_digest(path) if path else ""
            if digest:
                try:
                    logdir.mkdir(parents=True, exist_ok=True)
                    (logdir / "commands.md").write_text(digest)
                except OSError as e:
                    self.log(f"[{task_id}] could not keep the command digest: {e}")
        if final_report and result is not None and isinstance(result.structured_output, dict):
            # a finished attempt sent back only for a merge conflict or verify: the next one reuses this report
            # instead of rebuilding it from docs/decisions and docs/debt (Q-202, Q-203, Q-208)
            try:
                import json as _json
                logdir.mkdir(parents=True, exist_ok=True)
                # whole: a cut JSON file is unreadable (Q-257: previous_report.json ended mid-string)
                (logdir / "final_report.json").write_text(_json.dumps(result.structured_output, indent=1))
            except (OSError, TypeError, ValueError) as e:
                self.log(f"[{task_id}] could not keep the final report: {e}")
        for name in CARRY_FILES:
            if name == "report.json" and final_report:
                continue
            src = wt / ".swarm-run" / name
            try:
                if src.is_file() and src.stat().st_size:
                    text = src.read_text(errors="replace")
                    if len(text) > CARRY_FILE_MAX and name.endswith(".json"):
                        continue          # better no draft report than an invalid one
                    logdir.mkdir(parents=True, exist_ok=True)
                    (logdir / name).write_text(text[:CARRY_FILE_MAX])
            except OSError as e:
                self.log(f"[{task_id}] could not keep .swarm-run/{name}: {e}")
        self._save_extra_files(wt, logdir, task_id)

    def _save_extra_files(self, wt: Path, logdir: Path, task_id: str) -> None:
        """Copy the worker's other `.swarm-run/` files (helper scripts, small partial results) to
        `<attempt>/swarm-run/`, files up to CARRY_EXTRA_MAX_FILE and CARRY_EXTRA_MAX_TOTAL in all."""
        run_dir = wt / ".swarm-run"
        if not run_dir.is_dir():
            return
        import shutil
        total = 0
        try:
            files = sorted(f for f in run_dir.rglob("*") if f.is_file() and not f.is_symlink())
        except OSError:
            return
        for f in files:
            rel = f.relative_to(run_dir)
            if rel.as_posix() in CARRY_SKIP or any(part.startswith(".") or part == "__pycache__" for part in rel.parts):
                continue
            try:
                size = f.stat().st_size
                if size == 0 or size > CARRY_EXTRA_MAX_FILE or total + size > CARRY_EXTRA_MAX_TOTAL:
                    continue
                dest = logdir / "swarm-run" / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(f, dest)
                total += size
            except OSError as e:
                self.log(f"[{task_id}] could not keep .swarm-run/{rel}: {e}")

    def _previous_carry(self, task_id: str, attempt: int, wt: Path | None = None) -> str:
        """The newest earlier attempt's notes and draft report, verbatim, for the prompt ("" if none on this host).
        Without notes, the shell-command digest from its transcript (Q-183). Files the notes name are listed with
        their state now (size, mtime, JSON validity), so a retry knows a background job finished (Q-193)."""
        base = self.log_dir / task_id
        for k in range(attempt - 1, 0, -1):
            d = base / f"attempt-{k}"
            parts = []
            texts = []
            for name, label in (("notes.md", "`.swarm-run/notes.md` (restored there; keep appending to it)"),
                                ("report.json", "draft report (restored at `.swarm-run/previous_report.json`; copy "
                                 "it to `.swarm-run/report.json` and keep updating it)"),
                                ("final_report.json", "final report (that attempt finished; if the feedback above "
                                 "only asks for conflicts or verify fixes, update this report instead of rebuilding "
                                 "it; it is also in `.swarm-run/previous_report.json`)"),
                                ("commands.md", "last shell commands and their output (from the CLI transcript; "
                                 "it wrote no notes)")):
                f = d / name
                try:
                    text = f.read_text(errors="replace").strip() if f.is_file() else ""
                except OSError:
                    text = ""
                if text:
                    texts.append(text)
                    cut = (f"\n… (cut at {CARRY_CAP} characters here; the whole file is in `.swarm-run/`)"
                           if len(text) > CARRY_CAP else "")
                    parts.append(f"From attempt {k}, {label}:\n\n```\n{text[:CARRY_CAP]}\n```{cut}")
                    if (name.endswith("report.json") and not any("harness_feedback` is already" in x for x in parts)
                            and _has_feedback(text)):
                        parts.append(f"Attempt {k}'s `harness_feedback` is already on the board for the orchestrator. "
                                     "In your report, list only harness notes that are new in this attempt (the "
                                     "runner drops repeats; Q-288).")
            extra = d / "swarm-run"
            if extra.is_dir():
                names = sorted(f.relative_to(extra).as_posix() for f in extra.rglob("*") if f.is_file())
                if names:
                    parts.append(f"Other files attempt {k} left in `.swarm-run/`, restored there for you (reuse them "
                                 "instead of rewriting them): " + ", ".join(f"`{n}`" for n in names[:40])
                                 + (f" and {len(names) - 40} more" if len(names) > 40 else ""))
            shared = self._new_shared_files(d)
            if parts or shared:
                status = self._notes_files(d, "\n".join(texts), wt) if texts else ""
                if status:
                    parts.append(f"Files named above, as they are now (attempt {k} ended at the time shown for its "
                                 "logs; a file written later was finished by a background job):\n\n" + status)
                if shared:
                    parts.append(f"Files written to the project's shared data dirs since attempt {k} started (results "
                                 "its background jobs produced, whether or not its notes mention them; Q-241):\n\n"
                                 + shared)
                return "\n\n".join(parts)
        return ""

    def _restore_carry(self, task_id: str, attempt: int, wt: Path) -> None:
        """Put the newest earlier attempt's notes back at `.swarm-run/notes.md` (so the worker keeps appending to
        them) and its final or draft report at `.swarm-run/previous_report.json` (Q-202, Q-203, Q-208: a merge-only
        re-run found only prompt.md and rebuilt its report by hand)."""
        base = self.log_dir / task_id
        for k in range(attempt - 1, 0, -1):
            d = base / f"attempt-{k}"
            report = next((d / n for n in ("final_report.json", "report.json") if (d / n).is_file()), None)
            notes = d / "notes.md"
            extra = d / "swarm-run"
            if not report and not notes.is_file() and not extra.is_dir():
                continue
            try:
                import shutil
                run_dir = wt / ".swarm-run"
                run_dir.mkdir(exist_ok=True)
                if notes.is_file() and not (run_dir / "notes.md").exists():
                    (run_dir / "notes.md").write_text(notes.read_text(errors="replace"))
                if report:
                    (run_dir / "previous_report.json").write_text(report.read_text(errors="replace"))
                if extra.is_dir():
                    for f in sorted(extra.rglob("*")):
                        dest = run_dir / f.relative_to(extra)
                        if f.is_file() and not dest.exists():
                            dest.parent.mkdir(parents=True, exist_ok=True)
                            shutil.copy2(f, dest)
            except OSError as e:
                self.log(f"[{task_id}] could not restore the previous attempt's notes: {e}")
            return

    def _mark_started(self, task_id: str, attempt: int) -> None:
        try:
            d = self.log_dir / task_id / f"attempt-{attempt}"
            d.mkdir(parents=True, exist_ok=True)
            (d / "started").write_text(str(time.time()))
        except OSError:
            pass

    def _new_shared_files(self, attempt_dir: Path, cap: int = 20) -> str:
        """Files in the config `env:` dirs (HEARING_RESULTS_DIR …) modified after that attempt started: Q-241's
        attempt noted one finished run while its background job had written two result files."""
        try:
            since = float((attempt_dir / "started").read_text().strip())
        except (OSError, ValueError):
            return ""
        found = []
        for var, value in self.cfg.project_env().items():
            root = Path(os.path.expanduser(value))
            if not value.startswith(("/", "~")) or not root.is_dir():
                continue
            try:
                for f in list(root.iterdir()) + [g for sub in root.iterdir() if sub.is_dir() for g in sub.iterdir()]:
                    st = f.stat()
                    if f.is_file() and st.st_mtime > since:
                        found.append((st.st_mtime, var, f, st.st_size))
            except OSError:
                continue
        found.sort(reverse=True)
        lines = [f"- `{f}` (${var}) {size} bytes, written {datetime.fromtimestamp(mt, timezone.utc):%H:%M:%S} UTC"
                 for mt, var, f, size in found[:cap]]
        if len(found) > cap:
            lines.append(f"- … {len(found) - cap} more")
        return "\n".join(lines)

    def _notes_files(self, attempt_dir: Path, text: str, wt: Path | None) -> str:
        from .feedback import notes_files_status
        ended = None
        for name in ("stdout.txt", "notes.md", "commands.md"):
            f = attempt_dir / name
            if f.is_file():
                ended = f.stat().st_mtime
                break
        roots = [r for r in (wt, self.cfg.repo_root) if r is not None]
        try:
            return notes_files_status(text, roots, {**os.environ, **self.cfg.project_env()}, ended_at=ended)
        except OSError:
            return ""

    def _park(self, task: Task, wt: Path, result: RunResult, *, prev_status: Status | None = None) -> Outcome:
        """Runner is stopping: keep whatever the model left, push it, and requeue for a resume."""
        if self.ws.changed_files(wt):
            self.ws.commit_all(wt, f"{task.id}: parked by runner stop")
            self.ws.push(wt, task.branch, force_with_lease=True)
        self._requeue(task, "runner stopped mid-task; work parked on the branch", prev_status=prev_status)
        self.log(f"[{task.id}] parked → {task.status.value} (resume)")
        return Outcome(task, None, result, None, task.status)

    def _rate_limited(self, task: Task, result: RunResult, *, prev_status: Status | None = None) -> Outcome:
        now = self.now()
        limited = task.agent
        with self._hb_lock:      # a heartbeat that read the row before this write would drop the cooldown
            row = self._write_cooldown(limited, result, now)
        self._reroute_after_limit(task, limited, now)
        # a merge-conflict or review round goes back as Changes Requested (feedback and PR intact), not Ready
        task.status, task.claim_nonce = requeue_status(prev_status), ""
        kind = USAGE_LIMIT_NOTE if result.usage_limited else "rate limited"
        task.last_error = f"{kind}: {result.error[:300]}"
        self.board.update_task(task, ["status", "claim_nonce", "last_error", "agent", "model", "effort"])
        self.log(f"[{task.id}] {kind}; {limited} cooling down until {row.cooldown_until}"
                 + (f"; task rerouted → {task.agent}" if task.agent != limited else ""))
        return Outcome(task, None, result, None, task.status)

    def _write_cooldown(self, limited: str, result: RunResult, now) -> AgentRow:
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
        return row

    def _reroute_after_limit(self, task: Task, limited: str, now) -> None:
        # hand the task to someone else now; otherwise it bounces back to this agent at every cooldown end
        # (T-043 lost an hour that way on Oct 5 2026 while two agents idled)
        # Only to an agent that can run it now: when every agent is cooling, moving it just swaps cooldowns (field
        # note 85: T-095/T-102 went claude-a -> claude-a2, both limited, and lost their Changes Requested status).
        try:
            from .router import context_from_board, is_available, route
            ctx = context_from_board(self.board, self.cfg, now)
            agent, model, effort = route(task, self.cfg, ctx, exclude={limited})
            target = self.cfg.agents.get(agent)
            if agent != limited and target and is_available(target, ctx.rows.get(agent),
                                                            importance=task.importance, now=now):
                task.agent, task.model, task.effort = agent, model, effort
        except Exception as e:  # noqa: BLE001 - routing must never block the requeue
            self.log(f"[{task.id}] reroute after rate limit failed: {e!r}")

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
        merged_from = ""   # branch HEAD before the pre-verify merge brought main in (for Q-228's note)
        if changed:
            # main often moves while a worker runs; verify and review the branch on top of current main (Q-097,
            # Q-121). Merged, not rebased (see Workspace.merge_main). A conflict here is aborted and left to the
            # merger, which sends the task back; the next run starts with the markers in place.
            # commit_all also concludes a merge the worker resolved but could not commit (sandbox).
            self.ws.commit_all(wt, f"{task.id}: {(report.summary or 'work in progress')[:60]}")
            markers = self.ws.conflict_marker_files(wt)
            try:
                if not markers and not self.ws.rebase_in_progress(wt):
                    before = self.ws.git(wt, "rev-parse", "HEAD", check=False).out.strip()
                    ok, conflicts, _ = self.ws.merge_main(wt, keep_conflicts=False)
                    if ok:
                        changed = self.ws.changed_files(wt)
                        after = self.ws.git(wt, "rev-parse", "HEAD", check=False).out.strip()
                        merged_from = before if before and after and before != after else ""
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
            if merged_from:
                verify_tail = self._semantic_merge_note(wt, merged_from) + "\n\n" + verify_tail
        else:
            verify_tail = verify.tail(1500) if verify is not None else ""
        placeholders = self._placeholder_hits(wt) if changed and verify_ok is not False and not markers else []
        strays = self._stray_files(wt, changed) if changed and verify_ok is not False and not markers else []

        lint_notes = self._scope_lint(task, wt, changed, report) if changed and not markers else []
        lint_notes += self._perturbed_notes(task)

        pr_url, push_error = task.pr_url, ""
        if changed:
            self.ws.commit_all(wt, f"{task.id}: {(report.summary or 'work in progress')[:60]}")
            # only the claim holder pushes this branch, so a lease against the fetched remote ref is safe
            push = self.ws.push(wt, task.branch, force_with_lease=True)
            if push.ok:
                body = report_to_markdown(report, attempt=attempt, verify_ok=verify_ok, verify_tail=verify_tail,
                                          pr_url="", flags=flags, harness_notes=lint_notes)
                try:
                    pr_url = self.ws.pr_create_or_update(task.branch, task.title_with_id(), body) or pr_url
                except RuntimeError as e:
                    self.log(f"[{task.id}] PR failed: {e}")
            else:
                push_error = push.err.strip()[:600] or f"exit {push.code}"
                head = self._remember_unpushed(task, wt, attempt)
                if head:
                    push_error = f"(local commit {head[:12]} not on {self.ws.remote}) {push_error}"
                self.log(f"[{task.id}] push failed: {push_error[:200]}")
        task.pr_url = pr_url

        md = report_to_markdown(report, attempt=attempt, verify_ok=verify_ok, verify_tail=verify_tail,
                                pr_url=pr_url, flags=flags, harness_notes=lint_notes)
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
        filed: list | None = None
        if report.question or report.harness_feedback:
            try:
                filed = self.board.list_questions()
            except Exception as e:  # noqa: BLE001 - filing a repeat beats losing a question or a note
                self.log(f"[{task.id}] could not read the task's earlier questions: {e!r}")
        if report.question:
            again = repeated_question(report.question, earlier_questions(filed or [], task.id))
            if again is not None:
                self.log(f"[{task.id}] question not filed again: it repeats {again.id} "
                         f"({'answered' if again.answer.strip() else again.status.lower()})")
            else:
                self._file_question(task, report, report.question)
        if report.harness_feedback:
            earlier = earlier_feedback_lines(filed or [], task.id)
            fresh = [f for f in report.harness_feedback if not repeated_feedback(f, earlier)]
            if len(fresh) < len(report.harness_feedback):
                self.log(f"[{task.id}] dropped {len(report.harness_feedback) - len(fresh)} harness note(s) an earlier "
                         "attempt already filed")
                report.harness_feedback = fresh
        hf = harness_feedback_question(task, report)
        if hf:
            self._file_question(task, report, hf, context=hf.pop("context"))
        from .relay import deliver_messages
        for note in deliver_messages(self.board, task, report):
            self.log(f"[{task.id}] {note.text[:120]}")

        status = self._decide(task, report, result, changed, verify_ok, verify_tail, push_error, markers, placeholders,
                              strays)
        if lint_notes and status in (Status.REVIEW, Status.MERGE_READY):
            task.feedback = scope_lint_feedback(lint_notes)   # the reviewer's prompt and the board show it (Q-439)
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

    def _scope_lint(self, task: Task, wt: Path, changed: list[str], report: Report) -> list[str]:
        """Non-blocking notes for the reviewer: scratch files the task added, and edits outside its scope that
        nothing names. Never raises: a lint must not lose a run's result."""
        try:
            patterns = self.cfg.verify.scratch_patterns
            patterns = DEFAULT_SCRATCH_PATTERNS if patterns is None else patterns
            scratch = scratch_files(self.ws.added_files(wt), patterns, task.scope) if patterns else []
            outside = out_of_scope_edits(changed, task.scope, exempt=HARNESS_PATHS, skip=scratch,
                                         named_in="\n".join([task.description, task.acceptance,
                                                             report.notes_for_reviewer]))
            return scope_lint_lines(scratch, outside)
        except Exception as e:  # noqa: BLE001
            self.log(f"[{task.id}] scope lint skipped: {e!r}")
            return []

    def _perturbed_notes(self, task: Task) -> list[str]:
        """Lines other tasks wrote because they ran past this task's `swarm-lock --exclusive` hold (Q-478): the
        measurement may have been perturbed. Never raises."""
        try:
            from .hostlock import lock_dir, task_perturbed_notes
            return task_perturbed_notes(lock_dir(self.cfg.project), task.id)
        except Exception as e:  # noqa: BLE001
            self.log(f"[{task.id}] perturbed-notes skipped: {e!r}")
            return []

    def _stray_files(self, wt: Path, changed: list[str]) -> list[str]:
        patterns = self.cfg.verify.stray_files
        patterns = DEFAULT_STRAY_PATTERNS if patterns is None else patterns
        return stray_files([f for f in changed if (wt / f).is_file()], patterns) if patterns else []

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
                placeholders: list[tuple[str, int, str]] = (), strays: list[str] = ()) -> Status:
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
        if placeholders or strays:
            text = "\n\n".join(([placeholder_feedback(list(placeholders))] if placeholders else [])
                               + ([stray_feedback(list(strays))] if strays else []))
            task.review_rounds += 1
            if task.review_rounds > self.cfg.max_review_rounds:
                self._file_question(task, report, {
                    "kind": "blocking", "text": f"{task.id} still leaves placeholder tokens or backup files after "
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
