from swarm.models import Status, Task
from swarm.night import FOCUS, PLANS, append_milestone, cycle_complete, decide_merge, improve_prompt, milestone_name, plan_for


def test_plans_rotate_and_milestones_are_named():
    assert milestone_name(3) == "N3"
    assert plan_for(1)[0] == PLANS[0][0] and plan_for(len(PLANS) + 2)[0] == PLANS[1][0]


def test_append_milestone_adds_a_bullet_and_a_task_section_once():
    plan = "# P\n\n## Summary\nx\n\n## Milestones\n- M1 slice\n\n## Constraints\n- none\n"
    out = append_milestone(plan, 2)
    assert "- N2 night cycle 2: Quizlet-mini in apps/quiz/" in out.split("## Constraints")[0]
    assert "### N2 tasks · Quizlet-mini (apps/quiz/)" in out and out.count("### N2 tasks") == 1
    assert "python -m apps.quiz serve" in out
    assert append_milestone(out, 2) == out            # idempotent


def test_cycle_complete_and_merge_decision():
    tasks = [Task(id="T-1", title="a", milestone="N1", status=Status.DONE), Task(id="T-2", title="b", milestone="N1", status=Status.CUT),
             Task(id="T-3", title="c", milestone="N2", status=Status.RUNNING)]
    assert cycle_complete(tasks, "N1") and not cycle_complete(tasks, "N2") and not cycle_complete(tasks, "N9")
    assert decide_merge(True, 2) and not decide_merge(False, 2) and not decide_merge(True, 0)


def test_improve_prompt_carries_the_rules_and_the_log():
    p = improve_prompt(3, "## Cycle 3 log", FOCUS[2])
    assert "ONE improvement" in p and "Test first" in p and "night 3:" in p and "## Cycle 3 log" in p
    assert "tests_passed" in p and "Never touch .venv" in p


def test_night_can_run_without_the_improvement_agent(tmp_path):
    from swarm.night import Night
    n = Night(tmp_path, cycles=1, minutes=1, improve=False)
    assert n.improve_enabled is False


def test_restart_does_not_plan_a_milestone_twice():
    from swarm.night import needs_planning
    tasks = [Task(id="T-31", title="a", milestone="N3", status=Status.READY)]
    assert needs_planning(tasks, "N3") is False and needs_planning(tasks, "N4") is True


def test_repeat_cycles_build_in_a_fresh_folder():
    """Cycle 5 would rebuild Notecard in apps/notecard, which cycle 1 already filled."""
    first, repeat = plan_for(1), plan_for(len(PLANS) + 1)
    assert first[0] == repeat[0] and first[1] == "apps/notecard" and repeat[1] == "apps/notecard2"
    assert "python -m apps.notecard2 serve" in append_milestone("# P\n", len(PLANS) + 1)


def test_cycle_report_records_its_start_for_usage_scoping():
    from swarm.night import CycleReport
    from datetime import datetime, timezone
    r = CycleReport(cycle=1, milestone="N1", started=datetime(2026, 9, 28, 7, 5, tzinfo=timezone.utc))
    assert r.started.strftime("%Y-%m-%dT%H:%M") == "2026-09-28T07:05"


def test_planner_prompt_forbids_uncheckable_reload_criteria():
    from swarm.prompt import PROMPTS_DIR
    assert "dead API URL" in (PROMPTS_DIR / "planner.md").read_text()
