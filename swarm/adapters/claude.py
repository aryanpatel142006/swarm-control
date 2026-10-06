from __future__ import annotations

import json
import os
from pathlib import Path

from ..models import RunResult, Usage
from .base import RATE_LIMIT_RE, Adapter, RunSpec, is_usage_limit, last_json_object, parse_reset_at

READ_ONLY_TOOLS = "Read,Grep,Glob,Bash(git diff:*),Bash(git log:*),Bash(git show:*),Bash(ls:*),Bash(cat:*)"


def user_mcp_servers() -> dict:
    """MCP servers configured for this laptop's Claude Code (user scope), by name."""
    servers: dict = {}
    for path in (Path.home() / ".claude.json",):
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        servers.update(data.get("mcpServers") or {})
    return servers


class ClaudeAdapter(Adapter):
    """Claude Code headless: `claude -p --output-format json` with validated structured output."""
    name = "claude"

    def __init__(self, agent_cfg=None, mcp_lookup=None):
        super().__init__(agent_cfg)
        if mcp_lookup is None:
            from ..tools import all_mcp_servers
            mcp_lookup = all_mcp_servers
        self.mcp_lookup = mcp_lookup

    def build_command(self, spec: RunSpec) -> tuple[list[str], bytes | None]:
        argv = ["claude", "-p", "--output-format", "json", "--model", spec.model,
                "--max-turns", str(spec.max_turns)]
        if spec.read_only:
            argv += ["--permission-mode", "dontAsk", "--allowedTools", READ_ONLY_TOOLS]
        else:
            argv += ["--permission-mode", "bypassPermissions"]
        if spec.effort:
            argv += ["--effort", spec.effort]
        if spec.budget_usd:
            argv += ["--max-budget-usd", str(spec.budget_usd)]
        if spec.schema:
            argv += ["--json-schema", json.dumps(spec.schema)]
        # only the MCP servers this task type asks for; every extra server adds its tool schemas to the run's context
        known = {**self.mcp_lookup(), **spec.mcp_servers} if spec.mcp else {}
        chosen = {name: known[name] for name in spec.mcp if name in known}
        argv += ["--mcp-config", json.dumps({"mcpServers": chosen}), "--strict-mcp-config"]
        for d in spec.plugin_dirs:
            argv += ["--plugin-dir", d]
        if spec.settings:
            argv += ["--settings", json.dumps(spec.settings)]
        argv += list(spec.extra_args)
        return argv, spec.prompt_file.read_bytes()

    def parse_output(self, code: int, out: str, err: str) -> RunResult:
        data = last_json_object(out)
        if not data:
            return RunResult(ok=False, exit_code=code, stdout=out, stderr=err,
                             error=f"no result json (exit {code})")
        subtype = data.get("subtype", "")
        is_error = bool(data.get("is_error")) or subtype != "success"
        u = data.get("usage") or {}
        usage = Usage(input_tokens=int(u.get("input_tokens", 0) or 0),
                      output_tokens=int(u.get("output_tokens", 0) or 0),
                      cost_usd=data.get("total_cost_usd"),
                      cache_write_tokens=int(u.get("cache_creation_input_tokens", 0) or 0),
                      cache_read_tokens=int(u.get("cache_read_input_tokens", 0) or 0))
        text = str(data.get("result") or "")
        rate_limited = is_error and bool(RATE_LIMIT_RE.search(text + "\n" + err))
        return RunResult(ok=not is_error, exit_code=code, stdout=out, stderr=err,
                         structured_output=data.get("structured_output"), usage=usage,
                         session_id=data.get("session_id"), rate_limited=rate_limited,
                         usage_limited=rate_limited and is_usage_limit(text + "\n" + err),
                         reset_at=parse_reset_at(text + "\n" + err) if rate_limited else None,
                         error="" if not is_error else f"{subtype}: {text[:300]}")
