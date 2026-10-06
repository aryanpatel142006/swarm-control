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

CLI: `python -m swarm.hostlock [--exclusive] [--dir D] [--slots N] [--wait S] -- cmd args…` (exit code = cmd's).
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
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


@contextlib.contextmanager
def verify_slot(directory: Path | str, slots: int = DEFAULT_SLOTS, *, exclusive: bool = False,
                wait_s: float = DEFAULT_WAIT_S, poll_s: float = 1.0, log: Callable[[str], None] | None = None,
                sleep: Callable[[float], None] = time.sleep, label: str = "verify") -> Iterator[bool]:
    """Hold one slot (or all of them with exclusive=True). Yields True when held, False when the wait ran out and
    the caller proceeds without one. Already inside a slot (HELD_ENV set): yields True at once."""
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
    deadline = time.monotonic() + wait_s
    announced = False
    try:
        if exclusive:
            for i in range(slots):
                fd = _open(directory / f"verify-{i}.lock")
                fds.append(fd)
                while True:
                    try:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if time.monotonic() > deadline:
                            raise TimeoutError
                        if not announced and log:
                            log(f"{label}: waiting for the machine to be free of verify runs (exclusive, {slots} slots)")
                            announced = True
                        sleep(poll_s)
            yield True
            return
        while True:
            for i in range(slots):
                fd = _open(directory / f"verify-{i}.lock")
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    fds.append(fd)
                    break
                except BlockingIOError:
                    os.close(fd)
            if fds:
                break
            if time.monotonic() > deadline:
                raise TimeoutError
            if not announced and log:
                log(f"{label}: all {slots} verify slots on this machine are busy; waiting")
                announced = True
            sleep(poll_s)
        yield True
    except TimeoutError:
        if log:
            log(f"{label}: no verify slot after {int(wait_s)} s; running without one")
        for fd in fds:
            os.close(fd)
        fds = []
        yield False
    finally:
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
    ap.add_argument("cmd", nargs=argparse.REMAINDER)
    a = ap.parse_args(argv)
    cmd = a.cmd[1:] if a.cmd[:1] == ["--"] else a.cmd
    if not cmd:
        ap.error("no command given (swarm-lock [--exclusive] -- <cmd> …)")
    if not a.dir:   # not under a swarm runner: nothing to coordinate with
        return subprocess.call(cmd)
    log = lambda m: print(f"[swarm-lock] {m}", file=sys.stderr, flush=True)   # noqa: E731
    started = time.monotonic()
    with verify_slot(a.dir, a.slots, exclusive=a.exclusive, wait_s=a.wait, log=log,
                     label="measurement" if a.exclusive else "verify") as held:
        waited = time.monotonic() - started
        if waited >= 5:
            log(f"waited {waited:.0f} s for {'the machine' if a.exclusive else 'a slot'}")
        env = {**os.environ, HELD_ENV: "1"} if held else dict(os.environ)
        try:
            return subprocess.call(cmd, env=env)
        except FileNotFoundError:
            log(f"command not found: {cmd[0]}")
            return 127


if __name__ == "__main__":
    sys.exit(main())
