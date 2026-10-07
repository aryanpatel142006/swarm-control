"""Self-improvement: turn a run's evidence into lessons the next runs read and config changes they obey.

Rules are deterministic (no model call): they read the board, the ledger, main's history and the decision logs.
Text findings go to docs/LESSONS.md (inlined into every worker brief and the planner prompt); safe config
changes go to .swarm/tuning.yaml, which load_config merges over config.yaml (comments in config.yaml survive).
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import yaml

from .models import TIERS, Status, Task
from .usage import Ledger

MAX_LESSONS = 20


@dataclass
class Finding:
    rule: str
    text: str
    patch: dict | None = None   # dotted config keys; a trailing '+' means "append to this list"


@dataclass
class Evidence:
    tasks: list[Task]
    ledger: Ledger
    board: str
    files_by_task: dict[str, list[str]]        # files each merged task touched on main
    tools_by_task: dict[str, list[dict]]       # tools_used entries from docs/decisions/<id>.md
    agents: dict[str, str]                     # agent name -> provider
    models_by_agent: dict[str, dict[str, str]] = field(default_factory=dict)   # agent -> tier -> model
    known_skills: set[str] | None = None       # skills that exist (project + plugins); None: accept any
    known_mcp: set[str] | None = None          # MCP servers the config can start; None: accept any
    defaults: dict[str, list[str]] = field(default_factory=dict)   # "skills_by_type.<type>" -> tools already default
    ledger_rows: list[dict] | None = None      # set by scoped(): the ledger rows of one milestone's tasks


def scoped(ev: Evidence, milestone: str | None) -> Evidence:
    """The evidence of one milestone's tasks. A retro runs when a milestone completes; with all-time evidence every
    retro on the selective-hearing board (M4..M9, Oct 6) re-reported the same five findings, M0's T-002 included."""
    if not milestone:
        return ev
    ids = {t.id for t in ev.tasks if t.milestone == milestone}
    rows = [r for r in (ev.ledger_rows if ev.ledger_rows is not None else ev.ledger._rows()) if r.get("task") in ids]
    return Evidence(tasks=[t for t in ev.tasks if t.id in ids], ledger=ev.ledger, board=ev.board, ledger_rows=rows,
                    files_by_task={k: v for k, v in ev.files_by_task.items() if k in ids},
                    tools_by_task={k: v for k, v in ev.tools_by_task.items() if k in ids},
                    agents=ev.agents, models_by_agent=ev.models_by_agent, known_skills=ev.known_skills,
                    known_mcp=ev.known_mcp, defaults=ev.defaults)


def _tier_of(models: dict[str, str], model: str) -> str | None:
    for tier in reversed(TIERS):   # low → best
        if models.get(tier) == model:
            return tier
    return None


def _next_model(models: dict[str, str], model: str) -> str | None:
    ladder = [t for t in reversed(TIERS) if models.get(t)]
    tier = _tier_of(models, model)
    if tier is None:
        return None
    for t in ladder[ladder.index(tier) + 1:]:
        if models[t] != model:
            return models[t]
    return None


def findings_from(ev: Evidence) -> list[Finding]:
    out: list[Finding] = []
    tasks = {t.id: t for t in ev.tasks}
    rows = [r for r in (ev.ledger_rows if ev.ledger_rows is not None else ev.ledger._rows())
            if not ev.board or r.get("board") == ev.board]

    # 1. a cheap model that keeps ending at the turn limit / timeout on a task type: raise that type's floor
    failed = Counter()
    for r in rows:
        t = tasks.get(r.get("task", ""))
        if t and (r.get("role") or "worker") == "worker" and not r.get("ok"):
            failed[(t.type, r.get("agent"), r.get("model"))] += 1
    seen_types = set()
    for (ttype, agent, model), n in sorted(failed.items(), key=lambda kv: -kv[1]):
        if n < 2 or ttype in seen_types:
            continue
        provider = ev.agents.get(agent, "")
        models = ev.models_by_agent.get(agent, {})
        tier, better = _tier_of(models, model), _next_model(models, model)
        if not provider or not tier or not better:
            continue
        seen_types.add(ttype)
        ids = sorted({r["task"] for r in rows if r.get("model") == model and not r.get("ok")
                      and tasks.get(r["task"]) and tasks[r["task"]].type == ttype})
        out.append(Finding("turn-limit",
                           f"{ttype} tasks on {model} failed {n} times ({', '.join(ids)}); floor for {ttype}/{tier} on "
                           f"{provider} raised to {better}.",
                           {f"routing.type_model_overrides.{provider}.{ttype}.{tier}": better}))

    # 2. small tasks that needed many attempts: a sizing lesson for the planner
    small = [t for t in ev.tasks if t.size == "S" and t.attempts >= 3]
    if small:
        types = sorted({t.type for t in small})
        out.append(Finding("undersized",
                           "Sized S but needed several attempts: " + ", ".join(f"{t.id} ({t.type}, {t.attempts} attempts)" for t in small)
                           + f". Plan {'/'.join(types)} work like these as M, not S (S has the smallest turn and time budget)."))

    # 3. one file edited by several tasks: conflicts and extra review rounds
    owners: dict[str, set[str]] = defaultdict(set)
    for tid, files in ev.files_by_task.items():
        for f in files:
            owners[f].add(tid)
    # the lesson is "one owner per milestone", so count owners within a milestone, not across the whole board
    by_ms: dict[tuple[str, str], set[str]] = defaultdict(set)
    for f, ids in owners.items():
        for tid in ids:
            by_ms[(tasks[tid].milestone if tid in tasks else "", f)].add(tid)
    shared = {k: ids for k, ids in by_ms.items()
              if len(ids) >= 3 or (len(ids) >= 2 and (k[1].startswith("docs/") or k[1].endswith(".md")))}
    if shared:
        ranked = sorted(shared.items(), key=lambda kv: (-len(kv[1]), kv[0][1]))
        parts = [f"{f} by {', '.join(sorted(ids))}" + (f" ({ms})" if ms else "") for (ms, f), ids in ranked]
        out.append(Finding("shared-file", "Edited by several tasks of one milestone: " + "; ".join(parts[:5])
                           + (f" and {len(parts) - 5} more" if len(parts) > 5 else "")
                           + ". Give each shared file one owning task per milestone (or a dependency chain); others "
                             "read it only. `swarm add` and the planner now name open tasks that cite the same file."))

    # 4. a tool that helped on two or more tasks of a type becomes that type's default
    helped: dict[tuple[str, str, str], set[str]] = defaultdict(set)
    for tid, tools in ev.tools_by_task.items():
        t = tasks.get(tid)
        if not t:
            continue
        for tool in tools:
            kind, name = str(tool.get("kind") or "skill"), str(tool.get("name") or "")
            if not tool.get("helped") or not name or kind not in ("skill", "mcp"):
                continue   # plain CLIs (git, pytest) and built-in tools are not config
            known = ev.known_skills if kind == "skill" else ev.known_mcp
            if known is not None and name not in known:
                continue
            helped[(t.type, kind, name)].add(tid)
    patch: dict[str, list[str]] = {}
    by_type: dict[str, list[str]] = defaultdict(list)
    for (ttype, kind, name), ids in sorted(helped.items()):
        if len(ids) < 2:
            continue
        key = f"{'mcp_by_type' if kind == 'mcp' else 'skills_by_type'}.{ttype}"
        if name in ev.defaults.get(key, []):
            continue   # already a default: re-announcing it every retro buried the new ones (Oct 6, five repeats)
        patch.setdefault(key + "+", []).append(name)
        by_type[ttype].append(f"{name} ({kind}; {', '.join(sorted(ids))})")
    if patch:
        out.append(Finding("tool-default", "New defaults by task type: "
                           + "; ".join(f"{ttype}: {', '.join(items)}" for ttype, items in sorted(by_type.items()))
                           + ".", patch))

    # 5. review-heavy tasks: reported, never auto-changed
    heavy = [t for t in ev.tasks if t.review_rounds >= 3]
    if heavy:
        out.append(Finding("review-rounds", "Needed 3+ review rounds: "
                           + ", ".join(f"{t.id} ({t.type}, {t.review_rounds})" for t in heavy)
                           + ". Check the acceptance criteria and the shared files behind each one."))
    return out


def _set_path(data: dict, dotted: str, value) -> bool:
    keys = dotted.split(".")
    node = data
    for k in keys[:-1]:
        node = node.setdefault(k, {})
        if not isinstance(node, dict):
            return False
    if node.get(keys[-1]) == value:
        return False
    node[keys[-1]] = value
    return True


def _get_path(data: dict, dotted: str):
    node = data
    for k in dotted.split("."):
        if not isinstance(node, dict) or k not in node:
            return None
        node = node[k]
    return node


def apply_patch(tuning_path: Path, patch: dict, base: dict | None = None) -> bool:
    """Write the patch into the tuning overlay. '+' keys store the merged list (base + tuning + new)."""
    tuning_path = Path(tuning_path)
    tuning = (yaml.safe_load(tuning_path.read_text()) or {}) if tuning_path.exists() else {}
    changed = False
    for key, value in patch.items():
        if key.endswith("+"):
            dotted = key[:-1]
            current = list(_get_path(tuning, dotted) or _get_path(base or {}, dotted) or [])
            merged = list(dict.fromkeys(current + list(value)))
            if merged != current or _get_path(tuning, dotted) is None:
                changed |= _set_path(tuning, dotted, merged)
        else:
            changed |= _set_path(tuning, key, value)
    if changed:
        tuning_path.parent.mkdir(parents=True, exist_ok=True)
        tuning_path.write_text("# Written by `swarm retro`; merged over config.yaml on load. Edit or delete freely.\n"
                               + yaml.safe_dump(tuning, sort_keys=False))
    return changed


def lessons_markdown(existing: str | None, findings: list[Finding], when: str | None = None) -> str:
    """docs/LESSONS.md: one bullet per rule, newest wins, capped. Short enough to sit in every brief."""
    when = when or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    bullets: dict[str, str] = {}
    for line in (existing or "").splitlines():
        m = re.match(r"- \*\*([a-z-]+)\*\* \((\d{4}-\d{2}-\d{2})\): (.*)", line)
        if m:
            bullets[m.group(1)] = line
    for f in findings:
        bullets[f.rule] = f"- **{f.rule}** ({when}): {f.text}"
    kept = list(bullets.values())[-MAX_LESSONS:]
    return ("# Lessons\n\nWritten by `swarm retro` from what happened on this board. Every worker and the planner read "
            "this file; keep it short.\n\n" + "\n".join(kept) + "\n")


# ---------- the driver: evidence from the repo and the board, then write and commit ----------
_SUBJECT = re.compile(r"^(T-\d+) ·")
_TOOL = re.compile(r"([A-Za-z0-9_.:-]+) \(([a-z]+)(?:, (helped|did not help))?\)")


def files_by_task_from_git(ws, wt: Path, main_ref: str, limit: int = 400) -> dict[str, list[str]]:
    log = ws.git(wt, "log", f"-{limit}", "--format=%H%x09%s", main_ref, check=False)
    out: dict[str, list[str]] = defaultdict(list)
    for line in (log.out or "").splitlines():
        sha, _, subject = line.partition("\t")
        m = _SUBJECT.match(subject)
        if not m:
            continue
        files = ws.git(wt, "show", "--name-only", "--format=", sha, check=False).out.split()
        for f in files:
            if f.startswith("docs/decisions/") or f.startswith("docs/debt/"):
                continue
            if f not in out[m.group(1)]:
                out[m.group(1)].append(f)
    return dict(out)


def tools_by_task_from_logs(wt: Path) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for path in sorted((wt / "docs" / "decisions").glob("T-*.md")):
        tools: list[dict] = []
        for line in path.read_text(errors="replace").splitlines():
            if "Tools used" not in line:
                continue
            for name, kind, helped in _TOOL.findall(line.split(":**", 1)[-1] if ":**" in line else line):
                tools.append({"name": name, "kind": kind, "helped": helped == "helped"})
        if tools:
            out[path.stem] = tools
    return out


def run_retro(cfg, board, ws, *, ledger: Ledger, log=print, dry_run: bool = False, now: datetime | None = None,
              milestone: str | None = None) -> list[Finding]:
    """Collect evidence, derive findings, write LESSONS.md + tuning.yaml + a report, commit and push to main. With
    `milestone` (serve passes the milestone that just completed) only that milestone's tasks count."""
    now = now or datetime.now(timezone.utc)
    wt = ws.main_worktree()
    board_id = (cfg.notion.tasks_ds or "")[:8]
    known_skills = {p.parent.name for p in (wt / ".claude" / "skills").glob("*/SKILL.md")}
    known_skills |= {p.parent.name for p in (Path.home() / ".claude" / "plugins" / "cache").glob("*/*/*/skills/*/SKILL.md")}
    known_mcp = set(cfg.mcp_servers) | {m for v in cfg.mcp_by_type.values() for m in v} | {m for v in cfg.mcp_by_importance.values() for m in v}
    try:
        from .tools import all_mcp_servers
        known_mcp |= set(all_mcp_servers())
    except Exception:  # noqa: BLE001 - discovery is best effort
        pass
    ev = Evidence(tasks=board.list_tasks(), ledger=ledger, board=board_id, known_skills=known_skills, known_mcp=known_mcp,
                  files_by_task=files_by_task_from_git(ws, wt, ws._main_ref()),
                  tools_by_task=tools_by_task_from_logs(wt),
                  agents={a.name: a.provider for a in cfg.agents.values()},
                  models_by_agent={a.name: dict(a.models) for a in cfg.agents.values()},
                  defaults={**{f"skills_by_type.{k}": list(v) for k, v in cfg.skills_by_type.items()},
                            **{f"mcp_by_type.{k}": list(v) for k, v in cfg.mcp_by_type.items()}})
    ev = scoped(ev, milestone)
    if milestone and not ev.tasks:
        log(f"retro: no tasks in milestone {milestone}")
        return []
    findings = findings_from(ev)
    for f in findings:
        if milestone:
            f.text = f"{milestone}: {f.text}"
    stamp = now.strftime("%Y-%m-%d %H:%M UTC")
    for f in findings:
        log(f"retro [{f.rule}] {f.text}" + (f" → {f.patch}" if f.patch else ""))
    if not findings:
        log("retro: nothing to learn from this board yet")
        return []
    if dry_run:
        return findings
    lessons_path = wt / "docs" / "LESSONS.md"
    lessons_path.parent.mkdir(parents=True, exist_ok=True)
    lessons_path.write_text(lessons_markdown(lessons_path.read_text() if lessons_path.exists() else None, findings,
                                             when=now.strftime("%Y-%m-%d")))
    base = yaml.safe_load((wt / ".swarm" / "config.yaml").read_text()) if (wt / ".swarm" / "config.yaml").exists() else {}
    patched = []
    for f in findings:
        if f.patch and apply_patch(wt / ".swarm" / "tuning.yaml", f.patch, base=base):
            patched.append(f.rule)
    report = wt / "docs" / "retro" / f"{now.strftime('%Y-%m-%d-%H%M')}.md"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(f"# Retro · {stamp}" + (f" · {milestone}" if milestone else "")
                      + f"\n\nBoard `{board_id or '-'}`, {len(ev.tasks)} tasks, "
                      f"{sum(1 for t in ev.tasks if t.status is Status.DONE)} done.\n\n"
                      + "\n".join(f"- **{f.rule}**: {f.text}" + (f"\n  - applied: `{f.patch}`" if f.rule in patched else "")
                                  for f in findings) + "\n")
    subject = f"swarm retro{' ' + milestone if milestone else ''}: {len(findings)} findings ({', '.join(f.rule for f in findings)})"
    if not ws.commit_all(wt, subject):
        log("retro: nothing new to commit")
        return findings
    pushed = ws.push(wt, cfg.main_branch)
    log(f"retro: {subject}" + ("" if pushed.ok else f" (push failed: {pushed.err.strip()[:120]})"))
    return findings
