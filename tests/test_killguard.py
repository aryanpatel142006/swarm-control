"""swarm/killguard.py: under a task, pkill/killall signal only that task's own processes (Oct 10: a worker's
`pkill -f "bash .swarm-run/batch.sh"` killed other tasks' swarm-lock measurements)."""
import os
import shutil
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

from swarm import killguard

ROOT = str(Path(__file__).resolve().parent.parent)


def _env(task=None):
    env = {k: v for k, v in os.environ.items() if k != killguard.TASK_ENV}
    if task:
        env[killguard.TASK_ENV] = task
    return env


def _sleeper(task, marker):
    """A process whose command line contains `marker` (like two worktrees' `bash .swarm-run/batch.sh`)."""
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", marker], env=_env(task))


def _gone(p, timeout=10.0):
    try:
        p.wait(timeout=timeout)
        return True
    except subprocess.TimeoutExpired:
        return False


def _cleanup(*procs):
    for p in procs:
        if p.poll() is None:
            p.kill()
            p.wait(timeout=10)


def _wait_task(p, task):
    end = time.monotonic() + 10
    while time.monotonic() < end:
        if killguard.task_of(p.pid) == task:
            return True
        time.sleep(0.05)
    return False


def test_task_of_reads_another_processes_environment():
    p = _sleeper("T-900", f"m-{uuid.uuid4().hex}")
    q = _sleeper(None, f"m-{uuid.uuid4().hex}")
    try:
        assert _wait_task(p, "T-900")
        assert killguard.task_of(q.pid) is None
    finally:
        _cleanup(p, q)


def test_split_pkill_args():
    term, kill = int(signal.SIGTERM), int(signal.SIGKILL)
    assert killguard.split_pkill_args(["-f", "bash .swarm-run/batch.sh"]) == (term, ["-f", "bash .swarm-run/batch.sh"])
    assert killguard.split_pkill_args(["-TERM", "-f", "x"]) == (term, ["-f", "x"])
    assert killguard.split_pkill_args(["-9", "x"]) == (kill, ["x"])
    assert killguard.split_pkill_args(["-SIGKILL", "-f", "x"]) == (kill, ["-f", "x"])
    assert killguard.split_pkill_args(["--signal", "KILL", "-f", "x"]) == (kill, ["-f", "x"])
    assert killguard.split_pkill_args(["-f", "-9"]) == (term, ["-f", "-9"])     # only the first word is a signal
    assert killguard.split_killall_args(["-9", "python3"]) == (kill, ["python3"], False)
    assert isinstance(killguard.split_killall_args(["-d", "x"]), str)


@pytest.mark.parametrize("via_wrapper", [False, True])
def test_pkill_under_a_task_spares_other_tasks(tmp_path, via_wrapper):
    marker = f"bash .swarm-run/batch.sh {uuid.uuid4().hex}"
    mine, theirs, human = _sleeper("T-370", marker), _sleeper("T-368", marker), _sleeper(None, marker)
    try:
        assert _wait_task(mine, "T-370") and _wait_task(theirs, "T-368")
        if via_wrapper:
            pkill = [str(p) for p in killguard.write_guards(tmp_path / "bin") if p.name == "pkill"]
            cmd = pkill + ["-TERM", "-f", marker]
        else:
            cmd = [sys.executable, "-c", "import sys; from swarm.killguard import main; sys.exit(main('pkill'))",
                   "-TERM", "-f", marker]
        r = subprocess.run(cmd, cwd=ROOT, env=_env("T-370"), capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, r.stderr
        assert _gone(mine)
        assert theirs.poll() is None and human.poll() is None
        assert "left 2 matching" in r.stderr and "task T-368" in r.stderr and str(theirs.pid) in r.stderr
        # nothing of this task matches any more: pkill's "no process matched"
        r = subprocess.run(cmd, cwd=ROOT, env=_env("T-370"), capture_output=True, text=True, timeout=60)
        assert r.returncode == killguard.NO_MATCH_RC
        assert theirs.poll() is None
    finally:
        _cleanup(mine, theirs, human)


def test_wrapper_without_a_task_is_the_real_pkill(tmp_path):
    marker = f"plain-{uuid.uuid4().hex}"
    p = _sleeper("T-1", marker)
    try:
        pkill = [x for x in killguard.write_guards(tmp_path / "bin") if x.name == "pkill"][0]
        body = pkill.read_text()
        assert "exec '/" in body and "bin/pkill'" in body           # the system pkill, not the guard itself
        r = subprocess.run([str(pkill), "-f", marker], env=_env(None), timeout=30)
        assert r.returncode == 0 and _gone(p)
    finally:
        _cleanup(p)


def test_killall_under_a_task_spares_other_tasks(tmp_path):
    name = f"kg{uuid.uuid4().hex[:8]}"
    exe = tmp_path / name
    src = tmp_path / "s.c"
    src.write_text("#include <unistd.h>\nint main(void) { sleep(60); return 0; }\n")
    cc = shutil.which("cc")
    if not cc or subprocess.run([cc, "-o", str(exe), str(src)], capture_output=True).returncode != 0:
        pytest.skip("no C compiler for a uniquely named process")   # a copied /bin/sleep is killed by macOS
    a = subprocess.Popen([str(exe), "60"], env=_env("T-A"))
    b = subprocess.Popen([str(exe), "60"], env=_env("T-B"))
    try:
        assert _wait_task(a, "T-A") and _wait_task(b, "T-B")
        killall = [x for x in killguard.write_guards(tmp_path / "bin") if x.name == "killall"][0]
        r = subprocess.run([str(killall), "-9", name], cwd=ROOT, env=_env("T-A"), capture_output=True, text=True,
                           timeout=60)
        assert r.returncode == 0, r.stderr
        assert _gone(a) and b.poll() is None
        r = subprocess.run([str(killall), "-d", name], env=_env("T-A"), capture_output=True, text=True, timeout=60)
        assert r.returncode == killguard.USAGE_RC and b.poll() is None
    finally:
        _cleanup(a, b)


def test_pkill_does_not_kill_its_own_calling_shell(tmp_path):
    """`bash -c 'pkill -f X; echo after'`: the calling shell's command line contains X too."""
    marker = f"selfmatch-{uuid.uuid4().hex}"
    p = _sleeper("T-5", marker)
    try:
        pkill = [x for x in killguard.write_guards(tmp_path / "bin") if x.name == "pkill"][0]
        r = subprocess.run(["bash", "-c", f"'{pkill}' -f '{marker}'; echo after-$?"], env=_env("T-5"),
                           capture_output=True, text=True, timeout=60)
        assert "after-0" in r.stdout, r.stderr
        assert _gone(p)
    finally:
        _cleanup(p)


def test_hidden_environment_falls_back_to_the_worktree(tmp_path):
    """macOS hides /bin/bash's environment from ps: the T-368 batch tree was bash processes in each worktree."""
    marker = f"bash .swarm-run/batch.sh {uuid.uuid4().hex}"
    wt_a, wt_b = tmp_path / "wt" / "T-A", tmp_path / "wt" / "T-B"
    wt_a.mkdir(parents=True)
    wt_b.mkdir(parents=True)
    script = f"while :; do sleep 1; done; : '{marker}'"
    a = subprocess.Popen(["/bin/bash", "-c", script], cwd=wt_a, env=_env("T-A"))
    b = subprocess.Popen(["/bin/bash", "-c", script], cwd=wt_b, env=_env("T-B"))
    try:
        time.sleep(0.5)
        assert killguard.owner(a.pid, "T-A", str(wt_a))[0] is True
        assert killguard.owner(b.pid, "T-A", str(wt_a))[0] is False
        pkill = [x for x in killguard.write_guards(tmp_path / "bin") if x.name == "pkill"][0]
        r = subprocess.run([str(pkill), "-f", marker], cwd=ROOT, env={**_env("T-A"), "SWARM_WORKTREE": str(wt_a)},
                           capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, r.stderr
        assert _gone(a) and b.poll() is None
    finally:
        _cleanup(a, b)
