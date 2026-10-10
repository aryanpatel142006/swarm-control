from __future__ import annotations

import json
import subprocess
from pathlib import Path

from ..models import RunResult, Usage
from ..strict_schema import to_openai_strict
from ..workspace import run_cmd
from .base import RATE_LIMIT_RE, Adapter, RunSpec, is_auth_lost, is_usage_limit, parse_reset_at


def registered_mcp_servers(run=run_cmd) -> set[str]:
    """Names of MCP servers this laptop's Codex knows (`codex mcp list --json`); empty when unavailable."""
    r = run(["codex", "mcp", "list", "--json"], cwd=Path.cwd(), timeout=30)
    if not r.ok:
        return set()
    try:
        data = json.loads(r.out)
    except ValueError:
        return set()
    rows = data.get("servers") if isinstance(data, dict) else data
    return {str(x.get("name")) for x in (rows or []) if isinstance(x, dict) and x.get("name")}


def git_common_dir(cwd: Path) -> str | None:
    """Absolute path of the repository's shared .git directory, or None outside a git checkout."""
    try:
        r = subprocess.run(["git", "-C", str(cwd), "rev-parse", "--path-format=absolute", "--git-common-dir"],
                           capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else None


class CodexAdapter(Adapter):
    """OpenAI Codex CLI headless: `codex exec --json` with `--output-schema`. Reports tokens, not dollars."""
    name = "codex"

    def __init__(self, agent_cfg=None, mcp_lookup=registered_mcp_servers):
        super().__init__(agent_cfg)
        self.mcp_lookup = mcp_lookup

    def build_command(self, spec: RunSpec) -> tuple[list[str], bytes | None]:
        argv = ["codex", "exec", "--json", "-C", str(spec.cwd), "-m", spec.model]
        if spec.effort:
            argv += ["-c", f'model_reasoning_effort="{spec.effort}"']
        if spec.read_only:
            sandbox = "read-only"
        else:
            sandbox = spec.sandbox or (self.cfg.sandbox if self.cfg else None) or "workspace-write"
        argv += ["--sandbox", sandbox]
        common = git_common_dir(spec.cwd)
        if common and sandbox != "danger-full-access":
            argv += ["--add-dir", common]   # a worktree's index and locks live under the main repo's .git
            argv += ["--add-dir", str(Path(common).parent)]   # and the shared .venv lives in the main checkout (Q-003)
        if spec.mcp:
            # servers are registered once with `codex mcp add` (enabled = false) and switched on per run;
            # an unregistered name is skipped, never passed: a dangling entry would fail the whole run
            known = self.mcp_lookup()
            for name in spec.mcp:
                if name in known:
                    argv += ["-c", f"mcp_servers.{name}.enabled=true"]
        if spec.schema:
            schema_path = spec.cwd / ".swarm-run" / "schema.json"
            schema_path.parent.mkdir(exist_ok=True)
            # newer CLIs send this to OpenAI structured outputs in strict mode (codex-c, Oct 10); older ones accept it too
            schema_path.write_text(json.dumps(to_openai_strict(spec.schema)))
            argv += ["--output-schema", str(schema_path)]
        argv += list(spec.extra_args) + ["-"]
        return argv, spec.prompt_file.read_bytes()

    def parse_output(self, code: int, out: str, err: str) -> RunResult:
        session, last_text, failed, error = None, None, False, ""
        errors: list[str] = []   # every error the CLI reported: a usage limit is often followed by a retry's 401
        usage = Usage()
        for line in out.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            t = ev.get("type", "")
            if t == "thread.started":
                session = ev.get("thread_id")
            elif t == "item.completed" and (ev.get("item") or {}).get("type") == "agent_message":
                last_text = ev["item"].get("text")
            elif t.startswith("item.") and (ev.get("item") or {}).get("type") == "error":
                errors.append(str(ev["item"].get("message") or ev["item"].get("text") or ""))
            elif t == "turn.completed":
                u = ev.get("usage") or {}
                usage.input_tokens += int(u.get("input_tokens", 0) or 0)
                usage.output_tokens += int(u.get("output_tokens", 0) or 0)
            elif t in ("turn.failed", "error"):
                failed = True
                e = ev.get("error")
                error = str((e.get("message") if isinstance(e, dict) else e) or ev.get("message") or t)
                errors.append(error)
        structured = None
        if last_text and last_text.strip().startswith("{"):
            try:
                structured = json.loads(last_text)
            except ValueError:
                structured = None
        ok = code == 0 and not failed
        if not ok and not error:
            error = f"exit {code}"
        said = "\n".join(dict.fromkeys(errors + [error, err[-4000:]]))
        rate_limited = (not ok) and bool(RATE_LIMIT_RE.search(said))
        auth = (not ok) and not rate_limited and is_auth_lost(said)
        if rate_limited and not RATE_LIMIT_RE.search(error):
            error = next((e for e in errors if RATE_LIMIT_RE.search(e)), error)   # name the limit, not the 401 after it
        return RunResult(ok=ok, exit_code=code, stdout=out, stderr=err, structured_output=structured,
                         usage=usage, session_id=session, rate_limited=rate_limited,
                         usage_limited=rate_limited and is_usage_limit(said),
                         reset_at=parse_reset_at(said) if rate_limited else None, auth_lost=auth, error=error)
