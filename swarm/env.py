"""Load `~/.swarm/env` (lines like `export NOTION_TOKEN=...`) when the shell did not.

Nothing sources that file automatically, and every new terminal that forgot it failed with
"NOTION_TOKEN is not set" (Oct 4 2026 setup). Values already in the environment always win.
"""
from __future__ import annotations

import os
import re
import shlex
from pathlib import Path

DEFAULT_ENV_FILE = Path.home() / ".swarm" / "env"
_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$")


def load_env_file(path: Path = DEFAULT_ENV_FILE) -> list[str]:
    """Set every KEY=VALUE from `path` that is not already in os.environ. Returns the keys it set."""
    if not path.exists():
        return []
    loaded: list[str] = []
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = _LINE.match(line)
        if not m:
            continue
        key, value = m.group(1), m.group(2).strip()
        try:
            parts = shlex.split(value)
            value = parts[0] if parts else ""
        except ValueError:
            pass
        if key not in os.environ:
            os.environ[key] = value
            loaded.append(key)
    return loaded
