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
               which=shutil.which, run=run_cmd, platform: str = sys.platform) -> list[Check]:
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
    wanted = list(dict.fromkeys(list(cfg.plugins_required)
                                + [p for names in cfg.plugins_by_type.values() for p in names]))
    if wanted and any(a.provider == "claude" for a in agents):
        from .tools import installed_plugins, plugin_ref
        have = installed_plugins(run=run) if which("claude") else {}
        for name in wanted:
            short, full = plugin_ref(name)
            info = have.get(short)
            ok = bool(info and info.enabled)
            detail = (f"{info.id} enabled" if ok else
                      f"{full} " + ("installed but disabled" if info else "not installed")
                      + "; run `swarm tools install`")
            checks.append(Check(f"plugin:{short}", ok, detail))
    if platform == "darwin":   # four runner drops in the Sep 28 rehearsal were one laptop going to sleep
        r = run(["pmset", "-g"], cwd=cfg.repo_root, timeout=15)
        line = next((ln for ln in r.out.splitlines() if ln.strip().startswith("sleep ")), "") if r.ok else ""
        prevented = "prevented" in line or line.split()[1:2] == ["0"]
        checks.append(Check("sleep", bool(line) and prevented,
                            "sleep prevented (caffeinate or never sleep)" if prevented else
                            "laptop can sleep: run every loop as `caffeinate -dims swarm run` / `caffeinate -dims swarm serve` and plug in"))
    for label, rel in (("verify_fast", cfg.verify.fast), ("verify_full", cfg.verify.full)):
        if rel:
            p = cfg.repo_root / rel
            checks.append(Check(label, p.exists(), str(p) if p.exists() else f"missing {rel}"))
    checks.extend(context_checks(cfg))
    return checks


def context_checks(cfg: Config) -> list[Check]:
    """Worker context is only as good as its config: every docs_by_type ref must resolve to text, and every
    *_by_type key must be a task type or a family (`ml`), or workers silently get nothing (Oct 4 2026)."""
    from .models import TASK_TYPES
    from .prompt import read_doc
    broken = []
    for refs in cfg.docs_by_type.values():
        for ref in refs:
            if not read_doc(cfg.repo_root, ref):
                broken.append(ref)
    checks = [Check("docs refs", not broken,
                    "all docs_by_type refs resolve" if not broken else "no text for: " + ", ".join(dict.fromkeys(broken))
                    + " (section refs match the heading text, e.g. PLAN.md#Ground rules)")]
    families = {t.split("_", 1)[0] for t in TASK_TYPES}
    valid = set(TASK_TYPES) | families | {"_all"}
    bad = [f"{m}.{k}" for m, mapping in (("docs_by_type", cfg.docs_by_type), ("skills_by_type", cfg.skills_by_type),
                                         ("mcp_by_type", cfg.mcp_by_type), ("plugins_by_type", cfg.plugins_by_type))
           for k in mapping if k not in valid]
    checks.append(Check("type keys", not bad,
                        "all *_by_type keys are task types" if not bad else "unknown keys: " + ", ".join(bad)))
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
