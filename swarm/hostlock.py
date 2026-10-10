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

Live test hold (Oct 9 2026: a measurement replayed against the shared remote GPU during a human live test and the
live session's tier overran, rtf 1.72): `swarm hold "<reason>" [--minutes N]` writes `locks/HOLD` (reason, since,
until, by); while it exists `swarm-lock` (exclusive and shared) waits before taking any slot, polling, and names
the reason and the until-time. It ends by itself at `until` (default 60 min), and `swarm release` removes it.
`--ignore-hold` or `SWARM_HOLD_BYPASS=1` skips the wait (the humans' own tooling).

Exclusive hold cap (Q-478: a 35-minute task's verify_fast waited 1801 s behind other tasks' exclusive measurements):
`verify.exclusive_max_minutes` (default 25; env SWARM_VERIFY_EXCLUSIVE_MAX_MIN, `--exclusive-max-minutes`). A normal
waiter that has been queued behind exclusive holders for longer than that proceeds without a slot (all of them are
taken) and says so: `exclusive hold by <holder> exceeded <N> min; proceeding - that measurement may be perturbed`.
The same line goes to `waits/<holder task>/perturbed.log`, which the runner appends to the measurement task's
harness notes. The live-test hold is not affected: it is waited out before the clock starts and has no cap.

CLI: `python -m swarm.hostlock [--exclusive] [--dir D] [--slots N] [--wait S] -- cmd args…` (exit code = cmd's);
`swarm-lock --snippet` prints the re-exec lines a project's verify script needs to take part (Q-224).
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import socket
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterator

DIR_ENV = "SWARM_VERIFY_LOCK_DIR"
SLOTS_ENV = "SWARM_VERIFY_SLOTS"
HELD_ENV = "SWARM_VERIFY_SLOT_HELD"
EXCL_CAP_ENV = "SWARM_VERIFY_EXCLUSIVE_MAX_MIN"
DEFAULT_EXCL_CAP_MIN = 25.0            # a waiter queued behind exclusive holders this long proceeds (Q-478)
PERTURBED_LOG = "perturbed.log"        # in waits/<holder task>/: the cap lines, for the measurement task's report
CMD_ENV = "SWARM_VERIFY_LOCK"          # absolute path of the `swarm-lock` wrapper script
DEFAULT_SLOTS = 2
DEFAULT_WAIT_S = 1800                  # after this a verify runs without a slot (logged) rather than never
HOLD_FILE = "HOLD"
HOLD_BYPASS_ENV = "SWARM_HOLD_BYPASS"
DEFAULT_HOLD_MINUTES = 60
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


_PSTART_CACHE: dict[int, tuple[float, str]] = {}
_PSTART_TTL_S = 5.0


def _proc_start(pid: int) -> str:
    """The process's start time as `ps` prints it ("" when unknown). With the pid it identifies one process: an entry
    whose pid was reused by another process (a long-lived `swarm serve` or `swarm run`, Q-523) no longer matches."""
    now = time.monotonic()
    hit = _PSTART_CACHE.get(pid)
    if hit and now - hit[0] < _PSTART_TTL_S:
        return hit[1]
    try:
        r = subprocess.run(["ps", "-o", "lstart=", "-p", str(int(pid))], capture_output=True, text=True, timeout=5)
        out = " ".join(r.stdout.split()) if r.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError, ValueError):
        out = ""
    _PSTART_CACHE[pid] = (now, out)
    return out


def _entry_alive(h: dict) -> bool:
    """A slot/pending entry still names a live process: its pid is alive and, when the entry recorded the process's
    start time, the process with that pid now is the same one (not a reused pid). Entries written before Oct 10
    have no start time and fall back to the pid check."""
    pid = h.get("pid")
    if not isinstance(pid, int) or not _alive(pid):
        return False
    want = h.get("pstart")
    if not want:
        return True
    now = _proc_start(pid)
    return not now or " ".join(str(want).split()) == now


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
            "exclusive": exclusive, "started": time.time(), "pstart": _proc_start(os.getpid())}


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
        if h and _entry_alive(h) and (h["pid"], h.get("label")) not in seen:
            seen.add((h["pid"], h.get("label")))      # two harness verifies in one process are two holders
            out.append(h)
    return out


def pending_exclusive(directory: Path | str) -> list[dict]:
    out = []
    for f in sorted(Path(directory).glob(PENDING_PREFIX + "*")):
        h = _read_json(f)
        if h is None or not isinstance(h.get("pid"), int) or h["pid"] == os.getpid():
            continue
        if _entry_alive(h):
            out.append(h)
        else:          # its waiter died (SIGKILL) or the pid was reused: the file would stall every verify (Q-523)
            with contextlib.suppress(OSError):
                f.unlink()
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


def _iso(t: datetime) -> str:
    return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(v: object) -> datetime | None:
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def write_hold(directory: Path | str, reason: str, minutes: float = DEFAULT_HOLD_MINUTES, *,
               by: str = "", now: datetime | None = None) -> dict:
    """Start a live-test hold: measurements and verifies wait until it is released or `minutes` have passed."""
    now = now or datetime.now(timezone.utc)
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    data = {"reason": reason, "since": _iso(now), "until": _iso(now + timedelta(minutes=minutes)),
            "by": by or socket.gethostname()}
    (directory / HOLD_FILE).write_text(json.dumps(data))
    return data


def read_hold(directory: Path | str, now: datetime | None = None) -> dict | None:
    """The active hold, or None. An expired hold is ignored (and removed when possible), so a forgotten one
    cannot stall the swarm; an unreadable HOLD file counts as no hold."""
    path = Path(directory) / HOLD_FILE
    data = _read_json(path)
    until = _parse_iso(data.get("until")) if data else None
    if until is None:
        return None
    if (now or datetime.now(timezone.utc)) >= until:
        with contextlib.suppress(OSError):
            path.unlink()
        return None
    return {**data, "until_dt": until}


def clear_hold(directory: Path | str) -> bool:
    try:
        (Path(directory) / HOLD_FILE).unlink()
        return True
    except OSError:
        return False


def hold_text(hold: dict) -> str:
    return f"{hold.get('reason') or 'live test'} until {hold['until_dt'].strftime('%H:%M')} UTC"


def task_wait_seconds(directory: Path | str, task_id: str, now: float | None = None) -> float:
    """Seconds this task's processes have spent waiting for slots (finished waits plus ones still waiting)."""
    now = now or time.time()
    total = 0.0
    for f in (Path(directory) / WAITS_DIR / task_id).glob("*.json"):
        d = _read_json(f) or {}
        total += float(d.get("waited_s") or 0)
        since = d.get("waiting_since")
        if since and _entry_alive(d):
            total += max(0.0, now - float(since))
        elif since:          # the waiter was killed while queued: count it up to its last refresh
            total += max(0.0, float(d.get("last_seen") or since) - float(since))
    return total


def exclusive_cap_line(holder: dict, minutes: float) -> str:
    who = holder.get("task") or "harness"
    return (f"exclusive hold by {who} exceeded {minutes:g} min; proceeding \u2014 that measurement may be perturbed")


def _note_perturbed(directory: Path, holder: dict, line: str) -> None:
    """Write the cap line into the holder's wait log (waits/<holder task>/perturbed.log)."""
    try:
        d = directory / WAITS_DIR / (holder.get("task") or "_harness")
        d.mkdir(parents=True, exist_ok=True)
        with open(d / PERTURBED_LOG, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def task_perturbed_notes(directory: Path | str, task_id: str) -> list[str]:
    """The lines other tasks wrote because they ran past this task's exclusive hold (for its harness notes)."""
    try:
        text = (Path(directory) / WAITS_DIR / task_id / PERTURBED_LOG).read_text()
    except OSError:
        return []
    return list(dict.fromkeys(l.strip() for l in text.splitlines() if l.strip()))


def clear_task_waits(directory: Path | str, task_id: str) -> None:
    import shutil
    shutil.rmtree(Path(directory) / WAITS_DIR / task_id, ignore_errors=True)


class _ExclusiveCapExceeded(TimeoutError):
    """The wait behind exclusive holders passed `exclusive_cap_min`: proceed without a slot (already logged)."""


@contextlib.contextmanager
def verify_slot(directory: Path | str, slots: int = DEFAULT_SLOTS, *, exclusive: bool = False,
                wait_s: float = DEFAULT_WAIT_S, poll_s: float = 1.0, log: Callable[[str], None] | None = None,
                sleep: Callable[[float], None] = time.sleep, label: str = "verify",
                record: Path | None = None, honor_hold: bool = False,
                exclusive_cap_min: float | None = None) -> Iterator[bool]:
    """Hold one slot (or all of them with exclusive=True). Yields True when held, False when the wait ran out and
    the caller proceeds without one. Already inside a slot (HELD_ENV set): yields True at once. `record` is a file
    the wait is written to (the runner adds a worker's waits back to its wall-clock limit). `honor_hold`: first wait
    while a live-test hold exists (`swarm hold`), unless SWARM_HOLD_BYPASS=1; that wait does not use up `wait_s`.
    `exclusive_cap_min`: a non-exclusive waiter that has been queued behind exclusive holders for longer than this
    proceeds without a slot (yields True, so nested wrappers do not wait again) and logs it; the holder's wait log gets the same line (Q-478)."""
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
    cap_s = None if exclusive_cap_min is None or exclusive_cap_min <= 0 else exclusive_cap_min * 60
    behind = {"s": 0.0, "last": time.monotonic()}
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
            if not state["announced"] and record is not None:
                # Q-290: a worker read a few minutes behind a measurement as lost budget; it is credited back
                prefix += " The time this waits is added back to your run's time limit (up to half of it)."
            log(f"{label}: {prefix}")
            state["announced"], state["last_report"] = True, nowm

    def check_exclusive_cap() -> None:
        """Count the time spent while an exclusive holder is on the machine; past the cap, proceed (Q-478)."""
        nowm = time.monotonic()
        dt, behind["last"] = nowm - behind["last"], nowm
        if cap_s is None:
            return
        excl = [h for h in holders(directory, slots) if h.get("exclusive")]
        if not excl:
            return
        behind["s"] += dt
        if behind["s"] > cap_s:
            line = exclusive_cap_line(excl[0], cap_s / 60)
            if log:
                log(f"{label}: {line}")
            for h in excl:
                _note_perturbed(directory, h, line)
            raise _ExclusiveCapExceeded(line)

    def own_task_note(entries: list[dict]) -> str:
        mine = [h for h in entries if me["task"] and h.get("task") == me["task"] and h.get("exclusive")]
        return (" Your own task holds the exclusive measurement lock: this waits until that measurement ends; do not "
                "run verify while your measurement runs (rule 18)." if mine else "")

    try:
        if honor_hold and os.environ.get(HOLD_BYPASS_ENV) != "1":
            while (hold := read_hold(directory)) is not None:
                waiting(f"live test hold on this machine and the shared GPU ({hold_text(hold)}); waiting for "
                        f"`swarm release` or the until-time. A measurement or verify must not disturb a human test.")
                sleep(poll_s)
            deadline = time.monotonic() + wait_s       # the slot wait starts after the hold
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
            behind["last"] = time.monotonic()
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
                check_exclusive_cap()
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
    except TimeoutError as e:
        capped = isinstance(e, _ExclusiveCapExceeded)
        if log and not capped:
            log(f"{label}: no verify slot after {int(wait_s)} s; running without one")
        for fd in fds:
            os.close(fd)
        fds, held_idx = [], []
        with contextlib.suppress(OSError):
            pending.unlink()
        if record is not None:
            _write_json(record, {"pid": os.getpid(), "waited_s": time.monotonic() - started, "label": label})
        yield capped      # past the exclusive cap: True, so wrapped verifies inside run straight through
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


class _Cancelled(BaseException):
    """SIGTERM/SIGINT/SIGHUP reached `swarm-lock` while it waited for a slot: leave without running the command."""

    def __init__(self, signum: int):
        super().__init__(signum)
        self.signum = signum


def descendants(pid: int) -> list[int]:
    """Every live descendant of `pid` (children first), from one `ps` snapshot."""
    try:
        r = subprocess.run(["ps", "-A", "-o", "pid=,ppid="], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return []
    kids: dict[int, list[int]] = {}
    for line in r.stdout.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            kids.setdefault(int(parts[1]), []).append(int(parts[0]))
    out, todo = [], [pid]
    while todo:
        for c in kids.get(todo.pop(0), []):
            if c not in out and c != pid:
                out.append(c)
                todo.append(c)
    return out


def stop_tree(pid: int, *, grace_s: float = 10.0, include_root: bool = True) -> list[int]:
    """SIGTERM `pid` and its descendants, then SIGKILL whatever is still alive after `grace_s`. Returns the pids
    signalled. The tree is listed before the root goes, so grandchildren reparented to init are still found."""
    import signal
    tree = descendants(pid)
    targets = ([pid] if include_root else []) + tree
    for t in targets:
        with contextlib.suppress(OSError):
            os.kill(t, signal.SIGTERM)
    end = time.monotonic() + grace_s
    while time.monotonic() < end and any(_alive(t) and not _zombie(t) for t in targets):
        time.sleep(0.1)
    for t in targets:
        if _alive(t) and not _zombie(t):
            with contextlib.suppress(OSError):
                os.kill(t, signal.SIGKILL)
    return targets


def _zombie(pid: int) -> bool:
    try:
        r = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return False
    return r.stdout.strip().startswith("Z")


def _cmdline(pid: int) -> str:
    try:
        r = subprocess.run(["ps", "-o", "command=", "-p", str(int(pid))], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError, ValueError):
        return ""
    return r.stdout.strip() if r.returncode == 0 else ""


def task_lock_jobs(directory: Path | str, task_id: str) -> list[dict]:
    """Live `swarm-lock` processes (holding slots or queued for the exclusive lock) started for `task_id`. Only
    processes whose command line runs swarm.hostlock count: a harness verify inside `swarm serve` or `swarm run`
    is never one of them."""
    directory = Path(directory)
    out, seen = [], set()
    files = sorted(directory.glob("verify-*.holder")) + sorted(directory.glob(PENDING_PREFIX + "*"))
    for f in files:
        h = _read_json(f)
        if not h or not task_id or h.get("task") != task_id or not isinstance(h.get("pid"), int):
            continue
        if h["pid"] == os.getpid() or h["pid"] in seen or not _entry_alive(h):
            continue
        if "swarm.hostlock" not in _cmdline(h["pid"]):
            continue
        seen.add(h["pid"])
        out.append(h)
    return out


def stop_task_lock_jobs(directory: Path | str, task_id: str, *, grace_s: float = 10.0) -> list[str]:
    """Stop a task's earlier `swarm-lock` jobs and their command trees (Q-521: a previous attempt's detached
    measurement held the exclusive lock 20+ min after its worktree was reset). One line per job stopped."""
    lines = []
    for h in task_lock_jobs(directory, task_id):
        tree = stop_tree(h["pid"], grace_s=grace_s)
        lines.append(f"{describe([h])}; stopped pids {', '.join(str(t) for t in tree)}")
    return lines


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


def worker_lock_env(project: str, slots: int, exclusive_cap_min: float | None = None) -> dict[str, str]:
    """Environment a worker CLI gets so its verify scripts (and `swarm-lock`) share the harness's slots."""
    directory = lock_dir(project)
    try:
        ensure_lock_files(directory, slots)
        wrapper = write_wrapper(directory.parent / "bin")
    except OSError:
        return {}
    env = {DIR_ENV: str(directory), SLOTS_ENV: str(slots), CMD_ENV: str(wrapper),
           "PATH": os.pathsep.join([str(wrapper.parent), os.environ.get("PATH", "")])}
    if exclusive_cap_min is not None:
        env[EXCL_CAP_ENV] = f"{exclusive_cap_min:g}"
    return env


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="swarm-lock", description=__doc__.split("\n\n")[0])
    ap.add_argument("--exclusive", action="store_true", help="take every slot (latency measurements, live replays)")
    ap.add_argument("--dir", default=os.environ.get(DIR_ENV, ""))
    ap.add_argument("--slots", type=int, default=int(os.environ.get(SLOTS_ENV) or DEFAULT_SLOTS))
    ap.add_argument("--wait", type=float, default=DEFAULT_WAIT_S, help="seconds to wait before running anyway")
    ap.add_argument("--exclusive-max-minutes", type=float,
                    default=float(os.environ.get(EXCL_CAP_ENV) or DEFAULT_EXCL_CAP_MIN),
                    help="a normal verify queued behind exclusive holders this long proceeds (0 = never)")
    ap.add_argument("--ignore-hold", action="store_true",
                    help="do not wait for a live-test hold (`swarm hold`); SWARM_HOLD_BYPASS=1 does the same")
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
    import signal
    # Q-523: a queued swarm-lock that was killed must not start its command later, and a running one must take its
    # command tree with it (a detached batch kept the machine busy next to the restarted one). While waiting, the
    # signal raises _Cancelled (the slot code's finally removes the pending file); once the command runs, the
    # signal is passed on to the command's whole process tree. SIGHUP stays ignored under nohup.
    child: dict[str, subprocess.Popen | None] = {"p": None}
    stopping = {"sig": 0}

    def on_signal(signum, _frame):
        if child["p"] is None:
            raise _Cancelled(signum)
        if not stopping["sig"]:
            stopping["sig"] = signum
            stop_tree(child["p"].pid, grace_s=10.0)

    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        with contextlib.suppress(ValueError, OSError):
            if signal.getsignal(sig) is not signal.SIG_IGN:
                signal.signal(sig, on_signal)
    try:
        with verify_slot(a.dir, a.slots, exclusive=a.exclusive, wait_s=a.wait, log=log, label=label,
                         record=record, honor_hold=not a.ignore_hold,
                         exclusive_cap_min=None if a.exclusive else a.exclusive_max_minutes) as held:
            waited = time.monotonic() - started
            if waited >= 5:
                log(f"waited {waited:.0f} s for {'the machine' if a.exclusive else 'a slot'}"
                    + (" (the runner adds this back to the run's time limit)" if record else ""))
            env = {**os.environ, HELD_ENV: "1"} if held else dict(os.environ)
            try:
                child["p"] = subprocess.Popen(cmd, env=env)
            except FileNotFoundError:
                log(f"command not found: {cmd[0]}")
                return 127
            while True:
                try:
                    rc = child["p"].wait()
                    break
                except _Cancelled:      # raced: the signal came between Popen and child["p"] being set
                    stop_tree(child["p"].pid, grace_s=10.0)
            if stopping["sig"]:
                log(f"stopped by signal {stopping['sig']}; the command's process tree was terminated")
                return 128 + stopping["sig"]
            return rc
    except _Cancelled as c:
        log(f"cancelled by signal {c.signum} while waiting for {'the machine' if a.exclusive else 'a slot'}; "
            "the command was not run")
        return 128 + c.signum


if __name__ == "__main__":
    sys.exit(main())
