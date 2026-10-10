"""Independent reviewer: deterministic verify first, then a read-only model pass from a different family."""
from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from .adapters import get_adapter
from .adapters.base import RunSpec
from .board.base import Board, fresh_flags
from .config import Config
from .failover import ReviewerState, reviewer_state
from .feedback import HARNESS_NOTE_MARK, clip_middle, failing_step_line, verify_feedback
from .lightverify import VerifyChoice, choose_verify, quick_slot_wait
from .models import AUTH_LOST_NOTE, QUESTION_TEXT_CAP, USAGE_LIMIT_NOTE, AgentRow, Question, Status, Task, utcnow
from .prompt import PROMPTS_DIR
from .report import REVIEW_SCHEMA, earlier_questions, earlier_questions_note
from .strict_schema import drop_nulls
from .runner import RATE_LIMIT_COOLDOWN_MIN, STRUCTURED_PROVIDERS, USAGE_LIMIT_COOLDOWN_MIN
from .workspace import CmdResult, Workspace

DIFF_CAP = 60000
REVIEW_TURNS = 25
REVIEW_TIMEOUT_S = 15 * 60


@dataclass
class Verdict:
    verdict: str
    summary: str = ""
    findings: list[dict] = field(default_factory=list)


def parse_verdict(structured: dict | None, worktree: Path) -> Verdict:
    data = structured if isinstance(structured, dict) and "verdict" in structured else None
    if data is None:
        f = Path(worktree) / ".swarm-run" / "review.json"
        if f.exists():
            try:
                loaded = json.loads(f.read_text())
                data = loaded if isinstance(loaded, dict) and "verdict" in loaded else None
            except ValueError:
                data = None
    if data is not None:
        data = drop_nulls(data)   # strict-mode Codex: unused optional fields come back as null
    if data is None or data.get("verdict") not in ("approve", "request_changes", "escalate"):
        return Verdict("escalate", "reviewer produced no usable verdict", [])
    return Verdict(data["verdict"], str(data.get("summary") or ""),
                   [f for f in (data.get("findings") or []) if isinstance(f, dict)])


def build_review_prompt(task: Task, diff: str, verify_tail: str, instructions: str, questions_note: str = "",
                        verify_label: str = "full suite") -> str:
    asked = (["## Questions the worker asked, with the orchestrator's answers so far", "",
              "An answer that asks for a change is part of the acceptance: request changes when the diff does not "
              "make it (T-120 merged before Q-313's answer reached it).", "", questions_note.strip(), ""]
             if questions_note.strip() else [])
    lint = ([f"## {HARNESS_NOTE_MARK[:-1]}", "", task.feedback.strip()[len(HARNESS_NOTE_MARK):].strip(), "",
             "Scratch files should be removed; an edit outside the scope is acceptable only when the diff shows the "
             "acceptance needs it. Say so in a finding when it is not.", ""]
            if task.feedback.startswith(HARNESS_NOTE_MARK) else [])
    parts = [f"# Review of {task.id} · {task.title}", "", instructions.strip(), "", "## Task", "",
             f"- Type: {task.type} · Importance: {task.importance} · Scope: {', '.join(task.scope) or 'any'}",
             f"- Flags from the harness: {', '.join(task.flags) or 'none'}", "", "### Description", "",
             task.description.strip() or "(none)", "", "### Acceptance criteria", "",
             task.acceptance.strip() or "(none given)", "", *lint, *asked, f"## Verify output ({verify_label})", "", "```",
             verify_tail.strip() or "(no verify script)", "```", "", "## Diff against main", "", "```diff",
             diff[:DIFF_CAP] + ("\n[diff truncated]" if len(diff) > DIFF_CAP else ""), "```", "",
             "## Verdict contract", "", "Your final answer MUST be a JSON object matching:", "", "```json",
             json.dumps(REVIEW_SCHEMA, indent=1), "```", "",
             "If your CLI cannot return structured output, write it to `.swarm-run/review.json`."]
    return "\n".join(parts)


FEEDBACK_CAP = 6000   # Notion rich text is chunked (board/notion_props.py), so the field itself holds far more


def _finding_text(f: dict, key: str, *alts: str) -> str:
    for k in (key, *alts):
        v = str(f.get(k) or "").strip()
        if v:
            return v
    return ""


def findings_to_feedback(v: Verdict, cap: int = FEEDBACK_CAP) -> str:
    """Every finding reaches the worker with its text. Long reviews are shortened item by item, never cut at the
    tail: a flat [:1900] once delivered '- [medium] hearing/tse/av_mossformer.py:1' with nothing after it (Q-080)."""
    def render(limit: int | None) -> str:
        def cut(s: str) -> str:
            return s if limit is None or len(s) <= limit else s[:max(1, limit - 1)].rstrip() + "…"
        lines = [f"Reviewer requested changes: {cut(v.summary)}".strip()]
        for f in v.findings:
            loc = str(f.get("file") or "")
            if f.get("line"):
                loc += f":{f['line']}"
            issue = _finding_text(f, "issue", "summary", "description", "text", "message") \
                or "(the reviewer gave no text for this item; see the review report on the task page)"
            fix = _finding_text(f, "fix", "suggestion", "recommendation")
            lines.append(f"- [{f.get('severity', '?')}] {loc or '(no file)'}: {cut(issue)}" + (f" → {cut(fix)}" if fix else ""))
        return "\n".join(lines)

    text, limit = render(None), 1500
    while len(text) > cap and limit > 40:
        text, limit = render(limit), int(limit * 0.7)
    return text if len(text) <= cap else text[:cap - 1] + "…"


_FAILED_ID = re.compile(r"^(?:FAILED|ERROR) (\S+?::\S+|\S+\.py)\b", re.M)


def failing_tests(output: str) -> set[str]:
    """pytest node ids from the short test summary (`FAILED tests/x.py::test_y - …`, `ERROR tests/x.py`)."""
    return set(_FAILED_ID.findall(output or ""))


def _text(r: CmdResult | None) -> str:
    return "" if r is None else r.out + ("\n" + r.err if r.err else "")


class Reviewer:
    def __init__(self, cfg: Config, board: Board, ws: Workspace, *, adapter_factory=get_adapter, log=print,
                 prompt_text: str | None = None, ledger=None, now=utcnow):
        self.cfg, self.board, self.ws, self.adapter_factory, self.log = cfg, board, ws, adapter_factory, log
        self.prompt_text = prompt_text if prompt_text is not None else (PROMPTS_DIR / "reviewer.md").read_text()
        self.ledger = ledger
        self.now = now
        self.limits: dict[str, datetime] = {}   # reviewer candidates out of quota, until (UTC)
        self._active: str | None = None         # the reviewer used last, to log the return to the primary
        self._defer_logged: dict[str, datetime] = {}
        # serve may run several reviews at once (reviewer.parallel): the limits, the active reviewer and the defer log
        # are shared and guarded by this lock; the agent that reviewed a task is per thread (apply reads it after
        # review in the same thread). Worktrees are per task and fetches are serialized in Workspace.
        self._lock = threading.RLock()
        self._local = threading.local()

    @property
    def last_agent(self) -> str | None:
        return getattr(self._local, "last_agent", None)

    @last_agent.setter
    def last_agent(self, value: str | None) -> None:
        self._local.last_agent = value

    # ----- which account reviews (swarm/failover.py) -----
    def state(self) -> ReviewerState:
        """The active reviewer now; logs the return to the primary once its limit has reset."""
        try:
            rows = self.board.list_agents()
        except Exception as e:   # noqa: BLE001 - the board being slow must not stop a review
            self.log(f"reviewer: agent rows unavailable ({e!r}); using the in-memory limits only")
            rows = []
        with self._lock:
            st = reviewer_state(self.cfg, rows, self.now(), dict(self.limits))
            if st.active and self._active and st.active != self._active and st.active == st.primary:
                self.log(f"reviewer back to {st.primary} (its limit reset; {self._active} stands down)")
            if st.active:
                self._active = st.active
        return st

    def _exhausted(self, st: ReviewerState) -> Verdict:
        parts = ", ".join(f"{n} until {u:%H:%M} UTC" for n, u in st.limited.items())
        fallback = "no fallback configured" if len(st.limited) <= 1 else "every fallback is limited too"
        return Verdict("defer", f"reviewer rate limited: {parts}; {fallback}", [])

    def _record_limit(self, agent: str, result) -> datetime:
        """Remember until when `agent` cannot review. A used-up plan also goes on its board row (the same cooldown
        the runner writes), so routing stops sending it work and `swarm status` shows the failover."""
        now = self.now()
        auth = bool(getattr(result, "auth_lost", False)) and not result.rate_limited
        usage = bool(getattr(result, "usage_limited", False)) or auth
        reset = result.reset_at if result.reset_at and result.reset_at > now and not auth else None
        until = reset or now + (timedelta(minutes=USAGE_LIMIT_COOLDOWN_MIN) if usage
                                else timedelta(minutes=RATE_LIMIT_COOLDOWN_MIN))
        with self._lock:
            self.limits[agent] = until
        if usage:
            try:
                from .runner import mark_limited
                a = self.cfg.agents[agent]
                row = self.board.get_agent(agent) or AgentRow(name=agent, provider=a.provider, host=a.host)
                if not (row.cooldown_until and row.cooldown_until >= until):
                    mark_limited(row, AUTH_LOST_NOTE if auth else USAGE_LIMIT_NOTE, now, reset, by="the reviewer")
                    self.board.upsert_agent(row)
            except Exception as e:   # noqa: BLE001 - the in-memory record is enough for the failover itself
                self.log(f"reviewer: could not mark {agent} limited on the board: {e!r}")
        return until

    def review(self, task: Task) -> Verdict:
        if self.cfg.reviewer is not None:
            st = self.state()
            if st.active is None:   # nobody can review: do not spend a verify_full on a review that cannot happen
                return self._exhausted(st)
        wt = self.ws.provision(task.id, reuse_branch=True)
        try:
            setup = self.ws.run_script(wt, self.cfg.verify.setup_worktree, 600, slot=False)
            if setup is not None and not setup.ok:
                return Verdict("escalate", "setup_worktree.sh failed (environment, not code)",
                               [{"severity": "high", "file": self.cfg.verify.setup_worktree or "",
                                 "issue": setup.tail(1200), "fix": "orchestrator: fix the environment, then re-review"}])
            choice = self._choose_verify(task, wt)
            full = self.ws.run_script(wt, choice.command, 1800,
                                      quick_wait_s=quick_slot_wait(self.cfg.verify, choice.files))
            verify_note = ""
            if full is not None and not full.ok:
                full, verify_note, verdict = self._triage_full_failure(task, wt, full, choice.command)
                if verdict is not None:
                    return verdict
            role = self.cfg.reviewer
            if role is None:
                return Verdict("approve", "no reviewer configured; verify passed", [])
            diff = self.ws.git(wt, "diff", f"{self.ws.remote}/{self.cfg.main_branch}...HEAD", check=False).out
            tail = full.tail(1500) if full else ""
            prompt = build_review_prompt(task, diff, (verify_note + "\n\n" + tail).strip() if verify_note else tail,
                                         self.prompt_text, self._questions_note(task),
                                         verify_label=(f"light verify: {choice.command}, docs/eval-only diff"
                                                       if choice.light else "full suite"))
            pf = wt / ".swarm-run" / "review_prompt.md"
            pf.write_text(prompt)
            tried: set[str] = set()
            while True:
                st = self.state()
                agent = st.active
                if agent is None or agent in tried:
                    return self._exhausted(st)
                tried.add(agent)
                result = self._run_model(task, agent, st.model, pf, wt)
                if not result.ok and result.structured_output is None:
                    if result.rate_limited or getattr(result, "auth_lost", False):   # the account, not the code
                        until = self._record_limit(agent, result)
                        nxt = self.state().active
                        if nxt and nxt not in tried:
                            self.log(f"reviewer failover {agent} → {nxt} until {until:%H:%M} UTC")
                            continue
                        self.log(f"[{task.id}] reviewer {agent} limited until {until:%H:%M} UTC and no fallback "
                                 "is free: review deferred")
                        return self._exhausted(self.state())
                    return Verdict("escalate", f"reviewer run failed ({agent}): {result.error[:300]}", [])
                self.last_agent = agent
                return parse_verdict(result.structured_output, wt)
        finally:
            self.ws.dispose(wt)

    def _choose_verify(self, task: Task, wt: Path) -> VerifyChoice:
        """Light verify for a docs/eval-only branch (swarm/lightverify.py), else verify.full; logged either way."""
        try:
            files = self.ws.branch_files(wt)
        except Exception as e:   # noqa: BLE001 - a failed diff means the full verify, never a failed review
            self.log(f"[{task.id}] could not list the branch's files ({e!r}); full verify")
            files = []
        choice = choose_verify(self.cfg.verify, files, task.importance, self.cfg.verify.full)
        if getattr(self.cfg.verify, "light_paths", None):
            self.log(choice.log_line(task.id))
        return choice

    def _questions_note(self, task: Task) -> str:
        try:
            return earlier_questions_note(earlier_questions(self.board.list_questions(), task.id))
        except Exception as e:  # noqa: BLE001 - a review without the answers beats no review
            self.log(f"[{task.id}] could not read the task's questions: {e!r}")
            return ""

    def _run_model(self, task: Task, agent: str, model: str, pf: Path, wt: Path):
        role = self.cfg.reviewer
        agent_cfg = self.cfg.agents[agent]
        structured = agent_cfg.provider in STRUCTURED_PROVIDERS
        spec = RunSpec(prompt_file=pf, model=model or agent_cfg.models["mid"], effort=role.effort,
                       max_turns=REVIEW_TURNS, budget_usd=None, timeout_s=REVIEW_TIMEOUT_S, cwd=wt,
                       schema=REVIEW_SCHEMA if structured else None, read_only=True,
                       sandbox="read-only", extra_args=list(agent_cfg.extra_args))
        started = time.time()
        result = self.adapter_factory(agent_cfg).run(spec)
        if self.ledger is not None:
            self.ledger.append(agent=agent, model=spec.model, task_id=task.id, usage=result.usage, role="reviewer",
                               duration_s=time.time() - started, ok=result.ok)
        return result

    # ----- verify_full failures that are not the task's (Q-189, Q-190, Q-191, Q-196) -----
    def _triage_full_failure(self, task: Task, wt: Path, first: CmdResult, command: str | None = None):
        """verify_full failed on the branch. Run it once more (the slot lock means less load now); if it passes the
        failure was flaky. If it fails again, run it on current main: a failure main has too (same failing tests) is
        not this task's to fix. Either way the task goes on to the model review, and one test-hygiene note goes to
        the orchestrator so ONE task fixes the test (Q-196: two branches patched the same flaky Kokoro test and
        conflicted). Returns (result to show the reviewer, note for the reviewer, verdict or None to continue)."""
        command = command or self.cfg.verify.full     # the light command when the review chose light verify
        script = command or "verify_full"
        second = self.ws.run_script(wt, command, 1800)
        if second is not None and second.ok:
            self.log(f"[{task.id}] {script} failed, then passed on a rerun: flaky, not sent back")
            step = failing_step_line(_text(first))
            ids = (", ".join(sorted(failing_tests(_text(first)))[:5])
                   or ("no test id; failing step: " + step.replace(": ", " - ") if step else "no test id in the output"))
            self._file_test_hygiene(task, f"{script} is flaky ({ids}): it failed on {task.id}'s branch and passed "
                                    "on an immediate rerun", first)
            return second, (f"Note from the harness: {script} failed once and passed on a rerun (flaky; filed for "
                            "the orchestrator). Do not ask this task to fix that test."), None
        again = second or first
        on_main = self._verify_full_on_main(task, command)
        branch_ids, main_ids = failing_tests(_text(again)), failing_tests(_text(on_main)) if on_main else set()
        if on_main is not None and not on_main.ok and branch_ids and branch_ids <= main_ids:
            names = ", ".join(sorted(branch_ids)[:5])
            self.log(f"[{task.id}] {script} fails on main too ({names}): not this task's, not sent back")
            self._file_test_hygiene(task, f"{script} fails on main too ({names}): seen while reviewing {task.id}",
                                    on_main)
            return again, (f"Note from the harness: {script} fails on current main as well ({names}); that failure "
                           "is not this task's and is filed for the orchestrator. Review the change itself."), None
        main_line = ""
        if on_main is not None:
            main_line = (" It passes on current main, so this branch causes the failure." if on_main.ok else
                         " It also fails on current main, but with different tests; fix the ones this branch breaks.")
        return again, "", Verdict("request_changes", f"{script} failed twice", [{
            "severity": "high", "file": script,
            "issue": verify_feedback(_text(again), code=again.code,
                                     intro=f"{script} failed (exit {again.code}) on two runs.{main_line}"),
            "fix": "make the full verify pass (the output above is the failing step's own)"}])

    def _verify_full_on_main(self, task: Task, command: str | None = None) -> CmdResult | None:
        try:
            wt = self.ws.provision_detached(f"_main-verify-{task.id}")   # two reviews may check main at once
        except RuntimeError as e:
            self.log(f"[{task.id}] could not check verify_full on main: {e}")
            return None
        try:
            self.ws.run_script(wt, self.cfg.verify.setup_worktree, 600, slot=False)
            return self.ws.run_script(wt, command or self.cfg.verify.full, 1800)
        finally:
            self.ws.dispose(wt)

    def _file_test_hygiene(self, task: Task, text: str, result: CmdResult) -> None:
        """One open note per (failure kind, failing tests): the part of the text before the first ': '."""
        text = f"[test-hygiene] {text}"
        try:
            if any(q.status == "Open" and q.text.split(": ")[0] == text.split(": ")[0]
                   for q in self.board.list_questions(status="Open")):
                return
            self.board.create_question(Question(
                id="", text=text[:QUESTION_TEXT_CAP], kind="harness",
                # head and tail: the failing step of a script is usually its last line, and a flat [:1900] cut it
                # off (Q-244: the note showed verify_fast's PASS and none of the step that failed)
                context=clip_middle(verify_feedback(_text(result), code=result.code, intro="verify_full output:"),
                                    1850),   # + the "[… cut …]" marker stays under Notion's 1900
                options=["one task owns the fix", "mark the test slow/serial", "ignore"],
                proceeding_with="the task was not sent back for it", impact="medium", task_id=task.id,
                asked_by="reviewer"))
        except Exception as e:   # noqa: BLE001 - a note must never fail a review
            self.log(f"[{task.id}] could not file the test-hygiene note: {e!r}")

    def apply(self, task: Task, v: Verdict) -> Task:
        round_no = task.review_rounds + 1
        if v.verdict == "defer":          # leave it in Review; serve picks it up on a later tick
            with self._lock:
                now, last = self.now(), self._defer_logged.get(task.id + v.summary)
                fresh = last is None or now - last > timedelta(minutes=10)   # not two lines per task every 30 s
                if fresh:
                    self._defer_logged[task.id + v.summary] = now
            if fresh:
                self.log(f"[{task.id}] review deferred: {v.summary[:160]}")
            return task
        md = f"**Verdict:** `{v.verdict}`\n\n{v.summary}\n\n" + "\n".join(
            f"- [{f.get('severity', '?')}] {f.get('file', '')}:{f.get('line', '')} {f.get('issue', '')} → {f.get('fix', '')}"
            for f in v.findings)
        self.board.append_task_report(task, f"Review — round {round_no}", md)
        if v.verdict == "request_changes" and round_no > self.cfg.max_review_rounds:
            v = Verdict("escalate", f"{round_no - 1} review rounds exhausted: {v.summary}", v.findings)
        if v.verdict == "approve":
            task.status, task.feedback = Status.MERGE_READY, ""
        elif v.verdict == "request_changes":
            task.status, task.review_rounds = Status.CHANGES_REQUESTED, round_no
            task.feedback = findings_to_feedback(v)
            task.flags = list(dict.fromkeys(task.flags + ["resume"]))
        else:
            task.status = Status.BLOCKED
            self.board.create_question(Question(
                id="", text=f"Reviewer escalated {task.id}: {v.summary}"[:QUESTION_TEXT_CAP], kind="blocking",
                context=findings_to_feedback(v), options=["cut", "split", "accept as is", "human fix"],
                impact="high", task_id=task.id,
                asked_by=self.last_agent or (self.cfg.reviewer.agent if self.cfg.reviewer else "reviewer")))
        # the review took minutes: rebase the flags on the board's current list (a host pin set meanwhile stays)
        task.flags = fresh_flags(self.board, task, add=["resume"] if v.verdict == "request_changes" else ())
        self.board.update_task(task, ["status", "feedback", "review_rounds", "flags"])
        self.log(f"[{task.id}] review → {v.verdict}")
        return task

    def process(self, task: Task) -> Task:
        self.last_agent = None   # this thread's review sets it; never another task's agent (reviewer.parallel)
        return self.apply(task, self.review(task))
