"""Perplexity agent CLI, run headless in the task worktree.

UNVERIFIED DEFAULTS. We have not seen the Perplexity agent CLI yet: the binary name, every flag below and the model
ids in the docs (`sonar-pro`, `sonar`) are placeholders. Each agent can override them in `.swarm/config.yaml`:

    agents:
      perplexity-b:
        provider: perplexity
        cli: perplexity                  # binary on PATH (or an absolute path)
        args_template: "-p --prompt-file {prompt_file} --model {model} --cwd {cwd} --output-format json"
        approve_args: ["--yes"]          # auto-approve tool use; dropped on read-only runs (review, plan)

`args_template` takes the placeholders `{prompt_file}`, `{model}` and `{cwd}`. When it has no `{prompt_file}`, the
prompt's text is piped on stdin instead (for a CLI that only reads stdin). The CLI runs with the worktree as its cwd
either way, must exit 0 when it finished, and writes its report to `.swarm-run/report.json` (the worker prompt asks
for that, since this CLI has no structured-output flag we know of). Once the teammate reports the real command, set
`cli` / `args_template` / `approve_args` and confirm with `swarm doctor --smoke <agent>`.
"""
from __future__ import annotations

import shlex
import string

from ..models import RunResult, Usage
from .base import RATE_LIMIT_RE, Adapter, RunSpec, last_json_object

DEFAULT_CLI = "perplexity"                                   # unverified
DEFAULT_ARGS_TEMPLATE = ("-p --prompt-file {prompt_file} --model {model} --cwd {cwd} "
                         "--output-format json")             # unverified
DEFAULT_APPROVE_ARGS = ["--yes"]                             # unverified
PLACEHOLDERS = {"prompt_file", "model", "cwd"}


def template_fields(template: str) -> set[str]:
    """Placeholder names used in an args_template (`{model}` -> "model")."""
    return {name for _, name, _, _ in string.Formatter().parse(template) if name is not None}


class PerplexityAdapter(Adapter):
    """Perplexity agent CLI: `<cli> <args_template> <approve_args> <extra_args>`; binary and flags configurable."""
    name = "perplexity"

    def _cli(self) -> str:
        return (getattr(self.cfg, "cli", None) if self.cfg else None) or DEFAULT_CLI

    def _template(self) -> str:
        return (getattr(self.cfg, "args_template", None) if self.cfg else None) or DEFAULT_ARGS_TEMPLATE

    def _approve(self) -> list[str]:
        approve = getattr(self.cfg, "approve_args", None) if self.cfg else None
        return list(DEFAULT_APPROVE_ARGS if approve is None else approve)

    def build_command(self, spec: RunSpec) -> tuple[list[str], bytes | None]:
        template = self._template()
        args = template.format(prompt_file=str(spec.prompt_file), model=spec.model, cwd=str(spec.cwd))
        argv = shlex.split(self._cli()) + shlex.split(args)
        if not spec.read_only:
            argv += self._approve()
        argv += list(spec.extra_args)
        stdin = None if "prompt_file" in template_fields(template) else spec.prompt_file.read_bytes()
        return argv, stdin

    def parse_output(self, code: int, out: str, err: str) -> RunResult:
        data = last_json_object(out) or {}
        raw_error = data.get("error")
        if isinstance(raw_error, dict):
            raw_error = raw_error.get("message") or str(raw_error)
        error = str(raw_error or ("" if code == 0 else f"exit {code}"))
        # a report object in the reply text counts as structured output (field name unverified: try the usual ones)
        reply = next((data[k] for k in ("result", "response", "text", "output") if isinstance(data.get(k), str)), None)
        inner = last_json_object(reply) if reply else None
        structured = inner if isinstance(inner, dict) and "status" in inner else None
        u = data.get("usage") or {}
        usage = Usage(input_tokens=int(u.get("input_tokens", u.get("prompt_tokens", 0)) or 0),
                      output_tokens=int(u.get("output_tokens", u.get("completion_tokens", 0)) or 0),
                      cost_usd=data.get("total_cost_usd") or data.get("cost_usd"))
        ok = code == 0 and not raw_error
        # some CLIs print API errors on stdout: check the error and the tail of stdout, never a successful run's text
        rate_limited = not ok and bool(RATE_LIMIT_RE.search(error + "\n" + out[-2000:]))
        return RunResult(ok=ok, exit_code=code, stdout=out, stderr=err, structured_output=structured, usage=usage,
                         session_id=data.get("session_id") or data.get("sessionId"), error=error,
                         rate_limited=rate_limited)
