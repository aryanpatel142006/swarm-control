from __future__ import annotations

from ..models import RunResult, Usage
from .base import Adapter, RunSpec, last_json_object


class AntigravityAdapter(Adapter):
    """Google Antigravity CLI (`agy`). Flags are third-party sourced; verify with `swarm doctor --smoke`."""
    name = "antigravity"

    def build_command(self, spec: RunSpec) -> tuple[list[str], bytes | None]:
        argv = ["agy", "-p", spec.prompt_file.read_text(), f"--model={spec.model}", "--output-format", "json"]
        if not spec.read_only:
            argv += ["--approval-mode", "yolo"]
        argv += list(spec.extra_args)
        return argv, None

    def parse_output(self, code: int, out: str, err: str) -> RunResult:
        data = last_json_object(out) or {}
        error = str(data.get("error") or ("" if code == 0 else f"exit {code}"))
        return RunResult(ok=code == 0 and not data.get("error"), exit_code=code, stdout=out, stderr=err,
                         structured_output=None, usage=Usage(), error=error)
