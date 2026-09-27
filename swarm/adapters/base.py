"""Adapter base: RunSpec, subprocess execution with timeout, rate-limit detection."""
from __future__ import annotations

import json
import re
import subprocess
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


class Adapter:
    name = "base"

    def __init__(self, agent_cfg: AgentConfig | None = None):
        self.cfg = agent_cfg

    def build_command(self, spec: RunSpec) -> tuple[list[str], bytes | None]:
        raise NotImplementedError

    def parse_output(self, code: int, out: str, err: str) -> RunResult:
        return RunResult(ok=code == 0, exit_code=code, stdout=out, stderr=err,
                         error="" if code == 0 else f"exit {code}")

    def run(self, spec: RunSpec, runner=subprocess.run) -> RunResult:
        argv, stdin = self.build_command(spec)
        (spec.cwd / ".swarm-run").mkdir(exist_ok=True)
        try:
            proc = runner(argv, cwd=str(spec.cwd), input=stdin, capture_output=True, timeout=spec.timeout_s)
        except subprocess.TimeoutExpired as e:
            return RunResult(ok=False, exit_code=-1, stdout=(e.stdout or b"").decode(errors="replace"),
                             stderr=(e.stderr or b"").decode(errors="replace"), timed_out=True,
                             error=f"timeout after {spec.timeout_s}s")
        except FileNotFoundError as e:
            return RunResult(ok=False, exit_code=-2, stdout="", stderr=str(e), error=f"cli not found: {argv[0]}")
        out = proc.stdout.decode(errors="replace")
        err = proc.stderr.decode(errors="replace")
        result = self.parse_output(proc.returncode, out, err)
        if not result.ok and not result.rate_limited and RATE_LIMIT_RE.search(err[-4000:] + "\n" + result.error):
            result.rate_limited = True
        if result.rate_limited and result.reset_at is None:
            result.reset_at = parse_reset_at(err + "\n" + result.error + "\n" + out[-2000:])
        return result
