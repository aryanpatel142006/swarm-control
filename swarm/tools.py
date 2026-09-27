"""Toolbox: Claude Code plugins (skills + MCP servers) the swarm installs and enables on each laptop.

`claude plugin list --json` is the source of truth for what is installed; plugins declare MCP servers in
`<installPath>/.mcp.json` (or `mcp.json`, or inline in `.claude-plugin/plugin.json`). Workers get only the
MCP servers the task asks for (see Config.mcp_for), so a plugin being installed costs ~80 tokens of skill
description per session, not a full tool list.
"""
from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .workspace import run_cmd

DEFAULT_MARKETPLACE = "claude-plugins-official"
_ENV_RE = re.compile(r"\$\{(\w+)(?::-([^}]*))?\}")


@dataclass(frozen=True)
class PluginInfo:
    name: str
    id: str
    enabled: bool
    install_path: str


def _run(run, args: list[str], timeout: int = 120):
    return run(args, cwd=Path.cwd(), timeout=timeout)


def installed_plugins(run=run_cmd) -> dict[str, PluginInfo]:
    """Installed Claude Code plugins by short name. Empty when the CLI is missing or its output is unreadable."""
    r = _run(run, ["claude", "plugin", "list", "--json"])
    if not r.ok:
        return {}
    try:
        rows = json.loads(r.out)
    except ValueError:
        return {}
    out: dict[str, PluginInfo] = {}
    for row in rows if isinstance(rows, list) else []:
        pid = str(row.get("id", ""))
        if not pid:
            continue
        name = pid.split("@", 1)[0]
        out[name] = PluginInfo(name=name, id=pid, enabled=bool(row.get("enabled")),
                               install_path=str(row.get("installPath", "")))
    return out


def _expand_env(value):
    if isinstance(value, str):
        return _ENV_RE.sub(lambda m: os.environ.get(m.group(1), m.group(2) or ""), value)
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    return value


def plugin_mcp_servers(plugins: dict[str, PluginInfo]) -> dict:
    """MCP servers declared by installed plugins (enabled or not), by server name, ${VAR:-default} expanded.

    A worker only gets a server when the task names it (Config.mcp_for), so a disabled plugin's server is
    still usable for the task types that ask for it."""
    servers: dict = {}
    for p in plugins.values():
        if not p.install_path:
            continue
        root = Path(p.install_path)
        candidates = [root / ".mcp.json", root / "mcp.json", root / ".claude-plugin" / "plugin.json"]
        for path in candidates:
            try:
                data = json.loads(path.read_text())
            except (OSError, ValueError):
                continue
            if not isinstance(data, dict):
                continue
            block = data.get("mcpServers")
            if not isinstance(block, dict):   # flat form: {"name": {"command": ...}} (playwright plugin)
                block = {k: v for k, v in data.items()
                         if isinstance(v, dict) and ({"command", "url", "type"} & set(v))}
            servers.update({k: _expand_env(v) for k, v in block.items()})
    return servers


def all_mcp_servers(run=run_cmd) -> dict:
    """User-scope servers from ~/.claude.json plus servers shipped by enabled plugins (plugins win on clash)."""
    from .adapters.claude import user_mcp_servers
    merged = dict(user_mcp_servers())
    merged.update(plugin_mcp_servers(installed_plugins(run=run)))
    return merged


def plugin_ref(name: str) -> tuple[str, str]:
    """'context7' -> ('context7', 'context7@claude-plugins-official'); 'x@mkt' stays as given."""
    short, _, mkt = name.partition("@")
    return short, f"{short}@{mkt or DEFAULT_MARKETPLACE}"


def plugin_dirs(names: list[str], installed: dict[str, PluginInfo]) -> list[str]:
    """Install paths to pass as --plugin-dir: the wanted plugins that are installed but not enabled globally."""
    out = []
    for name in names:
        info = installed.get(plugin_ref(name)[0])
        if info and not info.enabled and info.install_path:
            out.append(info.install_path)
    return out


def ensure_plugins(names: list[str], *, run=run_cmd, log: Callable[[str], None] = lambda s: None,
                   which=shutil.which, enable: bool = True) -> list[str]:
    """Install missing plugins; with enable=True also enable disabled ones. Returns names still unavailable.

    enable=False is for per-task plugins: they stay off globally (zero context cost for other tasks) and the
    runner loads them for one run with --plugin-dir."""
    wanted = list(dict.fromkeys(names))
    if not wanted:
        return []
    if not which("claude"):
        log("toolbox: `claude` CLI not on PATH; cannot install plugins: " + ", ".join(wanted))
        return wanted
    have = installed_plugins(run=run)
    missing: list[str] = []
    for name in wanted:
        short, full = plugin_ref(name)
        info = have.get(short)
        if info is None:
            r = _run(run, ["claude", "plugin", "install", full], timeout=600)
            if r.ok:
                log(f"toolbox: installed plugin {full}")
                continue
            log(f"toolbox: could not install {full}: {(r.err or r.out).strip()[:200]}")
            missing.append(name)
        elif enable and not info.enabled:
            r = _run(run, ["claude", "plugin", "enable", short])
            if r.ok:
                log(f"toolbox: enabled plugin {short}")
            else:
                log(f"toolbox: could not enable {short}: {(r.err or r.out).strip()[:200]}")
                missing.append(name)
    return missing


def plugin_settings(installed: dict[str, PluginInfo], allowed: list[str]) -> dict | None:
    """A --settings override that switches off every globally enabled plugin the task does not need.

    Measured on Claude Code 2.1: a worker's base context halves (7.6k -> 3.9k cache-write tokens) when the
    laptop's personal plugins stay out of the run. Returns None when nothing needs switching off."""
    keep = {plugin_ref(n)[0] for n in allowed}
    off = {p.id: False for p in installed.values() if p.enabled and p.name not in keep}
    return {"enabledPlugins": off} if off else None


def ensure_playwright_browser(*, run=run_cmd, log: Callable[[str], None] = lambda s: None) -> bool:
    """Playwright's MCP server needs a Chromium download once per laptop."""
    r = _run(run, ["npx", "--yes", "playwright", "install", "chromium"], timeout=900)
    log("toolbox: playwright chromium " + ("ready" if r.ok else "install failed: " + (r.err or r.out)[-200:]))
    return r.ok
