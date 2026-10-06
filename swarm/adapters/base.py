"""Adapter base: RunSpec, subprocess execution with timeout, rate-limit detection."""
from __future__ import annotations

from typing import Callable

import json
import re
import subprocess
import signal
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from ..config import AgentConfig
from ..models import RunResult

RATE_LIMIT_RE = re.compile(
    r"rate.?limit|too many requests|\b429\b|usage limit|hit your .{0,40}limit|quota exceeded|"
    r"resource.?exhausted|overloaded|capacity", re.I)
_RESET_RE = re.compile(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2}))")


def parse_reset_at(text: str) -> datetime | None:
    m = _RESET_RE.search(text or "")
    if not m:
        return None
    dt = datetime.fromisoformat(m.group(1).replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def last_json_object(text: str) -> dict | None:
    text = (text or "").strip()
    if not text:
        return None
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else None
    except ValueError:
        pass
    for line in reversed(text.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                data = json.loads(line)
                if isinstance(data, dict):
                    return data
            except ValueError:
                continue
    return None


@dataclass
class RunSpec:
    prompt_file: Path
    model: str
    effort: str | None
    max_turns: int
    budget_usd: float | None
    timeout_s: int
    cwd: Path
    schema: dict | None = None
    read_only: bool = False
    sandbox: str | None = None
    extra_args: list[str] = field(default_factory=list)
    mcp: list[str] = field(default_factory=list)      # MCP server names to enable for this run
    plugin_dirs: list[str] = field(default_factory=list)  # plugins loaded for this run only (Claude --plugin-dir)
    mcp_servers: dict = field(default_factory=dict)   # inline server definitions (config mcp_servers) by name
    settings: dict | None = None                      # per-run settings override (Claude --settings)
    should_stop: Callable[[], bool] | None = None     # polled while the CLI runs; True kills it (claim lost)
    stop_poll_s: float = 5.0
    claim_nonce: str = ""                              # the claim this run holds on the board (for logs and tests)
    env: dict = field(default_factory=dict)            # extra environment for this run (PYTHONPATH, SWARM_TASK_ID)


class Adapter:
    name = "base"

    def __init__(self, agent_cfg: AgentConfig | None = None):
        self.cfg = agent_cfg

    def build_command(self, spec: RunSpec) -> tuple[list[str], bytes | None]:
        raise NotImplementedError

    def parse_output(self, code: int, out: str, err: str) -> RunResult:
        return RunResult(ok=code == 0, exit_code=code, stdout=out, stderr=err,
                         error="" if code == 0 else f"exit {code}")

    @staticmethod
    def _run_watched(argv: list[str], spec: RunSpec, stdin: bytes | None, env: dict | None = None) -> subprocess.CompletedProcess:
        """Like subprocess.run, but polls spec.should_stop and kills the CLI when it says so (exit -3)."""
        proc = subprocess.Popen(argv, cwd=str(spec.cwd), env=env, stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)

        def stop(sig):   # the CLI spawns children (shells, servers); signal the whole process group
            try:
                os.killpg(proc.pid, sig)
            except (ProcessLookupError, PermissionError):
                pass
        if stdin is not None:
            try:
                proc.stdin.write(stdin)
                proc.stdin.close()
            except (BrokenPipeError, OSError):
                pass
        deadline = time.monotonic() + spec.timeout_s
        import threading
        chunks = {"out": b"", "err": b""}

        def drain(stream, key):
            chunks[key] = stream.read()
        readers = [threading.Thread(target=drain, args=(proc.stdout, "out"), daemon=True),
                   threading.Thread(target=drain, args=(proc.stderr, "err"), daemon=True)]
        for th in readers:
            th.start()
        stopped = False
        while proc.poll() is None:
            if time.monotonic() > deadline:
                stop(signal.SIGKILL)
                proc.wait()
                for th in readers:
                    th.join(timeout=5)
                raise subprocess.TimeoutExpired(argv, spec.timeout_s, output=chunks["out"], stderr=chunks["err"])
            if spec.should_stop():
                stopped = True
                stop(signal.SIGTERM)
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    stop(signal.SIGKILL)
                    proc.wait()
                break
            time.sleep(spec.stop_poll_s)
        for th in readers:
            th.join(timeout=5)
        code = -3 if stopped else proc.returncode
        return subprocess.CompletedProcess(argv, code, stdout=chunks["out"], stderr=chunks["err"])

    def run(self, spec: RunSpec, runner=subprocess.run, sleep=time.sleep) -> RunResult:
        argv, stdin = self.build_command(spec)
        (spec.cwd / ".swarm-run").mkdir(exist_ok=True)
        for attempt in (1, 2):
            try:
                env = {**os.environ, **(getattr(self.cfg, "env", None) or {}), **(spec.env or {})}
                if spec.should_stop is not None:
                    proc = self._run_watched(argv, spec, stdin, env=env)
                else:
                    proc = runner(argv, cwd=str(spec.cwd), input=stdin, capture_output=True, timeout=spec.timeout_s, env=env)
                break
            except subprocess.TimeoutExpired as e:
                return RunResult(ok=False, exit_code=-1, stdout=(e.stdout or b"").decode(errors="replace"),
                                 stderr=(e.stderr or b"").decode(errors="replace"), timed_out=True,
                                 error=f"timeout after {spec.timeout_s}s")
            except FileNotFoundError as e:
                # Claude Code swaps its own binary during auto-update; one retry covers that window
                if attempt == 1:
                    sleep(5)
                    continue
                return RunResult(ok=False, exit_code=-2, stdout="", stderr=str(e), error=f"cli not found: {argv[0]}")
        out = proc.stdout.decode(errors="replace")
        err = proc.stderr.decode(errors="replace")
        if proc.returncode == -3:
            return RunResult(ok=False, exit_code=-3, stdout=out, stderr=err,
                             error="stopped: the task was handed to another agent while this run was in progress")
        result = self.parse_output(proc.returncode, out, err)
        if not result.ok and not result.rate_limited and RATE_LIMIT_RE.search(err[-4000:] + "\n" + result.error):
            result.rate_limited = True
        if result.rate_limited and result.reset_at is None:
            result.reset_at = parse_reset_at(err + "\n" + result.error + "\n" + out[-2000:])
        return result
