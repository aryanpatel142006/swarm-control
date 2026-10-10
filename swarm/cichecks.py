"""GitHub check runs on a task's PR, summarised for the reviewer.

A check run that GitHub never started (failed payments / spending limit: annotation "The job was not started
because recent account payments have failed or your spending limit needs to be increased", zero steps, ~2 s) is
reported as unavailable CI infrastructure, not as a failure: the harness's local verify is the gate. Reviewers had
sent tasks back over the 'fast' job failing that way on every PR (Oct 10).
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Callable

from .workspace import CmdResult

BILLING_NOTE = "CI infrastructure unavailable (GitHub billing); rely on the local verify"
_BILLING_RE = re.compile(r"not (been )?started|payments? (have )?failed|spending limit|billing", re.I)
_JOB_RE = re.compile(r"/job/(\d+)")
NOT_STARTED_MAX_S = 10   # a job GitHub refused completes within seconds, with no steps

Gh = Callable[[list[str], object], CmdResult]


def _seconds(run: dict) -> float | None:
    try:
        a = datetime.fromisoformat(str(run.get("startedAt")).replace("Z", "+00:00"))
        b = datetime.fromisoformat(str(run.get("completedAt")).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return (b - a).total_seconds()


def never_started(run: dict, annotations: list[str], steps: list | None) -> bool:
    """True when the check run failed because GitHub never started it (billing / spending limit)."""
    if any(_BILLING_RE.search(a or "") for a in annotations):
        return True
    secs = _seconds(run)
    return steps is not None and not steps and secs is not None and secs <= NOT_STARTED_MAX_S


def _json(r: CmdResult):
    if not r.ok:
        return None
    try:
        return json.loads(r.out or "null")
    except ValueError:
        return None


def checks_note(gh: Gh, cwd, pr_ref: str) -> str:
    """One line per check run on the PR; '' when there are none or gh cannot tell. Never raises."""
    try:
        data = _json(gh(["pr", "view", pr_ref, "--json", "statusCheckRollup"], cwd)) or {}
        runs = [r for r in data.get("statusCheckRollup") or [] if isinstance(r, dict)]
    except Exception:   # noqa: BLE001 - a missing check summary never blocks a review
        return ""
    lines = []
    for run in runs:
        name = run.get("name") or run.get("context") or "check"
        state = str(run.get("conclusion") or run.get("state") or run.get("status") or "").lower()
        if state in ("failure", "error", "startup_failure", "timed_out", "cancelled"):
            m = _JOB_RE.search(str(run.get("detailsUrl") or run.get("targetUrl") or ""))
            annotations: list[str] = []
            steps = None
            if m:
                try:
                    ann = _json(gh(["api", f"repos/{{owner}}/{{repo}}/check-runs/{m.group(1)}/annotations"], cwd))
                    annotations = [str(a.get("message") or "") for a in ann or [] if isinstance(a, dict)]
                    job = _json(gh(["api", f"repos/{{owner}}/{{repo}}/actions/jobs/{m.group(1)}"], cwd))
                    steps = job.get("steps") if isinstance(job, dict) else None
                except Exception:   # noqa: BLE001
                    pass
            if never_started(run, annotations, steps):
                lines.append(f"- {name}: {BILLING_NOTE}. Not a task failure.")
                continue
            lines.append(f"- {name}: {state}")
        else:
            lines.append(f"- {name}: {state or 'pending'}")
    return "\n".join(lines)
