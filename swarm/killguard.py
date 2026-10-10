"""`pkill` / `killall` for workers: signal only the calling task's own processes.

Oct 10 2026 (selective-hearing): three `swarm-lock --exclusive` measurements of other tasks were killed "from outside"
by workers cleaning up their own jobs with pattern kills. Every worktree names its batch `.swarm-run/batch.sh`:
- 16:41 EDT, T-368 ran `pkill -f "bash .swarm-run/batch.sh"`; it also matched the queued `swarm-lock --exclusive --
  bash .swarm-run/batch.sh` of T-370 and T-371 ("cancelled by signal 15 while waiting for the machine");
- 17:09 EDT, T-370 ran `pkill -TERM -f "bash .swarm-run/batch.sh"`; it matched T-368's running measurement, which
  stopped after 26 of 40 rows.
Serve and runner restarts at the same time were a coincidence: neither signals `swarm-lock` processes.

The runner puts this guard on the worker's PATH (next to `swarm-lock`) as `pkill` and `killall`. With
`SWARM_TASK_ID` set, it lists the matches with the real `pgrep` and signals only processes whose environment carries
the same `SWARM_TASK_ID` (every process a worker starts inherits it, nohup'd or not; where macOS hides a platform
binary's environment, a working directory inside `$SWARM_WORKTREE` counts instead). Matches that belong to another
task, the harness or a human are left alone and named on stderr. Without `SWARM_TASK_ID` it is the real command.
"""
from __future__ import annotations

import os
import shutil
import signal as _signal
import subprocess
import sys
from pathlib import Path

TASK_ENV = "SWARM_TASK_ID"
GUARDED = ("pkill", "killall")
NO_MATCH_RC = 1          # pkill/killall: no process matched (or none of this task's)
USAGE_RC = 2


def real_binary(name: str, skip_dir: Path | str | None = None) -> str:
    """The system `name`, never a guard in `skip_dir` (the guard's own bin dir is first on the worker's PATH)."""
    skip = str(Path(skip_dir).resolve()) if skip_dir else ""
    dirs = [d for d in os.environ.get("PATH", "").split(os.pathsep) if d]
    for d in dirs + ["/usr/bin", "/bin", "/usr/sbin", "/sbin"]:
        try:
            if skip and str(Path(d).resolve()) == skip:
                continue
        except OSError:
            continue
        p = shutil.which(name, path=d)
        if p and not _is_guard(p):
            return p
    return f"/usr/bin/{name}"


def _is_guard(path: str) -> bool:
    try:
        with open(path, "rb") as f:
            return b"swarm/killguard.py" in f.read(400)
    except OSError:
        return False


def task_of(pid: int) -> str | None:
    """`SWARM_TASK_ID` in the environment of `pid`, or None (none set, gone, or not readable)."""
    environ = Path(f"/proc/{pid}/environ")
    if environ.exists():
        try:
            for item in environ.read_bytes().split(b"\0"):
                if item.startswith(TASK_ENV.encode() + b"="):
                    return item.split(b"=", 1)[1].decode("utf-8", "replace")
            return None
        except OSError:
            return None
    try:   # macOS: `ps -E` appends the environment to the command line (same user only)
        r = subprocess.run(["ps", "-E", "-ww", "-o", "command=", "-p", str(int(pid))],
                           capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    if r.returncode != 0:
        return None
    found = None
    for tok in r.stdout.split():
        if tok.startswith(TASK_ENV + "="):
            found = tok.split("=", 1)[1]        # the last one is the environment's (an argument may repeat it)
    return found


def cwd_of(pid: int) -> str | None:
    """The working directory of `pid` (lsof), or None."""
    try:
        r = subprocess.run(["lsof", "-a", "-p", str(int(pid)), "-d", "cwd", "-Fn"], capture_output=True, text=True,
                           timeout=10)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    for line in r.stdout.splitlines():
        if line.startswith("n/"):
            return line[1:]
    return None


def _inside(path: str, root: str) -> bool:
    try:
        p, r = Path(path).resolve(), Path(root).resolve()
    except OSError:
        return False
    return p == r or r in p.parents


def owner(pid: int, task: str, worktree: str = "") -> tuple[bool, str]:
    """(is it `task`'s process, who owns it in words). The environment decides; when it cannot be read (macOS hides
    the environment of /bin/bash, /bin/sleep and other platform binaries) the working directory does: inside the
    task's worktree is the task's, anywhere else is not."""
    t = task_of(pid)
    if t is not None:
        return t == task, f"task {t}"
    cwd = cwd_of(pid)
    if cwd and worktree and _inside(cwd, worktree):
        return True, f"task {task} (cwd {cwd})"
    return False, (f"cwd {cwd}" if cwd else "not a task: harness, human or system")


def _cmdline(pid: int) -> str:
    try:
        r = subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return ""
    return r.stdout.strip()


def _ancestors() -> set[int]:
    """This process and its parents: a pattern that matches the calling shell's own command line must not kill it."""
    out, pid = {os.getpid()}, os.getppid()
    for _ in range(64):
        if pid <= 1 or pid in out:
            break
        out.add(pid)
        try:
            r = subprocess.run(["ps", "-o", "ppid=", "-p", str(pid)], capture_output=True, text=True, timeout=5)
            pid = int(r.stdout.strip() or 0)
        except (OSError, subprocess.SubprocessError, ValueError):
            break
    return out


def parse_signal(tok: str) -> int | None:
    """`9`, `KILL`, `SIGKILL`, `kill` → the signal number; None when `tok` is not a signal."""
    t = tok.strip()
    if t.isdigit():
        return int(t)
    name = t.upper()
    name = name if name.startswith("SIG") else "SIG" + name
    sig = getattr(_signal, name, None)
    return int(sig) if isinstance(sig, _signal.Signals) else None


def split_pkill_args(argv: list[str]) -> tuple[int, list[str]]:
    """(signal, the pgrep arguments) of a pkill command line: `-9`, `-TERM`, `-SIGTERM` first, or `--signal X`."""
    sig, rest, i = int(_signal.SIGTERM), [], 0
    while i < len(argv):
        a = argv[i]
        if a == "--signal" and i + 1 < len(argv) and parse_signal(argv[i + 1]) is not None:
            sig, i = parse_signal(argv[i + 1]), i + 2
            continue
        if a.startswith("--signal=") and parse_signal(a.split("=", 1)[1]) is not None:
            sig, i = parse_signal(a.split("=", 1)[1]), i + 1
            continue
        if i == 0 and a.startswith("-") and not a.startswith("--") and parse_signal(a[1:]) is not None:
            sig, i = parse_signal(a[1:]), i + 1          # `-9`, `-TERM`, `-SIGKILL` (only first, as pkill reads it)
            continue
        rest.append(a)
        i += 1
    return sig, rest


def split_killall_args(argv: list[str]) -> tuple[int, list[str], bool] | str:
    """(signal, process names, regex) of a killall command line, or an error text for options this guard does not
    take (it refuses rather than guess)."""
    sig, names, regex, i = int(_signal.SIGTERM), [], False, 0
    while i < len(argv):
        a = argv[i]
        if a in ("-s", "--signal") and i + 1 < len(argv) and parse_signal(argv[i + 1]) is not None:
            sig, i = parse_signal(argv[i + 1]), i + 2
            continue
        if a == "-m":
            regex = True
        elif a in ("-q", "-v", "--quiet", "--verbose"):
            pass
        elif a == "--":
            names.extend(argv[i + 1:])
            break
        elif a.startswith("-") and len(a) > 1:
            s = parse_signal(a[1:])
            if s is None:
                return (f"killall option {a} is not supported for workers; kill your own jobs by PID "
                        "(`kill <pid>`, the PID from `$!`) or with `pkill -f <pattern>`")
            sig = s
        else:
            names.append(a)
        i += 1
    if not names:
        return "killall: no process name given"
    return sig, names, regex


def guarded_kill(pids: list[int], sig: int, task: str, *, worktree: str = "", err=sys.stderr) -> int:
    """Signal the pids of `task`; name the others on stderr. pkill's exit codes: 0 = one or more signalled."""
    skip = _ancestors()
    mine, others = [], []
    for pid in pids:
        if pid in skip:
            continue
        ok, who = owner(pid, task, worktree)
        (mine if ok else others).append((pid, who))
    for pid, _ in mine:
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            pass
        except PermissionError as e:
            print(f"pkill (swarm guard): pid {pid}: {e}", file=err)
    if others:
        lines = [f"  pid {pid} ({who}): {_cmdline(pid)[:140]}" for pid, who in others[:10]]
        print(f"pkill/killall (swarm guard): left {len(others)} matching process(es) alone because they are not "
              f"{task}'s (another task's measurement, the harness or a human). Stop your own jobs by PID "
              "(`kill <pid>`, the PID from `$!`) or `swarm-lock --stop`.\n" + "\n".join(lines), file=err)
    return 0 if mine else NO_MATCH_RC


def main(name: str, argv: list[str] | None = None, *, bin_dir: str | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    real = real_binary(name, bin_dir)
    task = os.environ.get(TASK_ENV, "")
    if not task:
        return subprocess.call([real, *argv])
    pgrep = real_binary("pgrep", bin_dir)
    if name == "pkill":
        if any(a in ("-h", "--help", "-V", "--version") for a in argv):
            return subprocess.call([real, *argv])
        sig, rest = split_pkill_args(argv)
        r = subprocess.run([pgrep, *rest], capture_output=True, text=True)
        if r.returncode not in (0, 1):
            sys.stderr.write(r.stderr.replace("pgrep", "pkill"))
            return r.returncode
        pids = [int(line.split()[0]) for line in r.stdout.splitlines() if line.split() and line.split()[0].isdigit()]
        return guarded_kill(pids, sig, task, worktree=os.environ.get("SWARM_WORKTREE", ""))
    parsed = split_killall_args(argv)
    if isinstance(parsed, str):
        print(parsed, file=sys.stderr)
        return USAGE_RC
    sig, names, regex = parsed
    pids: list[int] = []
    for n in names:
        r = subprocess.run([pgrep, n] if regex else [pgrep, "-x", n], capture_output=True, text=True)
        pids += [int(x) for x in r.stdout.split() if x.isdigit() and int(x) not in pids]
    return guarded_kill(pids, sig, task, worktree=os.environ.get("SWARM_WORKTREE", ""))


def write_guards(bin_dir: Path, python: str | None = None) -> list[Path]:
    """`<bin_dir>/pkill` and `<bin_dir>/killall`: shell wrappers that run this module under a task and the system
    command otherwise."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    pkg_root = str(Path(__file__).resolve().parent.parent)
    out = []
    for name in GUARDED:
        real = real_binary(name, bin_dir)
        code = (f"import sys; sys.path.insert(0, {pkg_root!r}); from swarm.killguard import main; "
                f"sys.exit(main({name!r}, bin_dir={str(bin_dir)!r}))")
        body = (f"#!/bin/sh\n# written by swarm-control (swarm/killguard.py): under a swarm task, {name} signals only "
                "that task's own processes\n"
                f"[ -n \"${{{TASK_ENV}:-}}\" ] || exec '{real}' \"$@\"\n"
                f"exec '{python or sys.executable}' -c \"{code}\" \"$@\"\n")
        path = bin_dir / name
        if not path.exists() or path.read_text() != body:
            tmp = path.with_suffix(".tmp")
            tmp.write_text(body)
            tmp.chmod(0o755)
            tmp.replace(path)
        out.append(path)
    return out


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[0]).name if Path(sys.argv[0]).name in GUARDED else "pkill"))
