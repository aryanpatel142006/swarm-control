"""Environment checks: tokens, CLIs, git, gh, verify scripts, and a one-turn smoke test per agent."""
from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

from .config import Config
from .workspace import run_cmd

CLI_BINARY = {"claude": "claude", "codex": "codex", "antigravity": "agy", "gemini": "gemini", "grok": "grok"}


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""


def run_checks(cfg: Config, host: str | None, *, offline: bool = False, notion_token: str | None = None,
               which=shutil.which, run=run_cmd) -> list[Check]:
    checks = [Check("python", sys.version_info >= (3, 11), sys.version.split()[0]),
              Check("config", True, str(cfg.path))]
    checks.append(Check("host", bool(host and host in cfg.hosts),
                        host or "set SWARM_HOST to one of: " + ", ".join(cfg.hosts)))
    token = notion_token if notion_token is not None else os.environ.get("NOTION_TOKEN")
    if not token:
        checks.append(Check("notion token", False, "NOTION_TOKEN not set"))
    elif offline:
        checks.append(Check("notion token", True, "present (offline: not verified)"))
    else:
        from .board.notion import NotionClient, NotionError
        try:
            me = NotionClient(token).me()
            checks.append(Check("notion token", True, f"connection ok as {me.get('name') or me.get('id')}"))
        except NotionError as e:
            checks.append(Check("notion token", False, str(e)))
    ids_ok = bool(cfg.notion.tasks_ds and cfg.notion.questions_ds and cfg.notion.agents_ds)
    checks.append(Check("notion ids", ids_ok, "present" if ids_ok else "run `swarm init --parent-page <id>`"))
    checks.append(Check("git", bool(which("git")), which("git") or "missing"))
    gh = which("gh")
    if gh and not offline:
        r = run(["gh", "auth", "status"], cwd=cfg.repo_root, timeout=30)
        text = (r.out + r.err).strip()
        checks.append(Check("gh auth", r.ok, text.splitlines()[0] if text else ""))
    else:
        checks.append(Check("gh", bool(gh), gh or "missing"))
    agents = cfg.agents_on_host(host) if host in cfg.hosts else list(cfg.agents.values())
    for a in agents:
        if a.provider == "generic":
            exe = (a.command_template or "").split()[0] if a.command_template else ""
            checks.append(Check(f"cli:{a.name} (generic)", bool(exe and (which(exe) or Path(exe).exists())), exe))
            continue
        binary = CLI_BINARY[a.provider]
        path = which(binary)
        detail = path or f"{binary} not found on PATH"
        if path and not offline:
            r = run([binary, "--version"], cwd=cfg.repo_root, timeout=30)
            text = (r.out or r.err).strip()
            detail = text.splitlines()[0] if text else path
        checks.append(Check(f"cli:{a.name} ({a.provider})", bool(path),
                            detail + (" [experimental adapter]" if a.experimental else "")))
    for label, rel in (("verify_fast", cfg.verify.fast), ("verify_full", cfg.verify.full)):
        if rel:
            p = cfg.repo_root / rel
            checks.append(Check(label, p.exists(), str(p) if p.exists() else f"missing {rel}"))
    return checks


def smoke_agent(cfg: Config, agent_name: str, tmp_dir: Path) -> Check:
    from .adapters import get_adapter
    from .adapters.base import RunSpec
    a = cfg.agents[agent_name]
    tmp_dir.mkdir(parents=True, exist_ok=True)
    (tmp_dir / ".swarm-run").mkdir(exist_ok=True)
    pf = tmp_dir / "smoke.md"
    pf.write_text("Reply with exactly this JSON and nothing else, then stop: "
                  '{"status":"done","summary":"smoke ok"}. If you cannot return structured output, '
                  "write that JSON to .swarm-run/report.json.")
    schema = {"type": "object", "properties": {"status": {"type": "string"}, "summary": {"type": "string"}},
              "required": ["status"]}
    spec = RunSpec(prompt_file=pf, model=a.models["low"], effort=a.effort.get("low"), max_turns=3, budget_usd=0.5,
                   timeout_s=180, cwd=tmp_dir, schema=schema, sandbox=a.sandbox)
    r = get_adapter(a).run(spec)
    ok = r.ok and (r.structured_output is not None or (tmp_dir / ".swarm-run" / "report.json").exists())
    cost = f" · ${r.usage.cost_usd}" if r.usage.cost_usd else ""
    return Check(f"smoke:{agent_name}", ok, (r.error or "ok")[:200] + cost)
