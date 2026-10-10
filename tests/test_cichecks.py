import json

from swarm.cichecks import BILLING_NOTE, checks_note, never_started
from swarm.models import Task
from swarm.reviewer import build_review_prompt
from swarm.runner import HARNESS_PATHS, SHOTS_DIR
from swarm.feedback import out_of_scope_edits
from swarm.workspace import CmdResult

BILLING_MSG = ("The job was not started because recent account payments have failed or your spending limit needs "
               "to be increased. Please check the 'Billing & plans' section in your settings")
RUN = {"__typename": "CheckRun", "name": "fast", "conclusion": "FAILURE", "status": "COMPLETED",
       "startedAt": "2026-10-10T21:39:26Z", "completedAt": "2026-10-10T21:39:28Z",
       "detailsUrl": "https://github.com/o/r/actions/runs/1/job/114319787635", "workflowName": "verify"}


def fake_gh(rollup, annotations=(), steps=()):
    calls = []

    def gh(args, cwd):
        calls.append(args)
        if args[:2] == ["pr", "view"]:
            return CmdResult(0, json.dumps({"statusCheckRollup": rollup}), "")
        if args[0] == "api" and args[1].endswith("/annotations"):
            return CmdResult(0, json.dumps([{"message": m} for m in annotations]), "")
        if args[0] == "api" and "/actions/jobs/" in args[1]:
            return CmdResult(0, json.dumps({"steps": list(steps)}), "")
        return CmdResult(1, "", "unexpected")
    gh.calls = calls
    return gh


def test_billing_refusal_is_reported_as_unavailable_infrastructure():
    gh = fake_gh([RUN], annotations=[BILLING_MSG, "ubuntu-latest label will migrate"])
    note = checks_note(gh, ".", "task/T-1")
    assert BILLING_NOTE in note and "fast" in note and "fast: failure" not in note
    assert any("check-runs/114319787635/annotations" in a[1] for a in gh.calls if a[0] == "api")


def test_zero_steps_two_seconds_counts_as_not_started_without_annotation():
    assert never_started(RUN, [], [])
    assert not never_started(RUN, [], None)   # steps unknown: no claim
    assert not never_started({**RUN, "completedAt": "2026-10-10T21:45:00Z"}, [], [])


def test_real_failure_stays_a_failure():
    gh = fake_gh([{**RUN, "completedAt": "2026-10-10T21:44:00Z"}], annotations=["Process completed with exit code 1."],
                 steps=[{"name": "pytest", "conclusion": "failure"}])
    note = checks_note(gh, ".", "task/T-1")
    assert note == "- fast: failure"


def test_gh_errors_and_no_checks_give_an_empty_note():
    assert checks_note(lambda a, c: CmdResult(1, "", "no pr"), ".", "task/T-1") == ""
    assert checks_note(fake_gh([]), ".", "task/T-1") == ""

    def boom(a, c):
        raise RuntimeError("gh missing")
    assert checks_note(boom, ".", "task/T-1") == ""


def test_review_prompt_carries_the_checks_section_only_when_given():
    t = Task(id="T-1", title="x")
    with_note = build_review_prompt(t, "", "ok", "instr", checks_note=f"- fast: {BILLING_NOTE}")
    assert "## GitHub checks on the PR" in with_note and "never a reason to request changes" in with_note
    assert "## GitHub checks" not in build_review_prompt(t, "", "ok", "instr")


def test_reviewer_and_worker_prompts_name_the_ci_and_screenshot_rules():
    from swarm.prompt import PROMPTS_DIR
    reviewer = (PROMPTS_DIR / "reviewer.md").read_text()
    rules = (PROMPTS_DIR / "rules.md").read_text()
    assert "never a reason to request changes" in reviewer and "web/app/tests/shots/" in reviewer
    assert "web/app/tests/shots/<task id>/" in rules and "200 KB" in rules


def test_committed_shots_are_not_out_of_scope_edits():
    changed = ["web/app/tests/shots/T-1/listen.jpg", "hearing/x.py"]
    exempt = HARNESS_PATHS + (SHOTS_DIR.format(task="T-1"),)
    assert out_of_scope_edits(changed, ["web/app/src/**"], exempt=exempt) == ["hearing/x.py"]
