from __future__ import annotations

import shlex

from .base import Adapter, RunSpec


class GenericAdapter(Adapter):
    """Any CLI. `command_template` uses {prompt_file} {model} {cwd}; the report comes via .swarm-run/report.json."""
    name = "generic"

    def build_command(self, spec: RunSpec) -> tuple[list[str], bytes | None]:
        template = (self.cfg.command_template if self.cfg else None) or "cat {prompt_file}"
        # Split the configured template first so substituted paths with spaces stay single arguments.
        argv = [arg.format(prompt_file=str(spec.prompt_file), model=spec.model, cwd=str(spec.cwd))
                for arg in shlex.split(template)]
        return argv + list(spec.extra_args), None
