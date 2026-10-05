from __future__ import annotations

import json

from ..models import RunResult, Usage
from .base import Adapter, RunSpec, last_json_object


class AntigravityAdapter(Adapter):
    """Google Antigravity CLI (`agy`, brew install --cask antigravity-cli). This is where Google AI Pro / personal
    accounts went in 2026 (the Gemini CLI refuses them). Flags checked against agy 1.2.12 on Sep 28 2026."""
    name = "antigravity"

    def build_command(self, spec: RunSpec) -> tuple[list[str], bytes | None]:
        argv = ["agy", "-p", spec.prompt_file.read_text(), "--output-format", "json", "--model", spec.model,
                "--print-timeout", f"{int(spec.timeout_s)}s"]
        if spec.effort and not spec.model.endswith(("-high", "-medium", "-low")):
            argv += ["--effort", spec.effort]   # agy refuses --effort when the model id already carries it
        if not spec.read_only:
            argv += ["--dangerously-skip-permissions"]
            if spec.schema:
                argv += ["--json-schema", json.dumps(spec.schema)]
        argv += list(spec.extra_args)
        return argv, None

    def parse_output(self, code: int, out: str, err: str) -> RunResult:
        data = last_json_object(out) or {}
        error = str(data.get("error") or ("" if code == 0 else f"exit {code}"))
        return RunResult(ok=code == 0 and not data.get("error"), exit_code=code, stdout=out, stderr=err,
                         structured_output=None, usage=Usage(), error=error)
