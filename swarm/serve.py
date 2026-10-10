"""Control loop (exactly one per project): reap, retry, relay answers, promote, review, merge, reroute, status."""
from __future__ import annotations

import re
import threading
import json
import time
from pathlib import Path
from datetime import timedelta
from typing import Callable

from .board.base import Board
from .config import Config
from .models import IMPORTANCES, AgentRow, Question, Status, Task, usage_limited, utcnow
from .router import context_from_board, escalate_importance, is_available, model_for, route, tier_for
from .status import render_headline, render_status
from .workspace import Workspace

IMPACT_TO_IMPORTANCE = {"high": "high", "medium": "normal", "low": "low"}
FAST_STEPS = ("assigned", "reaped", "reconciled", "redistributed", "retried", "relayed", "promoted", "rerouted")
# Routing (reroute, rebalance, redistribute_on_return) only ever moves queued work. Review, Merge Ready, Blocked,
# Running and Done rows are never reassigned, and routing never writes a status (field note 85: T-095, approved with
# PR #88 open, and T-102, in Review with PR #93, bounced between two cooling agents and ended Ready, Oct 6 2026).
ROUTABLE = (Status.READY, Status.BACKLOG)


def _dependency_cycles(tasks: list[Task]) -> list[list[str]]:
    """Each dependency cycle once (Tarjan's strongly connected components; linear in tasks + edges)."""
    graph = {t.id: [d for d in t.depends_on] for t in tasks}
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    stack: list[str] = []
    on: set[str] = set()
    out: list[list[str]] = []
    counter = [0]

    def strong(v: str) -> None:
        index[v] = low[v] = counter[0]
        counter[0] += 1
        stack.append(v)
        on.add(v)
        for w in graph.get(v, []):
            if w not in graph:
                continue
            if w not in index:
                strong(w)
                low[v] = min(low[v], low[w])
            elif w in on:
                low[v] = min(low[v], index[w])
        if low[v] == index[v]:
            comp = []
            while True:
                w = stack.pop()
                on.discard(w)
                comp.append(w)
                if w == v:
                    break
            if len(comp) > 1 or v in graph.get(v, []):
                out.append(sorted(comp))
    for v in graph:
        if v not in index:
            strong(v)
    return out


class Server:
    def __init__(self, cfg: Config, board: Board, ws: Workspace, *, reviewer, merger, now=utcnow,
                 sleep: Callable[[float], None] = time.sleep, log=print, host: str = "serve",
                 status_every_s: int = 900, review_batch: int = 2, background: bool = False):
        self.cfg, self.board, self.ws, self.reviewer, self.merger = cfg, board, ws, reviewer, merger
        self.now, self.sleep, self.log, self.host = now, sleep, log, host
        self.status_every_s, self.review_batch, self.background = status_every_s, review_batch, background
        self._last_status = None
        self._pr_checked: dict = {}
        self._agent_seen: dict = {}      # agent name -> last status we saw, to notice offline -> online
        # self-improvement: a retro per completed milestone (lessons + tuning committed to main)
        self.retro_state = Path.home() / ".swarm" / cfg.project / "retro.json"
        self.retro = self._default_retro
        self._holds_lock = False
        self._transport_failed_at = None  # last serve step that failed to reach the board at all
        # stops while the machine sleeps, the wall clock does not; an injected clock (tests) never sleeps
        self.monotonic = time.monotonic if now is utcnow else (lambda: self.now().timestamp())
        self._clock_ref = None             # (wall, monotonic) at the last reap
        self._woke_at = None               # when this machine last woke from sleep (wall clock)
        self._skew_warned: set[str] = set()
        self._waiting_logged: dict[str, str] = {}   # task id -> "HH:MM UTC" already logged as waiting on cooldowns
        self._slow_thread: threading.Thread | None = None
        self._slow_summary = {"reviewed": 0, "merged": 0}
        # reviewer.parallel > 1: one thread per review in flight (task id -> thread), and when each finished
        self._review_lock = threading.Lock()
        self._review_threads: dict[str, threading.Thread] = {}
        self._review_done: dict[str, tuple] = {}   # task id -> (status the review left it in, when)
        self._review_logged: tuple[str, ...] = ()
        self._busy_ids: set[str] = set()            # reviewed or merged right now on the slow thread (parallel=1)
        self._conflict_checked: dict[str, tuple[str, str]] = {}   # task id -> (branch tip, main tip) found clean
        from .config import config_signature
        self._config_sig = config_signature(getattr(cfg, "path", None))   # config files serve started with
        self._config_warned: str | None = None

    def warn_config_changed(self) -> int:
        """serve reads the config once (field note 27: it kept routing to a removed agent). Runners re-read theirs
        now (field note 97); serve says once per edit that it is still on the old one, instead of acting on it."""
        from .config import config_signature
        sig = config_signature(getattr(self.cfg, "path", None))
        if not sig or not self._config_sig or sig == self._config_sig or sig == self._config_warned:
            return 0
        self._config_warned = sig
        self.log("config changed on disk since serve started; serve still routes, reviews and merges with the config "
                 "it started with: restart serve (`swarm serve`) to apply routing, reviewer or policy changes "
                 "(runners re-read theirs on their own)")
        return 0

    # ----- lock -----
    def acquire_lock(self) -> bool:
        now = self.now()
        row = self.board.get_agent("serve")
        stale = timedelta(minutes=self.cfg.heartbeat_stale_minutes)
        if (row and row.last_heartbeat and (now - row.last_heartbeat) < stale and not self._holds_lock
                and row.host != self.host):
            self.log(f"another serve is alive on {row.host} (heartbeat {row.last_heartbeat})")
            return False
        if row and row.host == self.host and not self._holds_lock:
            self.log("taking over the serve lock from a previous run on this laptop")
        self.board.upsert_agent(AgentRow(name="serve", provider="serve", host=self.host, status="running",
                                         last_heartbeat=now, page_id=row.page_id if row else ""))
        self._holds_lock = True
        return True

    def heartbeat_lock(self) -> None:
        row = self.board.get_agent("serve") or AgentRow(name="serve", provider="serve")
        row.current_task = "orchestrating"     # the board column reads naturally for the control loop too
        row.host, row.status, row.last_heartbeat = self.host, "running", self.now()
        self.board.upsert_agent(row)
        self._holds_lock = True

    # ----- steps -----
    def assign_ids(self) -> int:
        """Cards humans created by hand in Notion have no ID; give them one so branches and lookups work."""
        n = 0
        for t in self.board.list_tasks():
            if t.id and re.fullmatch(r"T-\d+", t.id):
                continue
            t.id = self.board.next_task_id()
            self.board.update_task(t, ["id"])
            self.log(f"[{t.id}] assigned id to hand-made card '{t.title}'")
            n += 1
        return n

    SUSPEND_GAP_S = 60

    def _note_wake(self, now) -> None:
        """The wall clock ran ahead of the monotonic clock since the last reap: this machine slept. Lid closed on
        battery at 23:05 Oct 6 (caffeinate cannot hold a closed lid off battery), opened at 23:46: serve's first
        tick saw claude-a's 41-minute-old heartbeat and reaped T-115 attempt 4 mid-measurement, before the runner
        on the same laptop had beaten once since waking."""
        mono = self.monotonic()
        if self._clock_ref is not None:
            wall0, mono0 = self._clock_ref
            gap = (now - wall0).total_seconds() - (mono - mono0)
            if gap >= self.SUSPEND_GAP_S:
                self._woke_at = now
                self.log(f"this machine was asleep for about {int(gap // 60)} min; heartbeats older than the wake "
                         f"get a fresh {self.cfg.heartbeat_stale_minutes}-minute window before anything is reaped")
        self._clock_ref = (now, mono)

    def reap(self) -> int:
        now = self.now()
        stale = timedelta(minutes=self.cfg.heartbeat_stale_minutes)
        orphan_after = timedelta(minutes=self.cfg.heartbeat_stale_minutes)
        self._note_wake(now)
        woke = self._woke_at

        def beat(row):   # a heartbeat from before this machine woke counts from the wake
            hb = row.last_heartbeat if row else None
            return max(hb, woke) if (hb and woke) else hb
        rows = {a.name: a for a in self.board.list_agents()}
        # A stale heartbeat during a board outage says nothing about the worker: when serve itself could not reach
        # Notion within the stale window, the workers on this network could not either (T-061 was reaped and
        # rerouted to the wrong laptop over a DNS blip, Oct 6 2026). Heartbeats are trusted again once a full
        # stale window has passed without a transport failure.
        outage = (self._transport_failed_at is not None and (now - self._transport_failed_at) < stale)
        for name, row in rows.items():
            # a heartbeat from the future means that laptop's clock is wrong: it can never go stale and its
            # orphan checks are off by the skew (codex-b reported 09:02 UTC at 05:02 UTC, Oct 6 2026)
            if row.last_heartbeat and (row.last_heartbeat - now) > timedelta(minutes=2) and name not in self._skew_warned:
                self._skew_warned.add(name)
                self.log(f"{name}: heartbeat {row.last_heartbeat:%H:%M} UTC is ahead of this clock ({now:%H:%M}); "
                         f"fix that laptop's clock (reaping and orphan checks are unreliable for it)")
        n = 0
        for t in self.board.list_tasks(status=[Status.RUNNING]):
            row = rows.get(t.agent or "")
            alive = bool(row and row.last_heartbeat and (now - beat(row)) < stale)
            if not alive and outage:
                continue
            listed = bool(row and t.id in [x.strip() for x in row.current_task.split(",") if x.strip()])
            # orphan check on the worker's own clock: its heartbeat vs the claim it wrote, never serve's clock
            recent = (t.started is None or row is None or row.last_heartbeat is None
                      or (row.last_heartbeat - t.started) < orphan_after)
            if alive and (listed or recent):
                continue
            t.status, t.attempts, t.claim_nonce = Status.READY, t.attempts + 1, ""
            t.flags = list(dict.fromkeys(t.flags + ["resume"]))
            t.last_error = "worker heartbeat stale; requeued" if not alive else "worker no longer running it; requeued"
            self.board.update_task(t, ["status", "attempts", "claim_nonce", "flags", "last_error"])
            self.log(f"[{t.id}] reaped from {t.agent}")
            n += 1
        for name, row in rows.items():
            if name == "serve":
                continue
            if row.last_heartbeat and (now - beat(row)) >= stale and (row.status != "offline" or row.current_task):
                row.status, row.current_task = "offline", ""
                self.board.upsert_agent(row)
        return n

    # ----- routing guards -----
    def _still_routable(self, t: Task) -> bool:
        """Re-read the row right before a routing write: the listing may be minutes old (a runner claimed it, the
        reviewer moved it). Ready/Backlog rows move freely; a Changes Requested row may change agent only while no
        run holds its claim, and keeps its status. Nothing else is touched."""
        try:
            fresh = self.board.get_task(t.id)
        except Exception:   # noqa: BLE001 - when in doubt, leave the task where it is
            return False
        if fresh is None or fresh.agent != t.agent or fresh.status is not t.status:
            return False
        if fresh.status in ROUTABLE:
            return True
        return fresh.status is Status.CHANGES_REQUESTED and not fresh.claim_nonce

    def _target_ok(self, t: Task, agent: str, ctx, *, leaving_dead: bool) -> bool:
        """A move must land on an agent that can run the task now. Off an offline or unknown agent, a live one that
        is only cooling is still better (it comes back on its own); between cooling agents nothing is gained, and
        before this check reroute swapped T-095/T-102 between claude-a and claude-a2 on every tick (Oct 6 2026)."""
        a = self.cfg.agents.get(agent)
        if a is None:
            return False
        if t.pinned_host and a.host != t.pinned_host:
            return False      # a pinned task waits for its host's agents, whatever happened to its owner (T-216)
        row = ctx.rows.get(agent)
        if is_available(a, row, importance=t.importance, now=ctx.now):
            return True
        if not leaving_dead:
            return False
        return row.status != "offline" and row.last_heartbeat is not None and not usage_limited(row, ctx.now)

    def _log_waiting(self, t: Task, ctx) -> None:
        """One line per task and reset time while every candidate agent is cooling, instead of a move per tick."""
        ends = [r.cooldown_until for name, r in ctx.rows.items()
                if name in self.cfg.agents and r.cooldown_until and r.cooldown_until > ctx.now
                and (not t.pinned_host or self.cfg.agents[name].host == t.pinned_host)]
        until = min(ends).strftime("%H:%M") + " UTC" if ends else "an agent is free"
        if self._waiting_logged.get(t.id) == until:
            return
        self._waiting_logged[t.id] = until
        self.log(f"all agents cooling; {t.id} waits on {t.agent} until {until}")

    def redistribute_on_return(self) -> int:
        """When an agent comes back from offline, every Ready task is routed again with it available, so work that
        piled up on the survivors spreads out immediately instead of waiting for the slower stealing rule."""
        rows = {a.name: a for a in self.board.list_agents()}
        returned = []
        for name in self.cfg.agents:
            status = rows[name].status if name in rows else "offline"
            prev = self._agent_seen.get(name)
            self._agent_seen[name] = status
            if prev == "offline" and status != "offline":
                returned.append(name)
        if not returned:
            return 0
        ctx = context_from_board(self.board, self.cfg, self.now())
        n = 0
        for t in self.board.list_tasks(status=[Status.READY]):
            if t.status not in ROUTABLE:
                continue
            agent, model, effort = route(t, self.cfg, ctx)
            if agent != t.agent and self._target_ok(t, agent, ctx, leaving_dead=False) and self._still_routable(t):
                ctx.queue_depth[t.agent] = max(0, ctx.queue_depth.get(t.agent, 0) - 1)
                ctx.queue_depth[agent] = ctx.queue_depth.get(agent, 0) + 1
                t.agent, t.model, t.effort = agent, model, effort
                self.board.update_task(t, ["agent", "model", "effort"])
                self.log(f"[{t.id}] redistributed → {agent}/{model} ({', '.join(returned)} back online)")
                n += 1
        return n

    def reconcile_merged(self) -> int:
        """A PR merged outside the swarm (a human clicked merge) closes its task; otherwise the task is retried
        and the work redone (T-001, Oct 4 2026). Checked at most once a minute per task."""
        n = 0
        now = self.now()
        for t in self.board.list_tasks(status=[Status.RUNNING, Status.REVIEW, Status.MERGE_READY,
                                               Status.CHANGES_REQUESTED, Status.FAILED, Status.BLOCKED]):
            if not t.pr_url:
                continue
            last = self._pr_checked.get(t.id)
            if last and (now - last).total_seconds() < 60:
                continue
            self._pr_checked[t.id] = now
            info = self.ws.pr_info(t.pr_url)
            if str(info.get("state", "")).upper() == "MERGED":
                t.status, t.claim_nonce, t.feedback = Status.DONE, "", ""
                self.board.update_task(t, ["status", "claim_nonce", "feedback"])
                self.log(f"[{t.id}] PR already merged → Done")
                n += 1
        return n

    def retry_failed(self) -> int:
        n = 0
        ctx = context_from_board(self.board, self.cfg, self.now())
        for t in self.board.list_tasks(status=[Status.FAILED]):
            if t.attempts >= self.cfg.max_attempts:
                self.board.create_question(Question(
                    id="", text=f"{t.id} failed {t.attempts} times: {t.last_error[:600]}", kind="blocking",
                    context=t.last_error[:1900], options=["retry once more", "split", "cut", "human fix"],
                    impact="high", task_id=t.id, asked_by="serve"))
                t.status = Status.BLOCKED
                self.board.update_task(t, ["status"])
                self.log(f"[{t.id}] gave up after {t.attempts} attempts → blocked")
                n += 1
                continue
            if t.attempts >= 2:
                t.importance = escalate_importance(t.importance)
            t.agent, t.model, t.effort = route(t, self.cfg, ctx)
            t.status, t.claim_nonce = Status.READY, ""
            t.feedback = (f"Previous attempt failed: {t.last_error[:600]}. Your branch keeps the earlier pushed commits "
                          "(an unpushed one is re-applied and named below when this laptop still has it); continue from "
                          "it unless it is empty (the harness merges current main into it first)."
                          if t.last_error else "")
            t.flags = [f for f in t.flags if f != "resume"]
            self.board.update_task(t, ["status", "claim_nonce", "importance", "agent", "model", "effort",
                                       "feedback", "flags"])
            self.log(f"[{t.id}] retry #{t.attempts + 1} on {t.agent}/{t.model}")
            n += 1
        return n

    def relay(self) -> int:
        n = 0
        ctx = None
        for q in self.board.list_questions(status="Open"):
            if not q.answer.strip():
                continue
            t = self.board.get_task(q.task_id) if q.task_id else None
            if q.kind == "blocking" and t and t.status is Status.BLOCKED and any(f.startswith("merge_failed") for f in t.flags):
                # a merge that gave up: the human either merged by hand or wants the merge retried
                if "merged" in q.answer.lower():
                    t.status = Status.DONE
                else:
                    t.status = Status.MERGE_READY
                t.flags = [f for f in t.flags if not f.startswith("merge_failed")]
                t.claim_nonce = ""
                self.board.update_task(t, ["status", "flags", "claim_nonce"])
                self.log(f"[{t.id}] merge question answered → {t.status.value}")
            elif q.kind == "blocking" and t and t.status is Status.BLOCKED and q.answer.strip().lower().startswith("cut"):
                t.status = Status.CUT
                self.board.update_task(t, ["status"])
                self.log(f"[{t.id}] cut by answer to {q.id}")
            elif (q.kind == "blocking" and t and t.status is Status.BLOCKED and t.pr_url
                  and q.answer.strip().lower().startswith("accept")):
                # the human overrides the reviewer: ship what is on the branch (T-010, Oct 5 2026)
                t.status, t.feedback, t.claim_nonce = Status.MERGE_READY, "", ""
                self.board.update_task(t, ["status", "feedback", "claim_nonce"])
                self.log(f"[{t.id}] accepted as is by answer to {q.id} → merge")
            elif q.kind == "blocking":
                if t and t.status is Status.BLOCKED:
                    t.feedback = f"Human answer to \"{q.text}\": {q.answer}"[:1900]
                    t.flags = list(dict.fromkeys(t.flags + ["resume"]))
                    t.status, t.claim_nonce = Status.READY, ""
                    self.board.update_task(t, ["feedback", "flags", "status", "claim_nonce"])
                    self.log(f"[{t.id}] unblocked by {q.id}")
            elif q.needs_follow_up:
                ctx = ctx or context_from_board(self.board, self.cfg, self.now())
                follow = Task(id="", title=f"Follow-up: {q.text[:70]}",
                              description=f"Human answer to \"{q.text}\": {q.answer}\n\nContext: {q.context}",
                              acceptance="- the human's answer is implemented\n- verify passes", type="bugfix",
                              importance=IMPACT_TO_IMPORTANCE.get(q.impact, "normal"), size="S",
                              milestone=t.milestone if t else "", scope=list(t.scope) if t else [],
                              depends_on=[t.id] if t and t.status is not Status.DONE else [],
                              feedback=q.answer[:1900])
                follow.status = Status.BACKLOG if follow.depends_on else Status.READY
                follow.agent, follow.model, follow.effort = route(follow, self.cfg, ctx)
                created = self.board.create_task(follow)
                self.log(f"[{created.id}] follow-up created from {q.id}")
            q.status = "Applied"
            self.board.update_question(q, ["status"])
            n += 1
        return n

    def promote(self) -> int:
        """Backlog → Ready once every dependency is Done. A Cut dependency counts as resolved (the worker is told);
        a dependency id that does not exist blocks the task with one question instead of waiting forever."""
        tasks = self.board.list_tasks()
        by_id = {t.id: t for t in tasks}
        ctx = None
        n = 0
        for cycle in _dependency_cycles(tasks):
            members = [by_id[c] for c in cycle]
            if all(m.status is Status.BACKLOG for m in members):
                for m in members:
                    m.status = Status.BLOCKED
                    self.board.update_task(m, ["status"])
                self.board.create_question(Question(
                    id="", text=f"Dependency cycle: {' → '.join(cycle + [cycle[0]])}. Which dependency should be dropped?",
                    kind="blocking", options=[f"drop {c}'s dependency" for c in cycle], impact="high", task_id=cycle[0],
                    asked_by="serve"))
                self.log(f"dependency cycle {cycle} → blocked")
        for t in [x for x in self.board.list_tasks(status=[Status.BACKLOG])]:
            missing = [d for d in t.depends_on if d not in by_id]
            if missing:
                t.status = Status.BLOCKED
                self.board.update_task(t, ["status"])
                self.board.create_question(Question(
                    id="", text=f"{t.id} depends on {', '.join(missing)}, which does not exist. Fix the dependency or drop it?",
                    kind="blocking", options=["drop the dependency", "cut"], impact="medium", task_id=t.id,
                    asked_by="serve"))
                self.log(f"[{t.id}] unknown dependency {', '.join(missing)} → blocked")
                continue
            if not all(by_id[d].status in (Status.DONE, Status.CUT) for d in t.depends_on):
                continue
            cut = [d for d in t.depends_on if by_id[d].status is Status.CUT]
            fields = ["status", "agent", "model", "effort"]
            if cut:
                t.feedback = (t.feedback.rstrip() + "\n\n" if t.feedback.strip() else "") + (
                    f"Dependency {', '.join(cut)} was cut. Build this task without it; stub or skip what it would "
                    "have provided and say so in your report.")
                fields.append("feedback")
            ctx = ctx or context_from_board(self.board, self.cfg, self.now())
            t.agent, t.model, t.effort = route(t, self.cfg, ctx)
            t.status = Status.READY
            self.board.update_task(t, fields)
            ctx.queue_depth[t.agent] = ctx.queue_depth.get(t.agent, 0) + 1
            self.log(f"[{t.id}] promoted → {t.agent}/{t.model}")
            n += 1
        return n

    @staticmethod
    def _by_importance(tasks: list[Task]) -> list[Task]:
        """Critical first, then priority, then id: the reviewer and the merger take the work the
        demo waits on before the rest (Oct 9 2026: T-155, critical, waited behind two high tasks)."""
        def key(t: Task):
            rank = IMPORTANCES.index(t.importance) if t.importance in IMPORTANCES else IMPORTANCES.index("normal")
            return (rank, t.priority if t.priority is not None else 99, t.id)
        return sorted(tasks, key=key)

    # a finished review's status change may not show in the next board query yet (Notion is eventually consistent):
    # a task the review moved out of Review is not started again for this long while the board still says Review
    REVIEW_SETTLE_S = 120

    def review_parallel(self) -> int:
        role = getattr(self.cfg, "reviewer", None)
        try:
            return max(1, int(getattr(role, "parallel", 1) or 1))
        except (TypeError, ValueError):
            return 1

    def review_pending(self) -> int:
        if self.review_parallel() <= 1:   # one review after another on this thread (the behaviour before Oct 9)
            n = 0
            for t in self._by_importance(self.board.list_tasks(status=[Status.REVIEW]))[: self.review_batch]:
                with self._review_lock:
                    self._busy_ids.add(t.id)
                try:
                    self.reviewer.process(t)
                finally:
                    with self._review_lock:
                        self._busy_ids.discard(t.id)
                n += 1
            return n
        return self._start_reviews()

    def reviews_in_flight(self) -> list[str]:
        with self._review_lock:
            return sorted(k for k, th in self._review_threads.items() if th.is_alive())

    def _start_reviews(self) -> int:
        """Start up to reviewer.parallel reviews, each in its own thread, critical first; never one already running
        and never a task whose finished review the board has not caught up with. Returns how many started.
        (Oct 9 2026: six tasks stacked up in Review, one at a time; the one the demo waited on sat 25 min.)"""
        parallel, now = self.review_parallel(), self.now()
        started: list[str] = []
        with self._review_lock:
            self._review_threads = {k: th for k, th in self._review_threads.items() if th.is_alive()}
            free = parallel - len(self._review_threads)
            if free > 0:
                for t in self._by_importance(self.board.list_tasks(status=[Status.REVIEW])):
                    if free <= 0:
                        break
                    if t.id in self._review_threads:
                        continue
                    done = self._review_done.get(t.id)
                    if done and done[0] not in (None, Status.REVIEW) \
                            and (now - done[1]).total_seconds() < self.REVIEW_SETTLE_S:
                        continue
                    th = threading.Thread(target=self._review_one, args=(t,), daemon=True, name=f"review-{t.id}")
                    self._review_threads[t.id] = th
                    started.append(t.id)
                    free -= 1
                    th.start()
            inflight = tuple(sorted(self._review_threads))
        if inflight != self._review_logged:
            self._review_logged = inflight
            if inflight:
                self.log(f"reviewing {', '.join(inflight)} ({len(inflight)}/{parallel} in flight)")
        return len(started)

    def _review_one(self, t: Task) -> None:
        status = None
        try:
            out = self.reviewer.process(t)
            status = getattr(out, "status", None)
        except Exception as e:  # noqa: BLE001 - one failing review must not stop the others or serve
            if "transport" in repr(e):
                self._transport_failed_at = self.now()
            self.log(f"serve step reviewed failed: [{t.id}] {e!r}")
        finally:
            with self._review_lock:
                self._review_done[t.id] = (status, self.now())
                if self._review_threads.get(t.id) is threading.current_thread():
                    del self._review_threads[t.id]

    def join_reviews(self, timeout: float | None = None) -> None:
        """Wait for the reviews in flight (`swarm serve --once`, tests)."""
        with self._review_lock:
            threads = list(self._review_threads.values())
        for th in threads:
            th.join(timeout)

    def merge_pending(self) -> int:
        n = 0
        for t in self._by_importance(self.board.list_tasks(status=[Status.MERGE_READY])):
            with self._review_lock:
                self._busy_ids.add(t.id)
            try:
                merged = self.merger.merge(t)
            finally:
                with self._review_lock:
                    self._busy_ids.discard(t.id)
            if merged:
                n += 1
                self.promote()
        return n

    # ----- merge conflicts caught before a review is spent (Oct 9 2026: T-150 sat in Review with its PR already
    # CONFLICTING; review, approve, a failed merge and a send-back would have cost ~10 min) -----
    def _busy(self) -> set[str]:
        """Tasks some round is working on right now: a review or merge in flight here, or a runner that still lists
        the task on its agent row (it has not finished publishing)."""
        with self._review_lock:
            busy = set(self._busy_ids) | {k for k, th in self._review_threads.items() if th.is_alive()}
        for row in self.board.list_agents():
            busy |= {x.strip() for x in (row.current_task or "").split(",") if x.strip()}
        return busy

    def _merge_conflicts(self, t: Task, tip: str) -> list[str] | None:
        """Files the branch tip conflicts with main in ([] = merges cleanly, None = could not tell). A local
        `git merge-tree --write-tree` on the fetched refs; GitHub's mergeable flag when that fails and a PR exists."""
        main_ref = f"{self.ws.remote}/{self.cfg.main_branch}"
        r = self.ws.git(self.ws.repo_root, "merge-tree", "--write-tree", "--name-only", "--no-messages",
                        main_ref, tip, check=False)
        if r.code == 0:
            return []
        if r.code == 1:
            files = sorted({ln.strip() for ln in r.out.splitlines()[1:] if ln.strip()})
            return files or ["(git named no file)"]
        if not t.pr_url:
            return None
        mergeable = str(self.ws.pr_info(t.pr_url).get("mergeable") or "UNKNOWN").upper()
        if mergeable == "CONFLICTING":
            return ["(file list unavailable: GitHub reports the PR as CONFLICTING)"]
        return [] if mergeable == "MERGEABLE" else None

    def _main_commits_since_base(self, tip: str, files: list[str], cap: int = 8) -> list[str]:
        main_ref = f"{self.ws.remote}/{self.cfg.main_branch}"
        base = self.ws.git(self.ws.repo_root, "merge-base", main_ref, tip, check=False).out.strip()
        if not base:
            return []
        paths = [f for f in files if not f.startswith("(")]
        out = ""
        if paths:
            out = self.ws.git(self.ws.repo_root, "log", "--format=%h %s", f"-{cap}", f"{base}..{main_ref}", "--",
                              *paths, check=False).out
        if not out.strip():
            out = self.ws.git(self.ws.repo_root, "log", "--format=%h %s", f"-{cap}", f"{base}..{main_ref}",
                              check=False).out
        return [ln.strip() for ln in out.splitlines() if ln.strip()]

    def check_conflicts(self) -> int:
        """Every Review / Merge Ready task whose branch no longer merges cleanly with main goes back to its worker
        as Changes Requested (claim cleared, the files and main's commits in the feedback) before a review is spent
        on it. The runner merges main in before the next run and leaves the markers for the worker. Skips a task a
        review, merge or runner is working on right now."""
        tasks = self.board.list_tasks(status=[Status.REVIEW, Status.MERGE_READY])
        if not tasks:
            return 0
        busy = self._busy()
        todo = [t for t in self._by_importance(tasks) if t.id not in busy]
        if not todo:
            return 0
        self.ws.fetch()
        main_ref = f"{self.ws.remote}/{self.cfg.main_branch}"
        main_tip = self.ws.git(self.ws.repo_root, "rev-parse", main_ref, check=False).out.strip()
        n = 0
        for t in todo:
            r = self.ws.git(self.ws.repo_root, "rev-parse", "--verify", "--quiet", f"{self.ws.remote}/{t.branch}",
                            check=False)
            tip = r.out.strip() if r.ok else ""
            if not tip or not main_tip or self._conflict_checked.get(t.id) == (tip, main_tip):
                continue
            files = self._merge_conflicts(t, tip)
            if files is None:
                continue
            if not files:
                self._conflict_checked[t.id] = (tip, main_tip)
                continue
            fresh = self.board.get_task(t.id)
            if fresh is None or fresh.status not in (Status.REVIEW, Status.MERGE_READY) or t.id in self._busy():
                continue
            from .merge import conflict_feedback
            block = ("Caught by serve before review. "
                     + conflict_feedback(files, self._main_commits_since_base(tip, files), self.cfg.main_branch))
            fresh.status, fresh.claim_nonce = Status.CHANGES_REQUESTED, ""
            fresh.feedback = "\n\n".join(x for x in (fresh.feedback.strip(), block) if x)[:6000]
            fresh.flags = list(dict.fromkeys(fresh.flags + ["resume"]))
            self.board.update_task(fresh, ["status", "claim_nonce", "feedback", "flags"])
            self.log(f"[{t.id}] conflict with main ({', '.join(files)}) → changes requested")
            n += 1
        return n

    def reroute(self) -> int:
        now = self.now()
        ctx = context_from_board(self.board, self.cfg, now)
        n = 0
        for t in self.board.list_tasks(status=[Status.READY, Status.CHANGES_REQUESTED]):
            if t.status not in ROUTABLE and not (t.status is Status.CHANGES_REQUESTED and not t.claim_nonce):
                continue
            row = ctx.rows.get(t.agent or "")
            unknown = t.agent not in self.cfg.agents
            cooling = bool(row and row.cooldown_until and row.cooldown_until > now)
            offline = bool(row and row.status == "offline")
            if not unknown and not offline and not cooling:
                continue
            if cooling and t.importance == "critical" and not usage_limited(row, now):
                # critical work waits for the strongest agent only while someone capable is NOT idle
                others_idle = any(a.name != t.agent and ctx.queue_depth.get(a.name, 0) == 0
                                  and is_available(a, ctx.rows.get(a.name), importance="critical", now=now)
                                  and a.strengths.get(t.type, 3) >= 3 for a in self.cfg.agents.values())
                if not others_idle:
                    continue
            agent, model, effort = route(t, self.cfg, ctx, exclude={t.agent} if (cooling or offline) else None)
            if agent == t.agent:
                continue
            if not self._target_ok(t, agent, ctx, leaving_dead=unknown or offline):
                if cooling:
                    self._log_waiting(t, ctx)
                continue
            if not self._still_routable(t):
                continue
            self._waiting_logged.pop(t.id, None)
            t.agent, t.model, t.effort = agent, model, effort
            self.board.update_task(t, ["agent", "model", "effort"])      # never the status (field note 85)
            self.log(f"[{t.id}] rerouted → {agent}/{model}")
            n += 1
        return n

    def rebalance(self) -> int:
        """Work stealing: an idle agent takes one queued non-critical task from an agent with a backlog.
        It may be weaker for that task type (strength >= 3) when the donor has no free slot: idle beats waiting
        (Oct 4 2026: claude-a sat idle while T-002 waited behind codex-b's single slot). An equal-or-stronger
        idle agent also takes from any backlog of two or more. Critical tasks wait for the strongest agent."""
        now = self.now()
        ctx = context_from_board(self.board, self.cfg, now)
        ready = [t for t in self.board.list_tasks(status=[Status.READY]) if t.status in ROUTABLE]
        running = {}
        for t in self.board.list_tasks(status=[Status.RUNNING]):
            running[t.agent] = running.get(t.agent, 0) + 1
        idle = [a for a in self.cfg.agents.values()
                if ctx.queue_depth.get(a.name, 0) == 0 and is_available(a, ctx.rows.get(a.name), importance="normal", now=now)]
        moved = 0
        for idle_agent in idle:
            def may_take(t: Task) -> bool:
                donor = self.cfg.agents[t.agent]
                mine, theirs = idle_agent.strengths.get(t.type, 3), donor.strengths.get(t.type, 3)
                saturated = running.get(t.agent, 0) >= donor.parallel
                return (mine >= theirs and ctx.queue_depth.get(t.agent, 0) >= 2) or (mine >= 3 and saturated)
            candidates = [t for t in ready if t.agent and t.agent != idle_agent.name and t.importance != "critical"
                          and t.agent in self.cfg.agents and may_take(t)
                          and (not t.pinned_host or idle_agent.host == t.pinned_host)]
            if not candidates:
                continue
            # smallest strength gap first, then the one furthest back in the donor's queue
            def gap(t: Task) -> int:
                return self.cfg.agents[t.agent].strengths.get(t.type, 3) - idle_agent.strengths.get(t.type, 3)
            t = sorted(candidates, key=lambda x: (-gap(x), x.priority, x.id))[-1]
            ready.remove(t)
            if not self._still_routable(t):
                continue
            donor = t.agent
            t.agent = idle_agent.name
            t.model, t.effort = model_for(idle_agent, tier_for(t, self.cfg), t.type, self.cfg)
            self.board.update_task(t, ["agent", "model", "effort"])
            ctx.queue_depth[donor] -= 1
            ctx.queue_depth[idle_agent.name] = 1
            self.log(f"[{t.id}] rebalanced {donor} → {idle_agent.name}")
            moved += 1
        return moved

    def write_status(self, force: bool = False) -> str | None:
        now = self.now()
        if not force and self._last_status and (now - self._last_status).total_seconds() < self.status_every_s:
            return None
        self._last_status = now
        tasks, agents, questions = self.board.list_tasks(), self.board.list_agents(), self.board.list_questions()
        text = render_status(self.cfg, tasks, agents, questions, now)
        try:
            self.board.write_headline(*render_headline(self.cfg, tasks, agents, questions, now))
        except Exception as e:  # noqa: BLE001 - an old board without a headline block keeps working
            self.log(f"headline not written: {str(e)[:100]}") if not getattr(self, "_headline_warned", False) else None
            self._headline_warned = True
        try:
            self.board.write_status_page(text)
        except Exception as e:   # a deleted or archived Status page must not take the loop down
            if not getattr(self, "_status_page_broken", False):
                self._status_page_broken = True
                self.log(f"status page cannot be written ({str(e)[:120]}); run `swarm init` again or restore the "
                         "Status page from the Notion trash. The loop keeps going.")
            return None
        return text

    # ----- tick -----
    def _step(self, summary: dict, name: str, fn: Callable[[], int]) -> None:
        try:
            summary[name] = fn()
        except Exception as e:  # noqa: BLE001 - one failing step must not stop the others
            summary[name] = 0
            summary["errors"] = summary.get("errors", 0) + 1
            if "transport" in repr(e):
                self._transport_failed_at = self.now()
            self.log(f"serve step {name} failed: {e!r}")

    def _slow_steps(self) -> None:
        summary: dict = {}
        self._step(summary, "reviewed", self.review_pending)
        self._step(summary, "merged", self.merge_pending)
        self._slow_summary = summary

    # ----- self-improvement -----
    def _default_retro(self, milestone: str | None = None):
        from .retro import run_retro
        from .usage import Ledger, default_ledger_path
        ledger = Ledger(default_ledger_path(self.cfg.project), board=(self.cfg.notion.tasks_ds or "")[:8])
        return run_retro(self.cfg, self.board, self.ws, ledger=ledger, log=self.log, milestone=milestone)

    def maybe_retro(self) -> int:
        """When every task of a milestone is Done or Cut (and at least one is Done), run the retro once for it."""
        tasks = self.board.list_tasks()
        by_ms: dict[str, list[Task]] = {}
        for t in tasks:
            if t.milestone:
                by_ms.setdefault(t.milestone, []).append(t)
        done_before: list[str] = []
        try:
            done_before = json.loads(self.retro_state.read_text()) if self.retro_state.exists() else []
        except (OSError, ValueError):
            done_before = []
        ran = 0
        for ms, group in sorted(by_ms.items()):
            if ms in done_before:
                continue
            if all(t.status in (Status.DONE, Status.CUT) for t in group) and any(t.status is Status.DONE for t in group):
                self.log(f"milestone {ms} complete → retro")
                self.retro(ms)
                done_before.append(ms)
                ran += 1
                try:
                    self.retro_state.parent.mkdir(parents=True, exist_ok=True)
                    self.retro_state.write_text(json.dumps(done_before))
                except OSError:
                    pass
        return ran

    def tick(self) -> dict:
        summary: dict = {"errors": 0}
        self._step(summary, "lock", lambda: (self.heartbeat_lock(), 0)[1])
        self._step(summary, "assigned", self.assign_ids)
        self._step(summary, "reaped", self.reap)
        self._step(summary, "reconciled", self.reconcile_merged)
        self._step(summary, "redistributed", self.redistribute_on_return)
        self._step(summary, "retried", self.retry_failed)
        self._step(summary, "relayed", self.relay)
        self._step(summary, "promoted", self.promote)
        self._step(summary, "conflicts", self.check_conflicts)   # before a review is spent on a branch that conflicts
        if self.background:
            # review + merge can take many minutes; keep the fast steps flowing on the main thread
            if self._slow_thread is None or not self._slow_thread.is_alive():
                summary.update(self._slow_summary)
                self._slow_summary = {"reviewed": 0, "merged": 0}
                self._slow_thread = threading.Thread(target=self._slow_steps, daemon=True)
                self._slow_thread.start()
            else:
                summary.update({"reviewed": 0, "merged": 0})
        else:
            self._step(summary, "reviewed", self.review_pending)
            self._step(summary, "merged", self.merge_pending)
        self._step(summary, "rerouted", self.reroute)
        self._step(summary, "rebalanced", self.rebalance)
        self._step(summary, "retro", self.maybe_retro)
        self._step(summary, "config", self.warn_config_changed)
        self._step(summary, "status", lambda: 1 if self.write_status() else 0)
        summary.pop("lock", None)
        return summary

    def loop(self, stop: Callable[[], bool] = lambda: False) -> None:
        for attempt in range(1, 9):   # the board may be unreachable at startup (Wi-Fi, Notion outage)
            try:
                locked = self.acquire_lock()
                break
            except Exception as e:  # noqa: BLE001
                wait = min(60, 15 * 2 ** (attempt - 1))   # see runner.backoff_seconds
                self.log(f"serve: board unreachable at startup ({type(e).__name__}); retrying in {wait}s")
                self.sleep(wait)
        else:
            raise SystemExit("serve: board unreachable for too long at startup")
        if not locked:
            raise SystemExit("another swarm serve is running; stop it first or wait for its heartbeat to go stale")
        self.write_status(force=True)
        while not stop():
            s = self.tick()
            if any(v for k, v in s.items() if k != "status"):
                self.log(" · ".join(f"{k} {v}" for k, v in s.items() if v and k != "status"))
            self.sleep(self.cfg.serve_seconds)
