"""Check the adapter's actual emitted flags against the installed CLI before any model call."""
from __future__ import annotations

import os
import re
import shutil
import shlex
from pathlib import Path
from tempfile import TemporaryDirectory

from .adapters import get_adapter
from .adapters.base import RunSpec
from .doctor import CLI_BINARY, Check
from .workspace import run_cmd


def check_headless(cfg, agent_name: str, *, run=run_cmd, which=shutil.which) -> Check:
    label = f"headless:{agent_name}"
    agent = cfg.agents.get(agent_name)
    if agent is None:
        return Check(label, False, f"unknown agent; choose one of: {', '.join(cfg.agents)}")
    if agent.provider == "generic":
        try:
            command = shlex.split(agent.command_template or "")
            command = [arg.format(prompt_file="prompt.md", model=agent.models["low"], cwd=str(cfg.repo_root))
                       for arg in command]
        except (ValueError, KeyError) as exc:
            return Check(label, False, f"invalid command template: {exc}")
        if not command:
            return Check(label, False, "empty command template")
        executable = cfg.repo_root / command[0]
        if not which(command[0]) and not (executable.is_file() and os.access(executable, os.X_OK)):
            return Check(label, False, f"{command[0]} not found or not executable")
        return Check(label, True, "custom command found; live smoke report required to verify compatibility")
    binary = CLI_BINARY[agent.provider]
    if not which(binary):
        return Check(label, False, f"{binary} not found on PATH")
    help_argv = [binary, "exec", "--help"] if agent.provider == "codex" else [binary, "--help"]
    help_result = run(help_argv, cwd=cfg.repo_root, timeout=30)
    if not help_result.ok:
        return Check(label, False, f"{' '.join(help_argv)} failed: " + (help_result.err or help_result.out)[:200])
    # Derive flags from the adapter so this check stays aligned when an adapter changes.
    try:
        with TemporaryDirectory(prefix="swarm-preflight-") as folder:
            cwd = Path(folder)
            prompt = cwd / "prompt.md"
            prompt.write_text("preflight only; no model invocation")
            spec = RunSpec(prompt_file=prompt, model=agent.models["low"], effort=agent.effort.get("low"),
                           max_turns=3, budget_usd=0.5, timeout_s=180, cwd=cwd,
                           schema={"type": "object", "properties": {"status": {"type": "string"}},
                                   "required": ["status"], "additionalProperties": False},
                           sandbox=agent.sandbox, extra_args=list(agent.extra_args))
            argv, _ = get_adapter(agent).build_command(spec)
    except (OSError, ValueError, KeyError) as exc:
        return Check(label, False, f"cannot build adapter command: {exc}")
    flags = list(dict.fromkeys(arg.split("=", 1)[0] for arg in argv[1:] if arg.startswith("-") and arg != "-"))
    help_text = help_result.out + "\n" + help_result.err
    missing = [flag for flag in flags if not re.search(r"(?<![\w-])" + re.escape(flag) + r"(?![\w-])", help_text)]
    if missing:
        return Check(label, False, f"{binary} help is missing adapter flags: {', '.join(missing)}; update CLI or adapter")
    return Check(label, True, f"{binary}: adapter flags supported; live smoke still verifies auth/model/report")
