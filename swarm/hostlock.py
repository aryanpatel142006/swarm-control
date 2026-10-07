"""Per-host verify slots: at most N verify runs at once on one machine, and an exclusive mode for measurements.

Oct 6 2026 (selective-hearing): with 6-9 workers on laptop-a the fast verify went from 55 s to 81-89 s, wall-clock
test assertions and a live-replay check in verify_full failed on unchanged code, and latency acceptance criteria
became coin flips (Q-136, Q-175, Q-177, Q-185, Q-189, Q-190, Q-191, Q-193, Q-195). Field note 61 sketched this:

- N lock files `verify-<i>.lock` under `~/.swarm/<project>/locks/`, taken with `fcntl.flock`; a verify holds any
  one of them, so N run at once and the rest wait (with a log line, never a failure). N is
  `hosts.<host>.max_parallel_verify` (default 2).
- `--exclusive` takes all N (in order, so two exclusive holders cannot deadlock): no harness or wrapped verify runs
  on the machine while a latency measurement or a real-time replay runs.
- Every verify the harness runs (`Workspace.run_script`: runner pre-publish, merger, reviewer) takes a slot. Verifies
  a worker runs inside its CLI take one when the project's verify script wraps itself (template
  `scripts/verify_fast.sh`) or the worker runs `swarm-lock -- <cmd>`; the runner puts `swarm-lock` on the worker's
  PATH and exports the lock directory. `SWARM_VERIFY_SLOT_HELD=1` marks a process tree that already holds a slot,
  so nested wrappers run straight through instead of waiting on themselves.

flock works on a read-only descriptor on macOS and Linux, so a sandboxed CLI that may not write outside its
worktree can still take a slot: the runner creates the files beforehand.

Waiting is visible and accounted for (Q-229, Q-232, Q-238, Q-241: measurements waited 5-7 min behind other
worktrees' verify_full without knowing why, inside 20-40 minute runs):
- a holder writes `verify-<i>.holder` (pid, task, label, start), and a waiter's log names who holds the slots;
- an exclusive waiter drops `exclusive-pending-<pid>`, and new verifies queue behind it instead of taking the slots
  it is waiting for (writer preference: it waits for running verifies only, never for ones that start later);
- a worker's wait is written to `waits/<task>/<pid>-<ns>.json`; the runner adds that time back to the run's
  wall-clock limit (capped), so a queue does not eat the task's budget.

CLI: `python -m swarm.hostlock [--exclusive] [--dir D] [--slots N] [--wait S] -- cmd args…` (exit code = cmd's);
`swarm-lock --snippet` prints the re-exec lines a project's verify script needs to take part (Q-224).
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Iterator

DIR_ENV = "SWARM_VERIFY_LOCK_DIR"
SLOTS_ENV = "SWARM_VERIFY_SLOTS"
HELD_ENV = "SWARM_VERIFY_SLOT_HELD"
CMD_ENV = "SWARM_VERIFY_LOCK"          # absolute path of the `swarm-lock` wrapper script
DEFAULT_SLOTS = 2
DEFAULT_WAIT_S = 1800                  # after this a verify runs without a slot (logged) rather than never
PENDING_PREFIX = "exclusive-pending-"
WAITS_DIR = "waits"
# A waiter refreshes its record this often: when the worker's shell tool kills a queued `swarm-lock` (a 600 s tool
# timeout, Q-249) the wait still counts up to the last refresh. Before, a killed waiter's time was lost and T-105
# attempt 1 timed out at exactly 1200 s although most of it was spent queued (Q-253).
WAIT_RECORD_EVERY_S = 15
REPORT_EVERY_S = 300                   # a long wait repeats who holds the slots this often
SNIPPET = """# Under a swarm runner: wait for one of this machine's verify slots (swarm-control hostlock)
if [ -n "${SWARM_VERIFY_LOCK:-}" ] && [ -z "${SWARM_VERIFY_SLOT_HELD:-}" ] && [ -x "${SWARM_VERIFY_LOCK}" ]; then
  exec "${SWARM_VERIFY_LOCK}" -- bash "$0" "$@"
fi
"""


def lock_dir(project: str) -> Path:
    return Path.home() / ".swarm" / project / "locks"


def ensure_lock_files(directory: Path, slots: int) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for i in range(max(1, slots)):
        f = directory / f"verify-{i}.lock"
        if not f.exists():
            f.touch()


def _open(path: Path) -> int:
    try:
        return os.open(str(path), os.O_RDWR | os.O_CREAT, 0o644)
    except OSError:
        return os.open(str(path), os.O_RDONLY)   # sandboxed: the file exists, flock still works read-only


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except (ProcessLookupError, OSError, ValueError):
        return False


def _write_json(path: Path, data: dict) -> None:
    try:
        tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(data))
        tmp.replace(path)
    except OSError:
        pass   # a read-only sandbox cannot write it; the lock itself still works


def _read_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _me(label: str, exclusive: bool) -> dict:
    return {"pid": os.getpid(), "task": os.environ.get("SWARM_TASK_ID", ""), "label": label,
            "exclusive": exclusive, "started": time.time()}


def _unlink_if_mine(path: Path) -> None:
    data = _read_json(path)
    if data is not None and data.get("pid") == os.getpid():
        with contextlib.suppress(OSError):
            path.unlink()


def holders(directory: Path | str, slots: int) -> list[dict]:
    """Who holds the slots now (live processes only; a crashed holder's file is ignored)."""
    out, seen = [], set()
    for i in range(max(1, slots)):
        h = _read_json(Path(directory) / f"verify-{i}.holder")
        if h and isinstance(h.get("pid"), int) and _alive(h["pid"]) and h["pid"] not in seen:
            seen.add(h["pid"])
            out.append(h)
    return out


def pending_exclusive(directory: Path | str) -> list[dict]:
    out = []
    for f in sorted(Path(directory).glob(PENDING_PREFIX + "*")):
        h = _read_json(f)
        if h and isinstance(h.get("pid"), int) and h["pid"] != os.getpid() and _alive(h["pid"]):
            out.append(h)
    return out


def describe(entries: list[dict], now: float | None = None) -> str:
    now = now or time.time()
    bits = []
    for h in entries:
        mins = max(0.0, (now - float(h.get("started") or now)) / 60)
        who = h.get("task") or "harness"
        bits.append(f"{who} {h.get('label') or 'verify'}{' (exclusive)' if h.get('exclusive') else ''} "
                    f"(pid {h.get('pid')}, {mins:.0f} min)")
    return ", ".join(bits) or "unknown holders"


def task_wait_seconds(directory: Path | str, task_id: str, now: float | None = None) -> float:
    """Seconds this task's processes have spent waiting for slots (finished waits plus ones still waiting)."""
    now = now or time.time()
    total = 0.0
    for f in (Path(directory) / WAITS_DIR / task_id).glob("*.json"):
        d = _read_json(f) or {}
        total += float(d.get("waited_s") or 0)
        since = d.get("waiting_since")
        if since and isinstance(d.get("pid"), int) and _alive(d["pid"]):
            total += max(0.0, now - float(since))
        elif since:          # the waiter was killed while queued: count it up to its last refresh
            total += max(0.0, float(d.get("last_seen") or since) - float(since))
    return total


def clear_task_waits(directory: Path | str, task_id: str) -> None:
    import shutil
    shutil.rmtree(Path(directory) / WAITS_DIR / task_id, ignore_errors=True)


@contextlib.contextmanager
def verify_slot(directory: Path | str, slots: int = DEFAULT_SLOTS, *, exclusive: bool = False,
                wait_s: float = DEFAULT_WAIT_S, poll_s: float = 1.0, log: Callable[[str], None] | None = None,
                sleep: Callable[[float], None] = time.sleep, label: str = "verify",
                record: Path | None = None) -> Iterator[bool]:
    """Hold one slot (or all of them with exclusive=True). Yields True when held, False when the wait ran out and
    the caller proceeds without one. Already inside a slot (HELD_ENV set): yields True at once. `record` is a file
    the wait is written to (the runner adds a worker's waits back to its wall-clock limit)."""
    if os.environ.get(HELD_ENV):
        yield True
        return
    directory = Path(directory)
    slots = max(1, int(slots))
    try:
        ensure_lock_files(directory, slots)
    except OSError:
        pass   # read-only sandbox: the runner created the files; _open falls back to O_RDONLY
    fds: list[int] = []
    held_idx: list[int] = []
    deadline = time.monotonic() + wait_s
    started = time.monotonic()
    me = _me(label, exclusive)
    pending = directory / f"{PENDING_PREFIX}{os.getpid()}"
    state = {"announced": False, "last_report": 0.0, "waiting": False, "since": 0.0, "last_record": 0.0}

    def waiting(reason: str) -> None:
        nowm = time.monotonic()
        if not state["waiting"]:
            state["waiting"], state["since"] = True, time.time()
        if record is not None and (state["last_record"] == 0.0 or nowm - state["last_record"] >= WAIT_RECORD_EVERY_S):
            state["last_record"] = nowm
            _write_json(record, {"pid": os.getpid(), "waiting_since": state["since"], "last_seen": time.time(),
                                 "label": label})
        if log and (not state["announced"] or nowm - state["last_report"] >= REPORT_EVERY_S):
            prefix = reason if not state["announced"] else f"still waiting after {nowm - started:.0f} s; {reason}"
            log(f"{label}: {prefix}")
            state["announced"], state["last_report"] = True, nowm

    def own_task_note(entries: list[dict]) -> str:
        mine = [h for h in entries if me["task"] and h.get("task") == me["task"] and h.get("exclusive")]
        return (" Your own task holds the exclusive measurement lock: this waits until that measurement ends; do not "
                "run verify while your measurement runs (rule 18)." if mine else "")

    try:
        if exclusive:
            _write_json(pending, me)
            for i in range(slots):
                fd = _open(directory / f"verify-{i}.lock")
                fds.append(fd)
                while True:
                    try:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        held_idx.append(i)
                        break
                    except BlockingIOError:
                        if time.monotonic() > deadline:
                            raise TimeoutError
                        busy = [h for h in holders(directory, slots) if h.get("pid") != os.getpid()]
                        waiting(f"waiting for the machine to be free of verify runs (exclusive, {slots} slots); "
                                f"running now: {describe(busy)}; new verifies queue behind this measurement")
                        sleep(poll_s)
            with contextlib.suppress(OSError):
                pending.unlink()
        else:
            while True:
                queued = pending_exclusive(directory)
                if not queued:
                    for i in range(slots):
                        fd = _open(directory / f"verify-{i}.lock")
                        try:
                            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                            fds.append(fd)
                            held_idx.append(i)
                            break
                        except BlockingIOError:
                            os.close(fd)
                    if fds:
                        break
                if time.monotonic() > deadline:
                    raise TimeoutError
                if queued:
                    waiting(f"a measurement is waiting for the machine ({describe(queued)}); this verify runs after "
                            f"it.{own_task_note(queued)}")
                else:
                    busy = holders(directory, slots)
                    waiting(f"all {slots} verify slots on this machine are busy ({describe(busy)}); waiting."
                            f"{own_task_note(busy)}")
                sleep(poll_s)
        for i in held_idx:
            _write_json(directory / f"verify-{i}.holder", me)
        if record is not None and state["waiting"]:
            _write_json(record, {"pid": os.getpid(), "waited_s": time.monotonic() - started, "label": label})
        yield True
    except TimeoutError:
        if log:
            log(f"{label}: no verify slot after {int(wait_s)} s; running without one")
        for fd in fds:
            os.close(fd)
        fds, held_idx = [], []
        with contextlib.suppress(OSError):
            pending.unlink()
        if record is not None:
            _write_json(record, {"pid": os.getpid(), "waited_s": time.monotonic() - started, "label": label})
        yield False
    finally:
        if exclusive:
            with contextlib.suppress(OSError):
                pending.unlink()
        for i in held_idx:
            _unlink_if_mine(directory / f"verify-{i}.holder")
        for fd in fds:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)


def write_wrapper(bin_dir: Path, python: str | None = None) -> Path:
    """`<bin_dir>/swarm-lock`: a shell wrapper around this module with the harness's own interpreter, so a worker
    (any shell, any venv) can run `swarm-lock -- <cmd>` without knowing where swarm-control lives."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    path = bin_dir / "swarm-lock"
    pkg_root = str(Path(__file__).resolve().parent.parent)
    # sys.path.insert, not PYTHONPATH=…: the wrapped command inherits this process's environment and must keep the
    # worker's PYTHONPATH (worktree first, rule 13)
    code = f"import sys; sys.path.insert(0, {pkg_root!r}); from swarm.hostlock import main; sys.exit(main())"
    body = ("#!/bin/sh\n# written by swarm-control (swarm/hostlock.py); takes a per-host verify slot around a command\n"
            f"exec '{python or sys.executable}' -c \"{code}\" \"$@\"\n")
    if not path.exists() or path.read_text() != body:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(body)
        tmp.chmod(0o755)
        tmp.replace(path)
    return path


def worker_lock_env(project: str, slots: int) -> dict[str, str]:
    """Environment a worker CLI gets so its verify scripts (and `swarm-lock`) share the harness's slots."""
    directory = lock_dir(project)
    try:
        ensure_lock_files(directory, slots)
        wrapper = write_wrapper(directory.parent / "bin")
    except OSError:
        return {}
    return {DIR_ENV: str(directory), SLOTS_ENV: str(slots), CMD_ENV: str(wrapper),
            "PATH": os.pathsep.join([str(wrapper.parent), os.environ.get("PATH", "")])}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="swarm-lock", description=__doc__.split("\n\n")[0])
    ap.add_argument("--exclusive", action="store_true", help="take every slot (latency measurements, live replays)")
    ap.add_argument("--dir", default=os.environ.get(DIR_ENV, ""))
    ap.add_argument("--slots", type=int, default=int(os.environ.get(SLOTS_ENV) or DEFAULT_SLOTS))
    ap.add_argument("--wait", type=float, default=DEFAULT_WAIT_S, help="seconds to wait before running anyway")
    ap.add_argument("--snippet", action="store_true",
                    help="print the lines that make a project's verify script take a slot, and exit")
    ap.add_argument("cmd", nargs=argparse.REMAINDER)
    a = ap.parse_args(argv)
    if a.snippet:
        print(SNIPPET, end="")
        return 0
    cmd = a.cmd[1:] if a.cmd[:1] == ["--"] else a.cmd
    if not cmd:
        ap.error("no command given (swarm-lock [--exclusive] -- <cmd> …)")
    if not a.dir:   # not under a swarm runner: nothing to coordinate with
        return subprocess.call(cmd)
    log = lambda m: print(f"[swarm-lock] {m}", file=sys.stderr, flush=True)   # noqa: E731
    started = time.monotonic()
    task = os.environ.get("SWARM_TASK_ID", "")
    record = None
    if task:
        try:
            wdir = Path(a.dir) / WAITS_DIR / task
            wdir.mkdir(parents=True, exist_ok=True)
            record = wdir / f"{os.getpid()}-{time.time_ns()}.json"
        except OSError:
            record = None
    label = ("measurement" if a.exclusive else "verify") + f": {' '.join(cmd)[:80]}"
    with verify_slot(a.dir, a.slots, exclusive=a.exclusive, wait_s=a.wait, log=log, label=label,
                     record=record) as held:
        waited = time.monotonic() - started
        if waited >= 5:
            log(f"waited {waited:.0f} s for {'the machine' if a.exclusive else 'a slot'}"
                + (" (the runner adds this back to the run's time limit)" if record else ""))
        env = {**os.environ, HELD_ENV: "1"} if held else dict(os.environ)
        try:
            return subprocess.call(cmd, env=env)
        except FileNotFoundError:
            log(f"command not found: {cmd[0]}")
            return 127


if __name__ == "__main__":
    sys.exit(main())
