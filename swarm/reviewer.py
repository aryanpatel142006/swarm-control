"""Independent reviewer: deterministic verify first, then a read-only model pass from a different family."""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from .adapters import get_adapter
from .adapters.base import RunSpec
from .board.base import Board
from .config import Config
from .feedback import verify_feedback
from .models import QUESTION_TEXT_CAP, Question, Status, Task
from .prompt import PROMPTS_DIR
from .report import REVIEW_SCHEMA
from .runner import STRUCTURED_PROVIDERS
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
    if data is None or data.get("verdict") not in ("approve", "request_changes", "escalate"):
        return Verdict("escalate", "reviewer produced no usable verdict", [])
    return Verdict(data["verdict"], str(data.get("summary") or ""),
                   [f for f in (data.get("findings") or []) if isinstance(f, dict)])


def build_review_prompt(task: Task, diff: str, verify_tail: str, instructions: str) -> str:
    parts = [f"# Review of {task.id} · {task.title}", "", instructions.strip(), "", "## Task", "",
             f"- Type: {task.type} · Importance: {task.importance} · Scope: {', '.join(task.scope) or 'any'}",
             f"- Flags from the harness: {', '.join(task.flags) or 'none'}", "", "### Description", "",
             task.description.strip() or "(none)", "", "### Acceptance criteria", "",
             task.acceptance.strip() or "(none given)", "", "## Verify output (full suite)", "", "```",
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
                 prompt_text: str | None = None, ledger=None):
        self.cfg, self.board, self.ws, self.adapter_factory, self.log = cfg, board, ws, adapter_factory, log
        self.prompt_text = prompt_text if prompt_text is not None else (PROMPTS_DIR / "reviewer.md").read_text()
        self.ledger = ledger

    def review(self, task: Task) -> Verdict:
        wt = self.ws.provision(task.id, reuse_branch=True)
        try:
            setup = self.ws.run_script(wt, self.cfg.verify.setup_worktree, 600, slot=False)
            if setup is not None and not setup.ok:
                return Verdict("escalate", "setup_worktree.sh failed (environment, not code)",
                               [{"severity": "high", "file": self.cfg.verify.setup_worktree or "",
                                 "issue": setup.tail(1200), "fix": "orchestrator: fix the environment, then re-review"}])
            full = self.ws.run_script(wt, self.cfg.verify.full, 1800)
            verify_note = ""
            if full is not None and not full.ok:
                full, verify_note, verdict = self._triage_full_failure(task, wt, full)
                if verdict is not None:
                    return verdict
            role = self.cfg.reviewer
            if role is None:
                return Verdict("approve", "no reviewer configured; verify passed", [])
            agent_cfg = self.cfg.agents[role.agent]
            diff = self.ws.git(wt, "diff", f"{self.ws.remote}/{self.cfg.main_branch}...HEAD", check=False).out
            tail = full.tail(1500) if full else ""
            prompt = build_review_prompt(task, diff, (verify_note + "\n\n" + tail).strip() if verify_note else tail,
                                         self.prompt_text)
            pf = wt / ".swarm-run" / "review_prompt.md"
            pf.write_text(prompt)
            structured = agent_cfg.provider in STRUCTURED_PROVIDERS
            spec = RunSpec(prompt_file=pf, model=role.model or agent_cfg.models["mid"], effort=role.effort,
                           max_turns=REVIEW_TURNS, budget_usd=None, timeout_s=REVIEW_TIMEOUT_S, cwd=wt,
                           schema=REVIEW_SCHEMA if structured else None, read_only=True,
                           sandbox="read-only", extra_args=list(agent_cfg.extra_args))
            started = time.time()
            result = self.adapter_factory(agent_cfg).run(spec)
            if self.ledger is not None:
                self.ledger.append(agent=role.agent, model=spec.model, task_id=task.id, usage=result.usage, role="reviewer",
                                   duration_s=time.time() - started, ok=result.ok)
            if not result.ok and result.structured_output is None:
                if result.rate_limited:   # the reviewer's provider is out of quota, not the code: try again later
                    return Verdict("defer", f"reviewer rate limited: {result.error[:200]}", [])
                return Verdict("escalate", f"reviewer run failed: {result.error[:300]}", [])
            return parse_verdict(result.structured_output, wt)
        finally:
            self.ws.dispose(wt)

    # ----- verify_full failures that are not the task's (Q-189, Q-190, Q-191, Q-196) -----
    def _triage_full_failure(self, task: Task, wt: Path, first: CmdResult):
        """verify_full failed on the branch. Run it once more (the slot lock means less load now); if it passes the
        failure was flaky. If it fails again, run it on current main: a failure main has too (same failing tests) is
        not this task's to fix. Either way the task goes on to the model review, and one test-hygiene note goes to
        the orchestrator so ONE task fixes the test (Q-196: two branches patched the same flaky Kokoro test and
        conflicted). Returns (result to show the reviewer, note for the reviewer, verdict or None to continue)."""
        script = self.cfg.verify.full or "verify_full"
        second = self.ws.run_script(wt, self.cfg.verify.full, 1800)
        if second is not None and second.ok:
            self.log(f"[{task.id}] {script} failed, then passed on a rerun: flaky, not sent back")
            ids = ", ".join(sorted(failing_tests(_text(first)))[:5]) or "no test id in the output"
            self._file_test_hygiene(task, f"{script} is flaky ({ids}): it failed on {task.id}'s branch and passed "
                                    "on an immediate rerun", first)
            return second, (f"Note from the harness: {script} failed once and passed on a rerun (flaky; filed for "
                            "the orchestrator). Do not ask this task to fix that test."), None
        again = second or first
        on_main = self._verify_full_on_main(task)
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

    def _verify_full_on_main(self, task: Task) -> CmdResult | None:
        try:
            wt = self.ws.provision_detached("_main-verify")
        except RuntimeError as e:
            self.log(f"[{task.id}] could not check verify_full on main: {e}")
            return None
        try:
            self.ws.run_script(wt, self.cfg.verify.setup_worktree, 600, slot=False)
            return self.ws.run_script(wt, self.cfg.verify.full, 1800)
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
                context=verify_feedback(_text(result), code=result.code, intro="verify_full output:")[:1900],
                options=["one task owns the fix", "mark the test slow/serial", "ignore"],
                proceeding_with="the task was not sent back for it", impact="medium", task_id=task.id,
                asked_by="reviewer"))
        except Exception as e:   # noqa: BLE001 - a note must never fail a review
            self.log(f"[{task.id}] could not file the test-hygiene note: {e!r}")

    def apply(self, task: Task, v: Verdict) -> Task:
        round_no = task.review_rounds + 1
        if v.verdict == "defer":          # leave it in Review; serve picks it up on a later tick
            self.log(f"[{task.id}] review deferred: {v.summary[:120]}")
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
                asked_by=self.cfg.reviewer.agent if self.cfg.reviewer else "reviewer"))
        self.board.update_task(task, ["status", "feedback", "review_rounds", "flags"])
        self.log(f"[{task.id}] review → {v.verdict}")
        return task

    def process(self, task: Task) -> Task:
        return self.apply(task, self.review(task))
