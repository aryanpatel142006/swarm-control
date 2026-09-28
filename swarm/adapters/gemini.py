from __future__ import annotations

from ..models import RunResult, Usage
from .base import Adapter, RunSpec, last_json_object


class GeminiAdapter(Adapter):
    """Gemini CLI headless (`gemini -p`), logged in with a Google account or an API key. Deprecated upstream
    (Homebrew disables it on 2026-12-18 in favour of antigravity-cli), fine for the Oct 2026 event."""
    name = "gemini"

    def build_command(self, spec: RunSpec) -> tuple[list[str], bytes | None]:
        # --skip-trust: headless runs exit 55 in a directory the CLI has not "trusted"; worktrees are always new
        argv = ["gemini", "-p", spec.prompt_file.read_text(), "--output-format", "json", "-m", spec.model,
                "--skip-trust"]
        if not spec.read_only:
            argv += ["--approval-mode", "yolo"]
        argv += list(spec.extra_args)
        return argv, None

    def parse_output(self, code: int, out: str, err: str) -> RunResult:
        data = last_json_object(out) or {}
        raw_error = data.get("error")
        if isinstance(raw_error, dict):
            raw_error = raw_error.get("message") or str(raw_error)
        error = str(raw_error or ("" if code == 0 else f"exit {code}"))
        # no --json-schema on this CLI: a report object in the reply text is the structured output
        response = data.get("response")
        inner = last_json_object(response) if isinstance(response, str) else None
        structured = inner if isinstance(inner, dict) and "status" in inner else None
        return RunResult(ok=code == 0 and not raw_error, exit_code=code, stdout=out, stderr=err,
                         structured_output=structured, usage=Usage(), error=error)
