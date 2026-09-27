from __future__ import annotations

from ..models import RunResult, Usage
from .base import Adapter, RunSpec, last_json_object


class GrokAdapter(Adapter):
    """xAI Grok Build CLI headless: `grok -p --prompt-file ... --output-format json`."""
    name = "grok"

    def build_command(self, spec: RunSpec) -> tuple[list[str], bytes | None]:
        argv = ["grok", "-p", "--prompt-file", str(spec.prompt_file), "--output-format", "json",
                "--max-turns", str(spec.max_turns), "--cwd", str(spec.cwd), "--model", spec.model]
        if not spec.read_only:
            argv += ["--always-approve"]
        argv += list(spec.extra_args)
        return argv, None

    def parse_output(self, code: int, out: str, err: str) -> RunResult:
        data = last_json_object(out) or {}
        u = data.get("usage") or {}
        usage = Usage(input_tokens=int(u.get("input_tokens", 0) or 0),
                      output_tokens=int(u.get("output_tokens", 0) or 0),
                      cost_usd=data.get("total_cost_usd"))
        return RunResult(ok=code == 0, exit_code=code, stdout=out, stderr=err, usage=usage,
                         session_id=data.get("sessionId"), error="" if code == 0 else f"exit {code}")
