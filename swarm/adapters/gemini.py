from __future__ import annotations

from ..models import RunResult, Usage
from .base import Adapter, RunSpec, last_json_object


class GeminiAdapter(Adapter):
    """Legacy Gemini CLI with an API key (personal accounts moved to Antigravity in June 2026)."""
    name = "gemini"

    def build_command(self, spec: RunSpec) -> tuple[list[str], bytes | None]:
        argv = ["gemini", "-p", spec.prompt_file.read_text(), "--output-format", "json", "-m", spec.model]
        if not spec.read_only:
            argv += ["--approval-mode", "yolo"]
        argv += list(spec.extra_args)
        return argv, None

    def parse_output(self, code: int, out: str, err: str) -> RunResult:
        data = last_json_object(out) or {}
        error = str(data.get("error") or ("" if code == 0 else f"exit {code}"))
        return RunResult(ok=code == 0 and not data.get("error"), exit_code=code, stdout=out, stderr=err,
                         usage=Usage(), error=error)
