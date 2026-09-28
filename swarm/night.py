"""Unattended improvement loop: `swarm night`.

Each cycle adds one small app as a new milestone, lets the swarm build it (fresh CLI sessions per task, every
agent on every laptop that is up), answers blocking questions itself, waits for serve to run the retro, then
hands the cycle's log to a fresh Claude session that makes ONE improvement to the harness with tests. The
improvement lands on main only when the whole test suite is green; the loops are restarted on the new code.
"""
from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .models import Status, Task

HARNESS_ROOT = Path(__file__).resolve().parent.parent

PLANS = [
    ("Notecard", "apps/notecard", "a notes app: stdlib JSON API (create/list/delete notes, in memory), one HTML page "
                                  "with inline CSS/JS that lists and adds notes, a CLI, unit tests, a README section"),
    ("Quizlet-mini", "apps/quiz", "a quiz app: quiz.json with 8 questions, stdlib API (next question, submit answer, "
                                  "score), one HTML page that plays the quiz, a CLI that prints the score table, tests"),
    ("Kanban-mini", "apps/kanban", "a kanban board: stdlib API (columns todo/doing/done, move card), one HTML page "
                                   "with Move buttons, a CLI to add/move cards, tests"),
    ("Pomodoro-log", "apps/pomodoro", "a pomodoro logger: stdlib API (start/stop sessions, daily totals), one HTML "
                                      "page with a timer and today's totals, a CLI that prints the week, tests"),
]


def milestone_name(cycle: int) -> str:
    return f"N{cycle}"


def plan_for(cycle: int) -> tuple[str, str, str]:
    """Plans rotate; a repeat gets its own folder so it never builds on top of an earlier cycle's code."""
    name, folder, brief = PLANS[(cycle - 1) % len(PLANS)]
    round_ = (cycle - 1) // len(PLANS) + 1
    return (name, folder if round_ == 1 else f"{folder}{round_}", brief)


def milestone_section(cycle: int) -> str:
    name, folder, brief = plan_for(cycle)
    ms = milestone_name(cycle)
    return f'''
### {ms} tasks · {name} ({folder}/)
Build {brief}. Everything lives under `{folder}/` (backend package `{folder.replace("/", ".")}.api` etc.); tests under `{folder}/tests/`; the page is `{folder}/index.html` (single file, inline CSS and JS, relative API URLs). Python 3.11 stdlib only, no npm.
1. **API and store** (backend, high, M). Data model + stdlib `http.server` JSON API with CORS, `python -m {folder.replace("/", ".")} serve --port <free port>`, unit tests. Scope: `{folder}/**`.
2. **Page** (frontend, high, M, depends on 1). `{folder}/index.html` that uses the API; verified in the headless browser. Scope: `{folder}/index.html`, `{folder}/screenshots/**`.
3. **CLI** (backend, normal, S, depends on 1). `python -m {folder.replace("/", ".")} <verb>` over urllib; tests mock HTTP. Scope: `{folder}/**`.
4. **End-to-end test** (tests, normal, S, depends on 1 and 2). Starts the API in a thread and drives the main flow. Scope: `{folder}/tests/**`.
5. **Docs** (docs, low, S, depends on 2 and 3). README section for {name}: three commands, what you see. Scope: `README.md`, `docs/**`.
'''


def append_milestone(plan_md: str, cycle: int) -> str:
    ms = milestone_name(cycle)
    if f"### {ms} tasks" in plan_md:
        return plan_md
    name, folder, _ = plan_for(cycle)
    line = f"- {ms} night cycle {cycle}: {name} in {folder}/ (five tasks, independent of earlier milestones)\n"
    if "## Milestones\n" in plan_md:
        head, _, rest = plan_md.partition("## Milestones\n")
        block_end = rest.find("\n## ")
        bullets, tail = (rest, "") if block_end < 0 else (rest[:block_end], rest[block_end:])
        plan_md = head + "## Milestones\n" + bullets.rstrip("\n") + "\n" + line + tail
    else:
        plan_md = plan_md.rstrip("\n") + "\n\n## Milestones\n" + line
    return plan_md.rstrip("\n") + "\n" + milestone_section(cycle)


def cycle_complete(tasks: list[Task], milestone: str) -> bool:
    mine = [t for t in tasks if t.milestone == milestone]
    return bool(mine) and all(t.status in (Status.DONE, Status.CUT) for t in mine)


def needs_planning(tasks: list[Task], milestone: str) -> bool:
    """A restarted night loop must not plan a milestone that already has tasks on the board."""
    return not any(t.milestone == milestone for t in tasks)


def decide_merge(tests_ok: bool, commits_ahead: int) -> bool:
    return tests_ok and commits_ahead > 0


FOCUS = ["bugs and wasted runs in the harness (runner, serve, merger, adapters)",
         "the Notion board from a human's point of view: what a person needs to see or do faster (init layout, "
         "views, fields, status page text) and the CLI commands they use",
         "prompt quality: worker rules, planner instructions, reviewer instructions, so agents need fewer attempts",
         "token efficiency: base context per run, review policy, retry ladder, sizing"]


def improve_prompt(cycle: int, cycle_log: str, focus: str) -> str:
    return f'''# Night cycle {cycle}: improve swarm-control

You maintain swarm-control, a Python harness (see README.md, docs/superpowers/specs, docs/rehearsals) that runs
AI coding CLIs against a Notion task board. A test cycle just finished; its log is below.
Your job: make ONE improvement to the harness that this evidence justifies.
Suggested focus for this cycle: {focus}. If the log shows a clear bug or waste elsewhere, fix that instead.

Rules
- Read the log, then the relevant code. Decide quickly; do not survey everything.
- Test first: add or change a pytest test that fails, then make it pass. Keep the change small and self-contained.
- Run `.venv/bin/pytest -q` before you finish; it must be green. Do not weaken or delete existing tests to get there.
- Never touch .venv, ~/.swarm, secrets, template/.swarm/notion.yaml, or the tryout project.
- Commit on the current branch with the message `night {cycle}: <what and why>` (git user is configured).
- Append one paragraph to docs/night/CHANGELOG.md: the finding, the change, the test.
- Final answer: a JSON object {{"summary": "...", "files": [...], "tests_passed": true|false}} and nothing else.

## Cycle log

{cycle_log}
'''


@dataclass
class CycleReport:
    cycle: int
    milestone: str
    done: int = 0
    total: int = 0
    cost: float = 0.0
    questions_answered: int = 0
    improvement: str = ""
    merged: bool = False
    notes: list[str] = field(default_factory=list)
    started: datetime | None = None


class Night:
    def __init__(self, project_dir: Path, *, log=print, cycles: int = 6, minutes: int = 75,
                 env: dict | None = None, claude_bin: str = "claude", improve: bool = True):
        self.project_dir = Path(project_dir)
        self.log, self.cycles, self.minutes = log, cycles, minutes
        self.improve_enabled = improve   # False: build + retro only; a person reviews the cycle logs and improves
        self.env = {**os.environ, **(env or {})}
        self.claude_bin = claude_bin
        self.night_dir = Path.home() / ".swarm" / "night" / datetime.now(timezone.utc).strftime("%Y-%m-%d")
        self.night_dir.mkdir(parents=True, exist_ok=True)
        self.procs: dict[str, subprocess.Popen] = {}

    # ----- helpers -----
    def sh(self, args: list[str], cwd: Path | None = None, timeout: int = 900, check: bool = False) -> subprocess.CompletedProcess:
        r = subprocess.run(args, cwd=str(cwd or self.project_dir), capture_output=True, text=True, timeout=timeout, env=self.env)
        if check and r.returncode != 0:
            raise RuntimeError(f"{' '.join(args[:3])} failed: {(r.stderr or r.stdout)[-300:]}")
        return r

    def swarm(self, *args: str, timeout: int = 900) -> subprocess.CompletedProcess:
        return self.sh(["swarm", *args], timeout=timeout)

    def board(self):
        from .cli import _cfg, make_board, state
        from .config import load_config
        state.cfg = load_config(self.project_dir / ".swarm" / "config.yaml")
        return state.cfg, make_board(state.cfg, False)

    # ----- loops -----
    def start_loops(self) -> None:
        for name in ("serve", "run"):
            if name in self.procs and self.procs[name].poll() is None:
                continue
            logf = open(self.night_dir / f"{name}.log", "ab")
            self.procs[name] = subprocess.Popen(["caffeinate", "-dims", "swarm", name], cwd=str(self.project_dir),
                                                stdout=logf, stderr=subprocess.STDOUT, env=self.env, start_new_session=True)
            self.log(f"night: started swarm {name} (pid {self.procs[name].pid})")

    def stop_loops(self, graceful_s: int = 240) -> None:
        run = self.procs.get("run")
        if run and run.poll() is None:
            os.killpg(run.pid, signal.SIGTERM)   # finishes in-flight runs, parks them, exits
            for _ in range(graceful_s // 5):
                if run.poll() is not None:
                    break
                time.sleep(5)
            if run.poll() is None:
                os.killpg(run.pid, signal.SIGKILL)
        serve = self.procs.get("serve")
        if serve and serve.poll() is None:
            os.killpg(serve.pid, signal.SIGKILL)
        self.procs.clear()
        self.log("night: loops stopped")

    # ----- one cycle -----
    def add_milestone(self, cycle: int) -> None:
        self.sh(["git", "pull", "-q", "--rebase", "origin", "main"])
        plan = self.project_dir / "PLAN.md"
        plan.write_text(append_milestone(plan.read_text(), cycle))
        self.sh(["git", "add", "PLAN.md"]); self.sh(["git", "commit", "-qm", f"PLAN.md: night cycle {cycle} ({plan_for(cycle)[0]})"])
        self.sh(["git", "pull", "-q", "--rebase", "origin", "main"]); self.sh(["git", "push", "-q", "origin", "main"])
        if not needs_planning(self.board()[1].list_tasks(), milestone_name(cycle)):
            self.log(f"night: {milestone_name(cycle)} already planned; resuming it")
            return
        r = self.swarm("plan", "PLAN.md", "--milestone", milestone_name(cycle), "--apply", timeout=1200)
        self.log(f"night: planned {milestone_name(cycle)}: " + (r.stdout.strip().splitlines()[-1] if r.stdout.strip() else r.stderr[-200:]))

    def answer_questions(self, report: CycleReport) -> None:
        cfg, board = self.board()
        for q in board.list_questions(status="Open"):
            if q.kind != "blocking":
                continue
            task = board.get_task(q.task_id) if q.task_id else None
            if re.search(r"failed \d+ times", q.text):
                answer = "retry once more" if "retry" not in (task.feedback if task else "") else "cut"
            else:
                answer = self.orchestrate_answer(q, task)
            if answer:
                self.swarm("answer", q.id, answer)
                report.questions_answered += 1
                self.log(f"night: answered {q.id}: {answer[:100]}")

    def orchestrate_answer(self, q, task) -> str:
        prompt = (f"You are the orchestrator of a hackathon codebase. An autonomous worker asked a blocking question.\n"
                  f"Task {q.task_id}: {task.title if task else ''}\n{(task.description if task else '')[:1200]}\n\n"
                  f"Question: {q.text}\nOptions: {q.options}\nContext: {q.context[:800]}\n\n"
                  "Reply with ONLY the answer the worker should act on, in one to three plain sentences: pick an option "
                  "or give the concrete decision. Prefer the simplest choice that keeps the demo working.")
        pf = self.night_dir / f"answer-{q.id}.md"
        pf.write_text(prompt)
        r = subprocess.run([self.claude_bin, "-p", prompt, "--output-format", "json", "--model", "sonnet", "--max-turns", "4",
                            "--max-budget-usd", "1", "--permission-mode", "dontAsk", "--allowedTools", "Read,Grep,Glob",
                            "--mcp-config", '{"mcpServers":{}}', "--strict-mcp-config"],
                           cwd=str(self.project_dir), capture_output=True, text=True, timeout=300, env=self.env)
        try:
            return str(json.loads(r.stdout).get("result") or "").strip()[:900] or "Proceed with the first option."
        except ValueError:
            return "Proceed with the first option."

    def wait_for_cycle(self, cycle: int, report: CycleReport) -> None:
        ms = milestone_name(cycle)
        deadline = time.monotonic() + self.minutes * 60
        while time.monotonic() < deadline:
            cfg, board = self.board()
            tasks = board.list_tasks()
            mine = [t for t in tasks if t.milestone == ms]
            report.total = len(mine)
            report.done = sum(1 for t in mine if t.status is Status.DONE)
            if cycle_complete(tasks, ms):
                return
            self.answer_questions(report)
            time.sleep(60)
        report.notes.append(f"time cap {self.minutes} min reached with {report.done}/{report.total} done")
        for t in [t for t in self.board()[1].list_tasks() if t.milestone == ms and t.status not in (Status.DONE, Status.CUT)]:
            self.swarm("cut", t.id)
            report.notes.append(f"cut {t.id} ({t.status})")

    def cycle_log(self, cycle: int, report: CycleReport) -> str:
        since = (report.started or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M")
        usage = self.swarm("usage", "--since", since).stdout   # this cycle only, not a fixed window
        cfg, board = self.board()
        rows = []
        for t in board.list_tasks():
            if t.milestone == milestone_name(cycle):
                rows.append(f"- {t.id} {t.type}/{t.importance}/{t.size} {str(t.status).split('.')[-1]} agent={t.agent} model={t.model} "
                            f"attempts={t.attempts} reviews={t.review_rounds} flags={t.flags} err={(t.last_error or '')[:80]}")
        qs = [f"- {q.id} ({q.task_id}, {q.kind}): {q.text[:160]} → {q.answer[:120]}" for q in board.list_questions()
              if q.task_id in {r.split()[1] for r in rows}]
        tails = []
        for name in ("serve", "run"):
            p = self.night_dir / f"{name}.log"
            if p.exists():
                tails.append(f"### {name} log (tail)\n```\n" + "\n".join(p.read_text(errors='replace').splitlines()[-80:]) + "\n```")
        self.sh(["git", "pull", "-q", "--rebase", "origin", "main"])   # the retro commits to main, not this checkout
        retro = sorted((self.project_dir / "docs" / "retro").glob("*.md"))
        retro_text = retro[-1].read_text() if retro else "(no retro yet)"
        return (f"## Cycle {cycle} · {milestone_name(cycle)} · {plan_for(cycle)[0]}\n\n{report.done}/{report.total} tasks done; "
                f"questions answered: {report.questions_answered}; notes: {report.notes}\n\n### Tasks\n" + "\n".join(rows)
                + "\n\n### Questions\n" + ("\n".join(qs) or "- none") + "\n\n### Usage\n```\n" + usage + "\n```\n\n### Retro\n"
                + retro_text + "\n\n" + "\n\n".join(tails) + "\n")

    def improve(self, cycle: int, log_text: str, report: CycleReport) -> None:
        focus = FOCUS[(cycle - 1) % len(FOCUS)]
        branch = f"night/{cycle}"
        h = HARNESS_ROOT
        self.sh(["git", "checkout", "-q", "main"], cwd=h); self.sh(["git", "pull", "-q", "--ff-only", "origin", "main"], cwd=h)
        self.sh(["git", "checkout", "-q", "-B", branch, "main"], cwd=h)
        (h / "docs" / "night").mkdir(exist_ok=True)
        log_path = h / "docs" / "night" / f"cycle-{datetime.now(timezone.utc).strftime('%Y-%m-%d')}-{cycle}.md"
        log_path.write_text(log_text)
        prompt = improve_prompt(cycle, log_text[:60000], focus)
        r = subprocess.run([self.claude_bin, "-p", prompt, "--output-format", "json", "--model", "opus", "--effort", "high",
                            "--max-turns", "70", "--max-budget-usd", "10", "--permission-mode", "bypassPermissions",
                            "--mcp-config", '{"mcpServers":{}}', "--strict-mcp-config"],
                           cwd=str(h), capture_output=True, text=True, timeout=3600, env=self.env)
        try:
            data = json.loads(r.stdout)
            result = json.loads(re.search(r"\{.*\}", str(data.get("result") or ""), re.S).group(0)) if data.get("result") else {}
        except (ValueError, AttributeError):
            result = {}
        summary = str(result.get("summary") or (r.stderr or "")[-200:] or "no summary")
        tests = self.sh([".venv/bin/pytest", "-q"], cwd=h, timeout=1200)
        ahead = self.sh(["git", "rev-list", "--count", f"main..{branch}"], cwd=h).stdout.strip()
        commits = int(ahead) if ahead.isdigit() else 0
        self.sh(["git", "add", "-A"], cwd=h)
        if self.sh(["git", "diff", "--cached", "--quiet"], cwd=h).returncode != 0:
            self.sh(["git", "commit", "-qm", f"night {cycle}: cycle log"], cwd=h)
            commits += 1
        merged = decide_merge(tests.returncode == 0, commits)
        self.sh(["git", "checkout", "-q", "main"], cwd=h)
        if merged:
            self.sh(["git", "merge", "-q", "--no-ff", "-m", f"night {cycle}: {summary[:60]}", branch], cwd=h)
            self.sh(["git", "push", "-q", "origin", "main"], cwd=h)
            self.sh(["git", "branch", "-q", "-D", branch], cwd=h)
        report.improvement, report.merged = summary, merged
        self.log(f"night: improvement {'merged' if merged else 'REJECTED'} (tests {'green' if tests.returncode == 0 else 'red'}, "
                 f"{commits} commits): {summary[:140]}")

    def record(self, report: CycleReport) -> None:
        p = HARNESS_ROOT / "docs" / "night" / f"{datetime.now(timezone.utc).strftime('%Y-%m-%d')}.md"
        p.parent.mkdir(exist_ok=True)
        head = "" if p.exists() else "# Night log\n\nOne line per unattended cycle: what was built, what it cost, what the harness learned.\n\n"
        line = (f"- cycle {report.cycle} ({report.milestone}, {plan_for(report.cycle)[0]}): {report.done}/{report.total} done · "
                f"{report.questions_answered} questions answered · "
                + (f"improvement {'merged' if report.merged else 'rejected'}: " if self.improve_enabled else "")
                + f"{report.improvement[:160]}" + (f" · notes: {'; '.join(report.notes)}" if report.notes else "") + "\n")
        p.write_text((p.read_text() if p.exists() else head) + line)
        self.sh(["git", "add", "docs/night"], cwd=HARNESS_ROOT); self.sh(["git", "commit", "-qm", f"night: cycle {report.cycle} summary"], cwd=HARNESS_ROOT)
        self.sh(["git", "push", "-q", "origin", "main"], cwd=HARNESS_ROOT)

    def run(self, start_cycle: int = 1) -> list[CycleReport]:
        reports = []
        self.start_loops()
        for cycle in range(start_cycle, start_cycle + self.cycles):
            report = CycleReport(cycle=cycle, milestone=milestone_name(cycle), started=datetime.now(timezone.utc))
            self.log(f"night: cycle {cycle} start ({plan_for(cycle)[0]})")
            try:
                self.add_milestone(cycle)
                self.wait_for_cycle(cycle, report)
                time.sleep(90)   # let serve run the milestone retro
                log_text = self.cycle_log(cycle, report)
                log_path = HARNESS_ROOT / "docs" / "night" / f"cycle-{datetime.now(timezone.utc).strftime('%Y-%m-%d')}-{cycle}.md"
                log_path.parent.mkdir(exist_ok=True)
                log_path.write_text(log_text)
                if self.improve_enabled:
                    self.stop_loops()
                    self.improve(cycle, log_text, report)
                else:
                    report.improvement = f"log written to {log_path.relative_to(HARNESS_ROOT)} for review"
            except Exception as e:   # one bad cycle must not end the night
                report.notes.append(f"cycle error: {str(e)[:200]}")
                self.log(f"night: cycle {cycle} error: {e}")
            self.record(report)
            reports.append(report)
            self.log(f"night: cycle {cycle} done: {report.done}/{report.total} · improvement {'merged' if report.merged else 'not merged'}")
            if self.improve_enabled:
                self.sh(["pip", "install", "-q", "-e", "."], cwd=HARNESS_ROOT)
                self.start_loops()
        self.stop_loops()
        return reports
