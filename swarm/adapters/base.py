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
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..config import AgentConfig
from ..models import RunResult

RATE_LIMIT_RE = re.compile(
    r"rate.?limit|too many requests|\b429\b|usage limit|hit your .{0,40}limit|quota exceeded|plan limit|"
    r"(?:hour|daily|weekly|session) limit reached|credit balance is too low|resource.?exhausted|overloaded|capacity",
    re.I)
# A plan that is used up (ChatGPT/Codex "You've hit your usage limit. Upgrade to Pro ... try again at 5:03 AM",
# Claude "usage limit reached", "5-hour limit reached · resets 3am") is not a short rate limit: retrying in 15 minutes
# fails the same way. codex-b got two more tasks that way on Oct 6 2026.
USAGE_LIMIT_RE = re.compile(
    r"usage limit|hit your .{0,40}limit|quota exceeded|plan limit|(?:hour|daily|weekly|session) limit reached|"
    r"upgrade to (?:pro|plus|max)|purchase more credits|credit balance is too low", re.I)
_RESET_RE = re.compile(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2}))")
_EPOCH_RE = re.compile(r"limit reached\|(\d{10})\b")
_CLOCK_RE = re.compile(
    r"(?:try again at|tries? again at|resets?(?: at)?|reset at|available again at|until)\s+"
    r"(\d{1,2})(?::(\d{2}))?\s*(?:([ap])\.?\s?m\.?\b)?(?:\s*\(([A-Za-z_]+(?:/[A-Za-z_+-]+)+)\))?", re.I)
_IN_RE = re.compile(
    r"(?:try again|retry|resets?|available again)(?: in| after)\s+(?:(\d+)\s*(?:h|hours?|hrs?)\b)?\s*,?\s*(?:and\s+)?"
    r"(?:(\d+)\s*(?:m|min|mins|minutes?)\b)?", re.I)


def parse_reset_at(text: str, now: datetime | None = None) -> datetime | None:
    """When a limit resets, from the CLI's message: an ISO timestamp, an epoch (`limit reached|1759999999`), a
    wall-clock time (`try again at 5:03 AM`, `resets 3am (America/New_York)`: the runner's local time, which is
    the CLI's, unless a zone is named) or a duration (`try again in 2 hours 13 minutes`). Always UTC, in the future."""
    text = text or ""
    m = _RESET_RE.search(text)
    if m:
        dt = datetime.fromisoformat(m.group(1).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    m = _EPOCH_RE.search(text)
    if m:
        return datetime.fromtimestamp(int(m.group(1)), tz=timezone.utc)
    for m in _CLOCK_RE.finditer(text):
        hour, minute, ampm, zone = int(m.group(1)), int(m.group(2) or 0), (m.group(3) or "").lower(), m.group(4)
        if not ampm and m.group(2) is None:
            continue   # a bare number ("until 3") is not a time
        if ampm:
            if not 1 <= hour <= 12:
                continue
            hour = hour % 12 + (12 if ampm == "p" else 0)
        if hour > 23 or minute > 59:
            continue
        tz = None
        if zone:
            try:
                from zoneinfo import ZoneInfo
                tz = ZoneInfo(zone)
            except Exception:   # noqa: BLE001 - unknown zone name: fall back to local time
                tz = None
        local_now = now.astimezone(tz) if tz else now.astimezone()
        at = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if at <= local_now:
            at += timedelta(days=1)
        return at.astimezone(timezone.utc)
    m = _IN_RE.search(text)
    if m and (m.group(1) or m.group(2)):
        return now + timedelta(hours=int(m.group(1) or 0), minutes=int(m.group(2) or 0))
    return None


def is_usage_limit(text: str) -> bool:
    return bool(USAGE_LIMIT_RE.search(text or ""))


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
        if result.rate_limited:
            text = result.error + "\n" + err[-4000:] + "\n" + out[-2000:]
            result.usage_limited = result.usage_limited or is_usage_limit(text)
            if result.reset_at is None:
                result.reset_at = parse_reset_at(text)
        return result
