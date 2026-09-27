from __future__ import annotations

import json
import subprocess
from pathlib import Path

from ..models import RunResult, Usage
from .base import RATE_LIMIT_RE, Adapter, RunSpec, parse_reset_at


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
        if spec.schema:
            schema_path = spec.cwd / ".swarm-run" / "schema.json"
            schema_path.parent.mkdir(exist_ok=True)
            schema_path.write_text(json.dumps(spec.schema))
            argv += ["--output-schema", str(schema_path)]
        argv += list(spec.extra_args) + ["-"]
        return argv, spec.prompt_file.read_bytes()

    def parse_output(self, code: int, out: str, err: str) -> RunResult:
        session, last_text, failed, error = None, None, False, ""
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
            elif t == "turn.completed":
                u = ev.get("usage") or {}
                usage.input_tokens += int(u.get("input_tokens", 0) or 0)
                usage.output_tokens += int(u.get("output_tokens", 0) or 0)
            elif t in ("turn.failed", "error"):
                failed = True
                error = str((ev.get("error") or {}).get("message") or ev.get("message") or t)
        structured = None
        if last_text and last_text.strip().startswith("{"):
            try:
                structured = json.loads(last_text)
            except ValueError:
                structured = None
        ok = code == 0 and not failed
        if not ok and not error:
            error = f"exit {code}"
        rate_limited = (not ok) and bool(RATE_LIMIT_RE.search(error + "\n" + err))
        return RunResult(ok=ok, exit_code=code, stdout=out, stderr=err, structured_output=structured,
                         usage=usage, session_id=session, rate_limited=rate_limited,
                         reset_at=parse_reset_at(error + err) if rate_limited else None, error=error)
