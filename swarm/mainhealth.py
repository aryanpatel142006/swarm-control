"""Is main green? serve runs the fast verify on each new origin/main commit and `swarm status` turns a red main into
a RISK line that names the failing check.

Oct 11 2026 (selective-hearing): direct commits to main left verify_fast red (ruff RUF046 from aa36b460, I001 and
FURB167 from d30cc4ad/e20bf749, test_judge_profile after 943a7157, test_app_static after T-478). Every task branched
from main inherited rc=1; workers reported it one by one (Q-2029, Q-2030, Q-2071) and a docs-only task had to fix
backend files to get its own verify green. Nobody saw it in `swarm status`.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

_RUFF_RE = re.compile(r"^(\S+?\.py):\d+:\d+: ([A-Z]+\d+)\b", re.M)
_RUFF_CONCISE_RE = re.compile(r"^([A-Z]+\d+) .*?\b(\S+?\.py)\b", re.M)
_FAILED_RE = re.compile(r"^(?:FAILED|ERROR) (\S+?::\S+|\S+\.py)\b", re.M)


def default_health_path(project: str) -> Path:
    return Path.home() / ".swarm" / project / "main_health.json"


def summarize_failure(output: str, cap: int = 3) -> str:
    """The failing checks in a verify log: ruff codes with their file, pytest node ids, else the last line."""
    output = output or ""
    bits: list[str] = []
    for f, code in _RUFF_RE.findall(output):
        item = f"ruff {code} {f}"
        if item not in bits:
            bits.append(item)
    if not bits:
        for code, f in _RUFF_CONCISE_RE.findall(output):
            item = f"ruff {code} {f}"
            if item not in bits:
                bits.append(item)
    for t in _FAILED_RE.findall(output):
        if t not in bits:
            bits.append(t)
    if bits:
        more = f" (+{len(bits) - cap} more)" if len(bits) > cap else ""
        return "; ".join(bits[:cap]) + more
    lines = [ln.strip() for ln in output.splitlines() if ln.strip()]
    return lines[-1][:160] if lines else "no output"


def write_health(path: Path, *, sha: str, ok: bool, command: str, output: str, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    data = {"sha": sha, "ok": bool(ok), "command": command, "at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "failing": "" if ok else summarize_failure(output), "tail": "" if ok else output[-3000:]}
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1))
    tmp.replace(path)
    return data


def read_health(path: Path) -> dict | None:
    try:
        data = json.loads(Path(path).read_text())
        return data if isinstance(data, dict) and "ok" in data else None
    except (OSError, ValueError):
        return None


def risk_line(health: dict | None) -> str:
    if not health or health.get("ok"):
        return ""
    return (f"main is red at {str(health.get('sha', ''))[:8]} ({health.get('command') or 'verify'}: "
            f"{health.get('failing') or 'failed'}, checked {str(health.get('at', ''))[11:16]} UTC): every task "
            "branched from main inherits it. Fix main first (one bugfix task at critical, or a direct fix through "
            "the pre-push gate); `swarm hooks --install --pre-push --verify` keeps it from happening again")


def check_main(ws, cfg, path: Path, *, log=print, force: bool = False, now: datetime | None = None) -> dict | None:
    """Run verify.fast on current origin/main when main moved since the last check (or `force`). Returns the new
    record, or None when nothing was checked."""
    command = cfg.verify.fast
    if not command:
        return None
    ws.fetch()
    sha = ws.git(ws.repo_root, "rev-parse", f"{ws.remote}/{ws.main_branch}", check=False).out.strip()
    if not sha:
        return None
    last = read_health(path)
    if last and last.get("sha") == sha and last.get("command") == command and not force:
        return None
    wt = ws.provision_detached("_main-health", sha)
    try:
        ws.run_script(wt, cfg.verify.setup_worktree, 600, slot=False)
        r = ws.run_script(wt, command, 1800)
    finally:
        ws.dispose(wt)
    if r is None:
        return None
    rec = write_health(path, sha=sha, ok=r.ok, command=command, output=r.out + ("\n" + r.err if r.err else ""),
                       now=now)
    if not r.ok:
        log(f"main is red at {sha[:8]}: {command} failed ({rec['failing']})")
    elif last and not last.get("ok"):
        log(f"main is green again at {sha[:8]}")
    return rec
