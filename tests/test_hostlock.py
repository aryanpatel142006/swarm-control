import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from swarm import hostlock
from swarm.config import load_config
from swarm.hostlock import HELD_ENV, verify_slot, worker_lock_env, write_wrapper
from swarm.workspace import Workspace


def test_slots_cap_concurrent_holders(tmp_path):
    d = tmp_path / "locks"
    with verify_slot(d, 2) as a, verify_slot(d, 2) as b:
        assert a and b
        logs = []
        with verify_slot(d, 2, wait_s=0.2, poll_s=0.05, log=logs.append) as c:
            assert c is False                      # both busy: ran out of wait, proceeds without a slot
        assert any("busy" in m for m in logs) and any("without one" in m for m in logs)
    with verify_slot(d, 2, wait_s=0.2, poll_s=0.05) as again:
        assert again is True                       # released on exit


def test_waiter_gets_the_slot_when_it_frees(tmp_path):
    d = tmp_path / "locks"
    got = []

    def waiter():
        with verify_slot(d, 1, wait_s=5, poll_s=0.02) as held:
            got.append((held, time.monotonic()))

    with verify_slot(d, 1):
        th = threading.Thread(target=waiter)
        th.start()
        time.sleep(0.2)
        assert got == []
        released = time.monotonic()
    th.join(5)
    assert got and got[0][0] is True and got[0][1] >= released


def test_exclusive_waits_for_every_slot_and_blocks_others(tmp_path):
    d = tmp_path / "locks"
    with verify_slot(d, 3):
        with verify_slot(d, 3, exclusive=True, wait_s=0.2, poll_s=0.05) as ex:
            assert ex is False                     # one slot is busy: the machine is not free
    with verify_slot(d, 3, exclusive=True) as ex:
        assert ex is True
        with verify_slot(d, 3, wait_s=0.2, poll_s=0.05) as v:
            assert v is False                      # nothing else verifies during a measurement


def test_held_env_runs_straight_through(tmp_path, monkeypatch):
    d = tmp_path / "locks"
    with verify_slot(d, 1):
        monkeypatch.setenv(HELD_ENV, "1")
        with verify_slot(d, 1, wait_s=0.1) as nested:
            assert nested is True                  # a wrapped verify inside a harness verify does not wait on itself


def test_wrapper_runs_the_command_and_keeps_its_pythonpath(tmp_path):
    wrapper = write_wrapper(tmp_path / "bin")
    d = tmp_path / "locks"
    env = {**os.environ, hostlock.DIR_ENV: str(d), hostlock.SLOTS_ENV: "1", "PYTHONPATH": "/the/worktree"}
    env.pop(HELD_ENV, None)
    out = subprocess.run([str(wrapper), "--", sys.executable, "-c",
                          f"import os; print(os.environ['PYTHONPATH'], os.environ.get('{HELD_ENV}'))"],
                         capture_output=True, text=True, env=env, timeout=30)
    assert out.returncode == 0, out.stderr
    assert out.stdout.split() == ["/the/worktree", "1"]
    rc = subprocess.run([str(wrapper), "--", "sh", "-c", "exit 7"], env=env, timeout=30).returncode
    assert rc == 7
    assert (d / "verify-0.lock").exists()


def test_worker_lock_env_puts_swarm_lock_on_path(tmp_path):
    env = worker_lock_env("demo", 3)
    assert env[hostlock.SLOTS_ENV] == "3"
    assert os.access(env[hostlock.CMD_ENV], os.X_OK)
    assert env["PATH"].split(os.pathsep)[0] == os.path.dirname(env[hostlock.CMD_ENV])
    assert sorted(p.name for p in hostlock.lock_dir("demo").iterdir()) == ["verify-0.lock", "verify-1.lock",
                                                                           "verify-2.lock"]


def test_config_reads_max_parallel_verify(project_dir, sample_config_dict):
    import yaml
    sample_config_dict["hosts"]["host-a"]["max_parallel_verify"] = 3
    sample_config_dict["env"] = {"HEARING_FIXTURES_DIR": "{repo_root}/eval/fixtures"}
    (project_dir / ".swarm" / "config.yaml").write_text(yaml.safe_dump(sample_config_dict))
    cfg = load_config(project_dir / ".swarm" / "config.yaml")
    assert cfg.hosts["host-a"].max_parallel_verify == 3 and cfg.hosts["host-b"].max_parallel_verify == 2
    assert cfg.project_env() == {"HEARING_FIXTURES_DIR": f"{project_dir.resolve()}/eval/fixtures"}


def test_run_script_takes_a_slot_for_verify_but_not_setup(git_repo, tmp_path):
    ws = Workspace(git_repo, tmp_path / "wt")
    labels = []

    class Slot:
        def __init__(self, label):
            labels.append(label)

        def __enter__(self):
            return True

        def __exit__(self, *a):
            return False

    ws.verify_slot = Slot
    ws.script_env = {"SHARED_DIR": "/data"}
    (git_repo / "scripts" / "env.sh").write_text(f'echo "$SHARED_DIR ${HELD_ENV}"\n')
    r = ws.run_script(git_repo, "scripts/env.sh", 30)
    assert r.out.split() == ["/data", "1"] and labels == ["repo: scripts/env.sh"]
    r2 = ws.run_script(git_repo, "scripts/env.sh", 30, slot=False)
    assert r2.out.split() == ["/data"] and len(labels) == 1


def test_template_verify_fast_takes_a_slot_and_ends_with_its_verdict(tmp_path):
    import shutil
    from pathlib import Path
    proj = tmp_path / "proj"
    (proj / "scripts").mkdir(parents=True)
    src = Path(hostlock.__file__).parent / "template" / "scripts" / "verify_fast.sh"
    shutil.copy(src, proj / "scripts" / "verify_fast.sh")
    d = tmp_path / "locks"
    env = {**os.environ, hostlock.DIR_ENV: str(d), hostlock.SLOTS_ENV: "1",
           hostlock.CMD_ENV: str(write_wrapper(tmp_path / "bin"))}
    env.pop(HELD_ENV, None)
    out = subprocess.run(["bash", "scripts/verify_fast.sh"], cwd=proj, env=env, capture_output=True, text=True,
                         timeout=60)
    assert out.returncode == 0 and out.stdout.strip().splitlines()[-1] == "verify_fast: OK"
    assert (d / "verify-0.lock").exists()            # it went through swarm-lock


# ----- visible, prioritised, accounted waits (Q-229, Q-232, Q-238, Q-241) -----
def test_new_verifies_queue_behind_a_waiting_measurement(tmp_path):
    d = tmp_path / "locks"
    hostlock.ensure_lock_files(d, 2)
    other = subprocess.Popen(["sleep", "30"])      # stands in for another worktree's `swarm-lock --exclusive`
    try:
        (d / f"{hostlock.PENDING_PREFIX}{other.pid}").write_text(
            '{"pid": %d, "task": "T-102", "label": "measurement: demo_regress", "exclusive": true, "started": %f}'
            % (other.pid, time.time()))
        logs = []
        with verify_slot(d, 2, wait_s=0.2, poll_s=0.05, log=logs.append) as v:
            assert v is False                      # both slots are free, but the measurement goes first
        assert any("measurement is waiting" in m and "T-102" in m for m in logs)
    finally:
        other.kill()
        other.wait()
    with verify_slot(d, 2, wait_s=0.5, poll_s=0.05) as v:
        assert v is True                           # a dead waiter's marker is ignored


def test_waiters_are_told_who_holds_the_slots_and_their_own_measurement(tmp_path, monkeypatch):
    d = tmp_path / "locks"
    monkeypatch.setenv("SWARM_TASK_ID", "T-102")
    with verify_slot(d, 1, exclusive=True, label="measurement: replay"):
        assert (d / "verify-0.holder").exists()
        logs = []
        with verify_slot(d, 1, wait_s=0.2, poll_s=0.05, log=logs.append):
            pass
    assert any("T-102 measurement: replay (exclusive)" in m and "pid" in m for m in logs)
    assert any("Your own task holds the exclusive measurement lock" in m for m in logs)
    assert not (d / "verify-0.holder").exists()    # released holders clean up


def test_a_tasks_wait_is_recorded_for_the_runner(tmp_path):
    d = tmp_path / "locks"
    rec_dir = d / hostlock.WAITS_DIR / "T-7"
    rec_dir.mkdir(parents=True)

    logs = []

    def waiter():
        with verify_slot(d, 1, wait_s=5, poll_s=0.02, record=rec_dir / "w.json", log=logs.append):
            pass

    with verify_slot(d, 1):
        th = threading.Thread(target=waiter)
        th.start()
        time.sleep(0.3)
        assert hostlock.task_wait_seconds(d, "T-7") >= 0.2   # counted while still waiting
    th.join(5)
    assert hostlock.task_wait_seconds(d, "T-7") >= 0.25
    assert any("added back to your run's time limit" in m for m in logs)    # Q-290
    hostlock.clear_task_waits(d, "T-7")
    assert hostlock.task_wait_seconds(d, "T-7") == 0


def test_a_waiter_killed_by_the_shell_tool_still_counts_up_to_its_last_refresh(tmp_path, monkeypatch):
    """Q-249/Q-253: the worker's shell tool killed a verify still queued in swarm-lock after 600 s; its record only
    had `waiting_since` with a dead pid, so none of the wait was added back and T-105 timed out at exactly 1200 s."""
    import json
    d = tmp_path / "locks"
    rec_dir = d / hostlock.WAITS_DIR / "T-9"
    rec_dir.mkdir(parents=True)
    monkeypatch.setattr(hostlock, "WAIT_RECORD_EVERY_S", 0.05)
    script = (f"import sys; sys.path.insert(0, {str(Path(hostlock.__file__).parents[1])!r}); "
              f"from swarm.hostlock import verify_slot; from pathlib import Path\n"
              f"import swarm.hostlock as h; h.WAIT_RECORD_EVERY_S = 0.05\n"
              f"with verify_slot(Path({str(d)!r}), 1, wait_s=60, poll_s=0.02, record=Path({str(rec_dir / 'w.json')!r})):\n"
              f"    pass\n")
    with verify_slot(d, 1):
        proc = subprocess.Popen([sys.executable, "-c", script])
        time.sleep(1.0)
        proc.kill()                        # the shell tool's timeout
        proc.wait()
    rec = json.loads((rec_dir / "w.json").read_text())
    assert "waited_s" not in rec and rec.get("last_seen")
    assert hostlock.task_wait_seconds(d, "T-9") >= 0.4


def test_snippet_matches_the_template(capsys):
    from pathlib import Path
    assert hostlock.main(["--snippet"]) == 0
    out = capsys.readouterr().out
    assert "SWARM_VERIFY_LOCK" in out and 'exec "${SWARM_VERIFY_LOCK}" -- bash "$0" "$@"' in out
    template = (Path(hostlock.__file__).parent / "template" / "scripts" / "verify_fast.sh").read_text()
    for line in out.splitlines()[1:]:
        assert line in template


# ---- live test hold (Oct 9 2026) ----

def _wait_for_hold_release(d, **kw):
    """Run a lock acquisition in a thread under a hold; returns (thread, got list, logs)."""
    got, logs = [], []

    def waiter():
        with verify_slot(d, 2, wait_s=5, poll_s=0.02, honor_hold=True, log=logs.append, **kw) as held:
            got.append((held, time.monotonic()))

    th = threading.Thread(target=waiter)
    th.start()
    return th, got, logs


def test_hold_blocks_exclusive_and_shared_until_released(tmp_path, monkeypatch):
    monkeypatch.delenv(hostlock.HOLD_BYPASS_ENV, raising=False)
    d = tmp_path / "locks"
    hostlock.write_hold(d, "live test", 30, by="laptop-a")
    for exclusive in (True, False):
        th, got, logs = _wait_for_hold_release(d, exclusive=exclusive)
        time.sleep(0.3)
        assert got == [] and any("live test hold" in m and "live test until" in m for m in logs)
        assert hostlock.clear_hold(d) is True
        th.join(5)
        assert got and got[0][0] is True
        hostlock.write_hold(d, "live test", 30)
    assert hostlock.clear_hold(d) and not hostlock.clear_hold(d)


def test_hold_expires_by_itself(tmp_path, monkeypatch):
    from datetime import datetime, timedelta, timezone
    monkeypatch.delenv(hostlock.HOLD_BYPASS_ENV, raising=False)
    d = tmp_path / "locks"
    t0 = datetime.now(timezone.utc)
    hostlock.write_hold(d, "forgotten", 0.01, now=t0 - timedelta(minutes=5))   # already past its until-time
    assert hostlock.read_hold(d) is None and not (d / hostlock.HOLD_FILE).exists()
    hostlock.write_hold(d, "short", 0.05)                                       # 3 s (stamps are whole seconds)
    t = time.monotonic()
    with verify_slot(d, 1, wait_s=5, poll_s=0.05, honor_hold=True) as held:
        assert held is True
    assert 1.5 < time.monotonic() - t < 5
    assert hostlock.write_hold(d, "default")["until"] > hostlock.write_hold(d, "x", 1)["until"]   # default 60 min


def test_hold_bypass_flag_and_env(tmp_path, monkeypatch):
    d = tmp_path / "locks"
    hostlock.write_hold(d, "live test", 30)
    monkeypatch.setenv(hostlock.HOLD_BYPASS_ENV, "1")
    t = time.monotonic()
    with verify_slot(d, 1, wait_s=5, poll_s=0.05, honor_hold=True, exclusive=True) as held:
        assert held is True
    assert time.monotonic() - t < 1
    monkeypatch.delenv(hostlock.HOLD_BYPASS_ENV)
    env = {**os.environ}
    env.pop(hostlock.HOLD_BYPASS_ENV, None)
    base = [sys.executable, "-m", "swarm.hostlock", "--dir", str(d), "--slots", "1", "--wait", "5"]
    root = str(Path(__file__).resolve().parent.parent)
    out = subprocess.run(base + ["--ignore-hold", "--", "echo", "ok"], cwd=root, env=env, capture_output=True,
                         text=True, timeout=30)
    assert out.returncode == 0 and out.stdout.strip() == "ok"
    # without the flag it waits: a short-lived process is still blocked after 1 s
    p = subprocess.Popen(base + ["--", "echo", "late"], cwd=root, env=env, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, text=True)
    time.sleep(1.5)
    assert p.poll() is None
    hostlock.clear_hold(d)
    stdout, stderr = p.communicate(timeout=15)
    assert stdout.strip() == "late" and "live test hold" in stderr


def test_status_shows_the_hold_line(cfg, tmp_path, monkeypatch):
    from datetime import datetime, timezone
    from swarm.board.memory import InMemoryBoard
    from swarm.status import render_status
    monkeypatch.setattr(hostlock, "lock_dir", lambda project: tmp_path / "locks")
    now = datetime.now(timezone.utc)
    board = InMemoryBoard()
    args = (cfg, board.list_tasks(), board.list_agents(), board.list_questions(), now)
    assert "HOLD:" not in render_status(*args)
    hostlock.write_hold(tmp_path / "locks", "live test", 30)
    line = next(ln for ln in render_status(*args).splitlines() if ln.startswith("HOLD:"))
    assert line.startswith("HOLD: live test until ") and line.endswith(" UTC (measurements and verifies wait)")
    hostlock.clear_hold(tmp_path / "locks")
    assert "HOLD:" not in render_status(*args)
