import contextlib
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


# ---- exclusive hold cap (Q-478) ----
CAP_MIN = 0.2 / 60          # 0.2 s


def test_waiter_behind_an_exclusive_holder_proceeds_after_the_cap(tmp_path, monkeypatch):
    monkeypatch.setenv("SWARM_TASK_ID", "T-meas")
    d = tmp_path / "locks"
    monkeypatch.delenv(HELD_ENV, raising=False)
    logs = []
    with verify_slot(d, 2, exclusive=True, label="measurement: replay"):
        monkeypatch.setenv("SWARM_TASK_ID", "T-35min")
        t0 = time.monotonic()
        with verify_slot(d, 2, wait_s=30, poll_s=0.05, log=logs.append, exclusive_cap_min=CAP_MIN) as held:
            waited = time.monotonic() - t0
            assert held is True                    # proceeds (nested wrappers run straight through)
    assert 0.2 <= waited < 5
    line = "exclusive hold by T-meas exceeded 0.00333333 min; proceeding — that measurement may be perturbed"
    cap_logs = [m for m in logs if "exceeded" in m]
    assert len(cap_logs) == 1 and cap_logs[0].endswith(line.split("T-meas ", 1)[1]) and "T-meas" in cap_logs[0]
    assert not any("no verify slot" in m for m in logs)
    notes = hostlock.task_perturbed_notes(d, "T-meas")
    assert len(notes) == 1 and notes[0].startswith("exclusive hold by T-meas exceeded ")
    assert notes[0].endswith("min; proceeding — that measurement may be perturbed")


def test_exact_line_for_a_25_minute_cap():
    line = hostlock.exclusive_cap_line({"task": "T-9"}, 25)
    assert line == "exclusive hold by T-9 exceeded 25 min; proceeding — that measurement may be perturbed"


def test_waiter_before_the_cap_keeps_waiting_and_gets_the_slot_after(tmp_path, monkeypatch):
    d = tmp_path / "locks"
    monkeypatch.delenv(HELD_ENV, raising=False)
    got = []

    def waiter():
        with verify_slot(d, 2, wait_s=30, poll_s=0.02, exclusive_cap_min=5) as held:
            got.append((held, hostlock.holders(d, 2)))

    with verify_slot(d, 2, exclusive=True):
        th = threading.Thread(target=waiter)
        th.start()
        time.sleep(0.4)
        assert got == []                           # well under the 5 min cap: still queued
    th.join(5)
    assert got and got[0][0] is True and [h for h in got[0][1] if h.get("exclusive")] == []
    assert hostlock.task_perturbed_notes(d, "") == []


def test_cap_counts_only_time_behind_exclusive_holders(tmp_path, monkeypatch):
    """Slots busy with ordinary verifies are not the cap's business: the waiter times out as before."""
    d = tmp_path / "locks"
    monkeypatch.delenv(HELD_ENV, raising=False)
    logs = []
    with verify_slot(d, 1):
        with verify_slot(d, 1, wait_s=0.5, poll_s=0.05, log=logs.append, exclusive_cap_min=CAP_MIN) as held:
            assert held is False
    assert not any("exceeded" in m for m in logs) and any("without one" in m for m in logs)


def test_live_test_hold_is_not_affected_by_the_cap(tmp_path, monkeypatch):
    monkeypatch.delenv(HELD_ENV, raising=False)
    monkeypatch.delenv(hostlock.HOLD_BYPASS_ENV, raising=False)
    d = tmp_path / "locks"
    hostlock.write_hold(d, "live test", 30, by="laptop-a")
    got, logs = [], []

    def waiter():
        with verify_slot(d, 2, wait_s=30, poll_s=0.02, honor_hold=True, log=logs.append,
                         exclusive_cap_min=CAP_MIN) as held:
            got.append(held)

    th = threading.Thread(target=waiter)
    th.start()
    time.sleep(0.6)                                # three times the cap: the hold still holds it
    assert got == [] and any("live test hold" in m for m in logs) and not any("exceeded" in m for m in logs)
    assert hostlock.clear_hold(d)
    th.join(5)
    assert got == [True]                           # and the cap clock only started after the hold ended


def test_cap_from_env_and_flag_in_the_wrapper(tmp_path):
    env = worker_lock_env("demo-cap", 2, 12.5)
    assert env[hostlock.EXCL_CAP_ENV] == "12.5"
    assert hostlock.EXCL_CAP_ENV not in worker_lock_env("demo-cap", 2)


def test_config_reads_exclusive_max_minutes(project_dir, sample_config_dict):
    import yaml
    cfg = load_config(project_dir / ".swarm" / "config.yaml")
    assert cfg.verify.exclusive_max_minutes == 25
    sample_config_dict["verify"] = {**(sample_config_dict.get("verify") or {}), "exclusive_max_minutes": 10}
    (project_dir / ".swarm" / "config.yaml").write_text(yaml.safe_dump(sample_config_dict))
    assert load_config(project_dir / ".swarm" / "config.yaml").verify.exclusive_max_minutes == 10


def test_entry_with_a_reused_pid_is_stale(tmp_path):
    """Q-523: slot entries whose PIDs were reused by long-lived processes (swarm serve, swarm run) never freed."""
    import json
    d = tmp_path / "locks"
    hostlock.ensure_lock_files(d, 2)
    me = os.getpid()
    mine = hostlock._proc_start(me)
    assert mine                                     # this process's start time is readable
    # a crashed holder's entry whose pid now belongs to another process: the recorded start time differs
    (d / "verify-0.holder").write_text(json.dumps({"pid": me, "task": "T-1", "label": "verify", "pstart": "Thu Jan  1 00:00:00 1970"}))
    other = subprocess.Popen(["sleep", "30"])
    (d / "exclusive-pending-424242").write_text(json.dumps({"pid": other.pid, "task": "T-2", "exclusive": True,
                                                            "pstart": "Thu Jan  1 00:00:00 1970"}))
    try:
        assert hostlock.holders(d, 2) == []
        assert hostlock.pending_exclusive(d) == []
        assert not (d / "exclusive-pending-424242").exists()      # a stale pending file is removed
        # an entry with the right start time is live; an old entry without one falls back to the pid check
        (d / "verify-0.holder").write_text(json.dumps({"pid": other.pid, "task": "T-1",
                                                       "pstart": hostlock._proc_start(other.pid)}))
        (d / "verify-1.holder").write_text(json.dumps({"pid": me, "task": "T-3"}))
        assert sorted(h["task"] for h in hostlock.holders(d, 2)) == ["T-1", "T-3"]
        # a normal verify is not held up by a stale pending exclusive file
        (d / "exclusive-pending-424243").write_text(json.dumps({"pid": other.pid, "exclusive": True,
                                                                "pstart": "Thu Jan  1 00:00:00 1970"}))
        with verify_slot(d, 2, wait_s=0.5, poll_s=0.05) as held:
            assert held is True
    finally:
        other.kill()
        other.wait()


def test_holder_entries_record_the_process_start_time(tmp_path):
    import json
    d = tmp_path / "locks"
    with verify_slot(d, 1, label="x") as held:
        assert held
        h = json.loads((d / "verify-0.holder").read_text())
        assert h["pid"] == os.getpid() and h["pstart"] == hostlock._proc_start(os.getpid())


def _wait_for(pred, timeout=15.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.05)
    return False


def test_sigterm_while_waiting_exits_without_running_the_command(tmp_path):
    """Q-523: killing a queued swarm-lock started the batch it wrapped as an orphan."""
    import signal
    d = tmp_path / "locks"
    marker = tmp_path / "ran"
    env = {**os.environ}
    env.pop(HELD_ENV, None)
    root = str(Path(__file__).resolve().parent.parent)
    for exclusive in (False, True):
        with verify_slot(d, 1):                     # the machine is busy: the wrapper queues
            p = subprocess.Popen([sys.executable, "-m", "swarm.hostlock", "--dir", str(d), "--slots", "1",
                                  "--wait", "60", *(["--exclusive"] if exclusive else []), "--",
                                  "touch", str(marker)], cwd=root, env=env, stderr=subprocess.PIPE, text=True)
            assert _wait_for(lambda: not exclusive or list(d.glob(hostlock.PENDING_PREFIX + "*")))
            time.sleep(1.0)                         # it is in the wait loop now
            p.send_signal(signal.SIGTERM)
            _, err = p.communicate(timeout=15)
        assert p.returncode == 128 + signal.SIGTERM, err
        assert "not run" in err
        time.sleep(0.3)
        assert not marker.exists()                  # the command never started, not even after the slot freed
        assert not list(d.glob(hostlock.PENDING_PREFIX + "*"))


def test_sigterm_while_running_stops_the_command_tree(tmp_path):
    import signal
    d = tmp_path / "locks"
    pidfile = tmp_path / "grandchild.pid"
    env = {**os.environ}
    env.pop(HELD_ENV, None)
    root = str(Path(__file__).resolve().parent.parent)
    p = subprocess.Popen([sys.executable, "-m", "swarm.hostlock", "--dir", str(d), "--slots", "1", "--exclusive",
                          "--", "sh", "-c", f"sleep 60 & echo $! > {pidfile}; wait"], cwd=root, env=env,
                         stderr=subprocess.PIPE, text=True)
    assert _wait_for(lambda: pidfile.exists() and pidfile.read_text().strip())
    gc = int(pidfile.read_text().strip())
    p.send_signal(signal.SIGTERM)
    p.communicate(timeout=20)
    assert _wait_for(lambda: not hostlock._alive(gc) or _is_zombie(gc), 15)
    assert not (d / "verify-0.holder").exists()


def _is_zombie(pid):
    out = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    return out.startswith("Z") or not out


def test_exclusive_max_load_waits_for_a_quiet_machine(tmp_path):
    """Q-517, Q-520, Q-521, Q-523: session-level measurements on a loaded Mac measured nothing."""
    d = tmp_path / "locks"
    loads = iter([9.0, 7.5, 3.0])
    logs = []
    with verify_slot(d, 1, exclusive=True, wait_s=5, poll_s=0.01, log=logs.append, max_load=4.0,
                     loadavg=lambda: (next(loads), 0, 0)) as held:
        assert held is True
    assert any("load" in m and "9.0" in m for m in logs)
    # bounded by the wait: past it the measurement runs anyway and says so
    logs.clear()
    with verify_slot(d, 1, exclusive=True, wait_s=0.2, poll_s=0.01, log=logs.append, max_load=4.0,
                     loadavg=lambda: (12.0, 0, 0)) as held:
        assert held is True                         # it holds the machine; only the load did not drop
    assert any("still" in m and "12.0" in m for m in logs)
    # off by default
    with verify_slot(d, 1, exclusive=True, wait_s=0.2, poll_s=0.01, loadavg=lambda: (99.0, 0, 0)) as held:
        assert held is True


def test_max_load_flag_needs_exclusive(tmp_path):
    import pytest
    with pytest.raises(SystemExit):
        hostlock.main(["--dir", str(tmp_path), "--max-load", "4", "--", "true"])
    assert hostlock.main(["--dir", str(tmp_path / "l"), "--exclusive", "--max-load", "1000", "--", "true"]) == 0


# ---- quick slot: web/docs-only verifies do not queue behind measurements for long ----
def test_quick_waiter_proceeds_behind_an_exclusive_holder_after_quick_cap(tmp_path, monkeypatch):
    d = tmp_path / "locks"
    monkeypatch.delenv(HELD_ENV, raising=False)
    monkeypatch.setenv("SWARM_TASK_ID", "T-meas")
    logs = []
    with verify_slot(d, 2, exclusive=True, label="measurement"):
        monkeypatch.setenv("SWARM_TASK_ID", "T-web")
        t0 = time.monotonic()
        with verify_slot(d, 2, wait_s=30, poll_s=0.05, log=logs.append, exclusive_cap_min=25,
                         quick_cap_s=0.2) as held:
            waited = time.monotonic() - t0
            assert held is True                    # nested wrapped verify_fast runs straight through
    assert 0.2 <= waited < 5
    assert any("verify.quick_paths" in m and "T-meas" in m for m in logs)
    assert not any("no verify slot" in m for m in logs)
    assert any("quick_paths" in n for n in hostlock.task_perturbed_notes(d, "T-meas"))


def test_quick_waiter_proceeds_behind_a_queued_exclusive_measurement(tmp_path, monkeypatch):
    """The Oct 10 serve.log case: 'a measurement is waiting for the machine' held every merge for 1800 s."""
    d = tmp_path / "locks"
    monkeypatch.delenv(HELD_ENV, raising=False)
    hostlock.ensure_lock_files(d, 2)
    other = subprocess.Popen(["sleep", "30"])
    import json
    (d / f"exclusive-pending-{other.pid}").write_text(json.dumps(
        {"pid": other.pid, "task": "T-meas", "exclusive": True, "pstart": hostlock._proc_start(other.pid)}))
    try:
        t0 = time.monotonic()
        with verify_slot(d, 2, wait_s=0.3, poll_s=0.05) as held:   # an audio verify keeps waiting (runs out here)
            assert held is False
        assert time.monotonic() - t0 >= 0.3
        t0 = time.monotonic()
        with verify_slot(d, 2, wait_s=30, poll_s=0.05, quick_cap_s=0.2) as held:
            assert held is True
        assert time.monotonic() - t0 < 5
    finally:
        other.kill()
        other.wait()


def test_quick_cap_does_not_skip_ordinary_slot_contention(tmp_path, monkeypatch):
    d = tmp_path / "locks"
    monkeypatch.delenv(HELD_ENV, raising=False)
    with verify_slot(d, 1):                        # a normal verify, not a measurement
        t0 = time.monotonic()
        with verify_slot(d, 1, wait_s=0.5, poll_s=0.05, quick_cap_s=0.1) as held:
            assert held is False                   # waited the full wait_s: the quick cap is for measurements only
        assert time.monotonic() - t0 >= 0.5


def test_run_script_passes_quick_wait_to_the_slot(git_repo):
    ws = Workspace(git_repo, git_repo.parent / "wt", "main")
    seen = []

    @contextlib.contextmanager
    def slot(label, quick_wait_s=None):
        seen.append(quick_wait_s)
        yield True

    ws.verify_slot = slot
    script = git_repo / "scripts" / "q.sh"
    script.parent.mkdir(exist_ok=True)
    script.write_text("exit 0\n")
    ws.run_script(git_repo, "scripts/q.sh", 30)
    ws.run_script(git_repo, "scripts/q.sh", 30, quick_wait_s=60)
    assert seen == [None, 60]
