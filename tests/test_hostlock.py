import os
import subprocess
import sys
import threading
import time

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
